from __future__ import annotations

from contextlib import suppress, asynccontextmanager
import asyncio
from dataclasses import dataclass

from pal.execution.contracts import CapabilityResult
from pal.execution.runtime import ExecutionRuntime
from pal.execution.tool_facade import (
    CompleteResult, PagedResult, EffectOutcome, EffectReceipt, ToolRejectedError,
    ToolAffordance, ToolExecutionError,
)
from pal.shared.result_rendering import render_structured_for_llm
from pal.shared import RuntimeStatus

from .adapter import ShellRuntime, ShellRejected, TERMINAL, READ_EFFECTS
from .tools import session_affordances
from .remote_contract import RemoteFailure
from .recovery import retry_read, LOGGER


REMOTE_PENDING_BYTES = 64 * 1024 * 1024



def native_action(record):
    return record.binding.descriptor.metadata.get("native_shell_action", "")


def output_result(result):
    # Control receipts, epochs, cursors and journal IDs never enter model text.
    fields = ("session_id", "target", "status", "returncode", "signal", "error",
              "stdout", "stderr", "tty", "watching", "has_deadline", "remaining_ms",
              "has_wake", "wake_remaining_ms", "truncated", "output_error")
    payload = {key: result[key] for key in fields if key in result}
    if result.get("status") not in TERMINAL:
        payload["returncode"] = None
    text = render_structured_for_llm(payload)
    return CapabilityResult(status=RuntimeStatus.OK, structured=payload, text=text, llm_text=text,
                            effect_receipt=EffectReceipt(outcome=EffectOutcome.APPLIED))


@dataclass
class PendingOutput:
    result: dict
    turn_id: str
    raw: CapabilityResult | None = None
    prepared: bool = False
    recovery_of: str = ""
    delivered: bool = False
    failure: str = ""
    covers_output: bool = True


class NativeShellOwner:
    """One process/write owner, shared by immutable registry projections."""

    def __init__(self):
        self._shell = None
        self.core = None
        self.events = None
        self.pending = {}
        self.sessions = {}
        self.closed = False
        self.defer_delivery = False
        self.require_output_delivery = False
        self.on_ready = None
        self.write_task = None
        self.remote_port = None
        from .approval import ShellApprovals
        self.approvals = ShellApprovals(self)
        from .observation_owner import ObservationOwner
        self.observations = ObservationOwner(self)

    @property
    def completion_blocked(self):
        return self.completion_blocked_for(None)

    def completion_blocked_for(self, target):
        return bool(any(not s.get('terminal_delivered') and not s.get('output_failure_reported') and
                        s.get('watching', True)
                        for s in self.sessions.values() if target is None or s.get('target', 0) == target)
                    or any(not p.delivered for p in self.pending.values() if target is None or p.result.get('target', 0) == target)
                    or (target in (None, 0) and self._shell is not None and self._shell._foreground))

    @property
    def has_work(self):
        observed_work = any(s.get("watching", True) or s.get("latest_status") not in TERMINAL
                            for s in self.sessions.values())
        return bool(observed_work or self.pending or (self._shell is not None
                    and (self._shell._foreground or self._shell.execution_work)))

    @asynccontextmanager
    async def write_scope(self):
        task = asyncio.current_task()
        previous = self.write_task
        if previous is not None and previous is not task:
            raise ShellRejected(
                f"write_busy: another host write is active (sessions={sorted(self.sessions)}, "
                f"pending={sorted(self.pending)})")
        self.write_task = task
        try:
            yield
        finally:
            self.write_task = previous

    @property
    def shell(self):
        if self.closed:
            raise ShellRejected("shell runtime is closed")
        if self._shell is None:
            from .router import ShellRouter
            self._shell = ShellRouter(owner=self, on_ready=self.notify)
        return self._shell

    def attach_remote(self, port):
        if self.remote_port is not None:
            raise RuntimeError("Remote backend already attached")
        self.remote_port = port
        self.observations.retry_acknowledgements()
        if self._shell is not None:
            self._shell.loop.call_soon_threadsafe(self._shell.resume)

    def detach_remote(self, port):
        if self.remote_port is port:
            self.remote_port = None

    def notify(self):
        self.observations.collect()
        if self.on_ready is not None:
            self.on_ready()
        if self.core is not None:
            self.core.notify_ready()

    async def stage(self, call, result, *, recovery_of="", raw=None):
        self.observations.record(result)
        tool_call = call.meta["tool_call"]
        pending = PendingOutput(result, str(call.meta.get("turn_id") or ""), recovery_of=recovery_of)
        self.pending[tool_call.call_id] = pending
        try:
            if result.get("target") and raw is None:
                retained = sum(item.result.get("stdout_total", 0) + item.result.get("stderr_total", 0)
                               for item in self.pending.values() if item.result.get("target") and item.raw is not None)
                incoming = result.get("stdout_total", 0) + result.get("stderr_total", 0)
                if retained + incoming > REMOTE_PENDING_BYTES:
                    raise RemoteFailure("output_capacity", "Local output retention capacity is exhausted", effect="applied")
            pending.raw = raw if raw is not None else output_result(await retry_read(
                lambda: self.shell.materialize(result), stage="output", identity=tool_call.call_id))
        except Exception as exc:
            pending.failure = "Command output is unavailable: " + str(exc)
            pending.raw = output_result({**result, "output_error": pending.failure})
        return pending.raw

    async def commit(self, call_id):
        pending = self.pending.get(call_id)
        if pending is None:
            return
        sid = pending.result["session_id"]
        session = self.sessions.get(sid)
        if session is not None:
            session["committed"] = True
        if not pending.prepared and not pending.failure:
            self.notify()
            return
        pending.delivered = True
        if not pending.covers_output:
            self.observations.note_tool_state(call_id, pending.result, turn_id=pending.turn_id)
            self.pending.pop(call_id, None)
            return
        if pending.failure:
            if session is not None:
                session["output_failure_reported"] = True
                self.observations.failures[sid] = pending.failure
            self.observations.note_tool_state(call_id, pending.result, turn_id=pending.turn_id)
            for old_id, old in tuple(self.pending.items()):
                if old_id != call_id and old.delivered and old.failure and old.result.get("output_id") == pending.result.get("output_id"):
                    self.pending.pop(old_id, None)
            if pending.result["status"] in TERMINAL:
                from .adapter import Completion
                self.observations.acknowledge(Completion(sid, pending.turn_id, pending.result))
            return
        if session is not None:
            session.pop("output_failure_reported", None)
            self.observations.failures.pop(sid, None)
            session["output_offsets"] = {stream: max(session.get("output_offsets", {}).get(stream, 0), pending.result.get(stream + "_total", 0))
                                         for stream in ("stdout", "stderr")}
            self.observations.note_tool_delivery(call_id, pending.result, turn_id=pending.turn_id)
        if pending.result["status"] in TERMINAL:
            from .adapter import Completion
            self.observations.acknowledge(Completion(sid, pending.turn_id, pending.result))
        else:
            # For a live partial snapshot, retire just its recovery ancestry.
            while (retired := self.pending.pop(call_id, None)) is not None:
                call_id = retired.recovery_of
        self.notify()

    async def interrupt(self, turn_id):
        if self._shell is None:
            return
        await self.shell.interrupt_turn(turn_id)
        # The adapter's return alone does not establish delivery to the model.
        for sid, session in list(self.sessions.items()):
            if sid >= 1 << 48:
                continue  # Independent remote tasks and their delivery tickets survive turn interruption.
            if session["origin_turn"] == turn_id and not session["committed"]:
                with suppress(ShellRejected):
                    await self.shell.terminate(sid)
                    await self.shell._discard_cancelled_session(sid)
                self.forget_session(sid)
        for call_id, pending in list(self.pending.items()):
            if pending.result.get("target"):
                continue
            if pending.turn_id == turn_id and pending.result["session_id"] not in self.sessions:
                with suppress(ShellRejected):
                    await self.shell.release_output(pending.result)
                self.pending.pop(call_id, None)

    def forget_session(self, session_id):
        """Drop host references after native output was explicitly retired."""
        if not session_id:
            return  # Zero identifies many independent one-shot results.
        self.observations.forget(session_id)
        self.sessions.pop(session_id, None)
        for call_id, pending in list(self.pending.items()):
            if pending.result["session_id"] == session_id:
                self.pending.pop(call_id, None)

    def check_idle(self):
        if any(self.observations.acking.values()) or any(
            not task.done() for store in (self.observations.refreshing, self.observations.preparing)
            for task in store.values()
        ):
            raise ShellRejected("execution_busy: background observation or acknowledgement is still active")
        if self.has_work or (self.write_task and not self.write_task.done()):
            raise ShellRejected("execution_busy: finish or reconcile native work before detaching")
        if self.events and any(not task.done() for task in self.events.tasks):
            raise ShellRejected("execution_busy: observation delivery is still running")
        if self._shell and (self._shell.remote_foreground or self._shell._pending):
            raise ShellRejected("execution_busy: shell requests are still active")

    def close_idle(self):
        self.check_idle()
        if self._shell:
            self._shell.close_idle()
            self._shell = None
        self.closed = True
        self.observations.closed = True
        self.pending.clear()
        self.sessions.clear()
        if self.events:
            self.events.pending.clear()
            self.events.failures.clear()
            self.events.in_flight.clear()

    async def close(self):
        self.closed = True
        await self.observations.close()
        if self.events is not None:
            await self.events.close()
        if self._shell is not None:
            await self._shell.close()
        self._shell = None
        self.pending.clear()
        self.sessions.clear()

    async def reset(self):
        if self._shell is not None and self._shell.remote_work:
            raise ShellRejected("remote_busy: reconcile and release remote operations before reset")
        await self.close()
        self.closed = False


class NativeExecutionRuntime(ExecutionRuntime):
    def __init__(self, *, owner=None, **kwargs):
        super().__init__(**kwargs)
        self.shell_owner = owner or NativeShellOwner()
        self._owns_shell = owner is None

    def build_introspection_provider(self):
        from .capabilities import build_provider
        return build_provider(self)

    def build_runtime_state_port(self):
        from .state import NativeExecutionStatePort
        return NativeExecutionStatePort(self)

    def project_execution_view(self, view):
        return self.project_view(view, self.shell_owner)

    def role_capabilities(self, allowed):
        from pal.bunshin.scoped_execution import SHELL_EVIDENCE_CAPABILITIES
        if (SHELL_EVIDENCE_CAPABILITIES | {"op_exec_shell"}).intersection(allowed):
            return list(dict.fromkeys([*allowed, "op_exec_session", "op_exec_status", "op_tool_call", "op_tool_read"]))
        return allowed

    def project_role_descriptor(self, descriptor):
        from dataclasses import replace
        from .guidance import SESSION_GUIDANCE
        canonical = descriptor.canonical_path
        metadata = dict(descriptor.metadata)
        guidance = descriptor.guidance
        if metadata.get('native_shell_action'):
            metadata['preserve_role_contract'] = True
        if canonical in {"op_exec_session", "op_exec_status"}:
            metadata['preserve_role_invocation_mode'] = True
        if canonical == "op_exec_shell":
            guidance = guidance.model_copy(update={
                "use_when": guidance.use_when + " wait_ms controls response waiting, not process lifetime; timeout_ms is an optional hard deadline."
                    " A nonzero session_id identifies retained execution. The next model request immediately reads prepared observations."
                    " A text-only response while work remains yields until an eligible event. Use tty=true for interactive input.",
                "failure_next_steps": guidance.failure_next_steps + " Do not replay a live session or poll repeatedly."
                    " Use shell_session for PTY input, termination or a needed fresh snapshot."
                    " Success requires status=exited and returncode=0. Pending shell output blocks writes and submission.",
            })
        if canonical == "op_exec_session":
            guidance = SESSION_GUIDANCE
        if canonical == "op_exec_status":
            guidance = guidance.model_copy(update={"failure_next_steps": "Report unavailable role execution through the role result; do not inspect or repair the resident runtime."})
        return replace(descriptor, guidance=guidance, metadata=metadata)

    def create_role_session_driver(self):
        from .role_sessions import BunshinShellSessions
        return BunshinShellSessions(self)

    def prepare_model_context(self, memory, continuation, *, context_view=None):
        self.shell_owner.observations.project(self, memory, continuation, context_view=context_view)

    def observe_tool_delivery(self, call, result):
        # Public results deliberately lack the host event identity. The pending
        # captured raw result is confirmed by acknowledge_tool_result_async.
        return None

    def model_response_received(self, continuation):
        self.shell_owner.observations.refresh()

    def stagnation_payload(self, call, result):
        from .observation_owner import semantic_state
        invocation = result.invocation_result
        if (getattr(invocation, 'kind', None) == 'rejected'
            and invocation.error_code in {'shell_write_busy', 'shell_result_capacity', 'invalid_session',
                                          'stdin_closed', 'shell_rejected'}):
            return {'ok': False, 'error_code': invocation.error_code, 'error': invocation.error,
                    'effect': invocation.effect, 'details': invocation.details}
        args = dict(call.args)
        name = call.name
        if name == 'call_tool':
            name, args = args.get('name'), args.get('args', {})
        if name in {'shell_session', 'op_exec_session', 'shell_status', 'op_exec_status'}:
            if args.get('action', 'read') == 'read' and not args.get('wait_ms'):
                payload = result.structured or {}
                pending = self.shell_owner.pending.get(call.call_id)
                if isinstance(invocation, PagedResult) and pending is not None:
                    # Use this call's captured result, never a newer live session.
                    payload = pending.result
                if payload.get('session_id'):
                    return {'ok': result.ok, **semantic_state(payload),
                            'stdout': payload.get('stdout'), 'stderr': payload.get('stderr'),
                            'output_bytes': {stream: payload.get(stream + '_total') for stream in ('stdout', 'stderr')}}
                if name in {'shell_status', 'op_exec_status'}:
                    return {'ok': result.ok, 'structured': payload}
        return super().stagnation_payload(call, result)

    async def close_role_work(self):
        await self.shell_owner.close()

    async def complete_evidence(self, call, result, **kwargs):
        from pal.shared.tool_protocol import new_tool_call
        if call.name == "op_exec_shell":
            while result.ok and (result.structured or {}).get("status") in {"running", "terminating"}:
                result = await self.execute_tool_async(new_tool_call(name="call_tool", args={
                    "name": "shell_session", "args": {"session_id": result.structured["session_id"], "wait_ms": 300000},
                }, call_id=call.call_id), **kwargs)
        return result

    def execution_diagnostics(self):
        return {"backend": "pal-shell-native", "sessions": len(self.shell_owner.sessions)}

    @classmethod
    def project_view(cls, view, owner):
        runtime = cls(owner=owner, runtime_root=view.runtime_root, logical_state=view.logical_state,
                      tool_result_pager=view.tool_result_pager, sync_executor=view.sync_executor,
                      lifecycle_gate=view.lifecycle_gate)
        runtime._registry_generation = view.registry_generation
        return runtime

    async def _call_record_async(self, record, binding, call, validated, turn_id, budget, allow_tools):
        arguments = record, binding, call, validated, turn_id, budget, allow_tools
        from pal.execution.runtime import _is_plugin_lifecycle_tool
        if _is_plugin_lifecycle_tool(record.alias):
            return await super()._call_record_async(*arguments)
        try:
            if record.execution.effect_kind.value in READ_EFFECTS or (native_action(record) and native_action(record) != "run"):
                return await super()._call_record_async(*arguments)
            target = record.binding.descriptor.metadata.get('native_shell_target', getattr(validated, 'target', 0))
            if native_action(record) == 'run' and target != 0:
                return await super()._call_record_async(*arguments)
            async with self.shell_owner.write_scope():
                if native_action(record) == "run":
                    return await super()._call_record_async(*arguments)
                if self.shell_owner.require_output_delivery and self.shell_owner.completion_blocked_for(0):
                    raise ShellRejected("write_busy: shell results are not ready yet; continue when their execution result is available")
                if record.binding.descriptor.metadata.get("delegates_execution"):
                    # This compound tool invokes run_shell itself. Keep host
                    # exclusivity, but let the child own its native write lease.
                    async with self.shell_owner.shell.tool_admission(record.execution.effect_kind.value):
                        pass
                    return await super()._call_record_async(*arguments)
                async with self.shell_owner.shell.tool_admission(record.execution.effect_kind.value):
                    return await super()._call_record_async(*arguments)
        except RemoteFailure as exc:
            # Reconcile only the original ticket. A lost reply never authorizes replay.
            if exc.operation_id and exc.effect != "not_started":
                try:
                    recovered = await retry_read(lambda: self.shell_owner.shell.reconcile(exc.operation_id),
                                                 stage="query", identity=exc.operation_id)
                    if "session_id" in recovered:
                        sid = recovered["session_id"]
                        if sid and sid not in self.shell_owner.sessions:
                            ticket = self.shell_owner.shell.tickets[sid]
                            self.shell_owner.sessions[sid] = dict(self.shell_owner.shell.operation_context[ticket.operation_id])
                        from types import SimpleNamespace
                        return await self.shell_owner.stage(SimpleNamespace(meta={"tool_call": call, "turn_id": turn_id}), recovered)
                    return output_result(recovered)
                except Exception:
                    LOGGER.warning("shell operation unresolved identity=%s", exc.operation_id)
            hints = [ToolAffordance(tool="call_tool", arguments={"name": "list_remote", "args": {}},
                                   reason="Check whether the execution target is available.")]
            receipt = EffectReceipt(outcome=EffectOutcome(exc.effect), receipt={"operation_id": exc.operation_id})
            message = ("The requested shell operation may have taken effect; its outcome could not be confirmed. "
                       "Do not repeat it automatically." if exc.effect == "unknown" else str(exc))
            raise ToolExecutionError(message, error_code=exc.code, effect_receipt=receipt,
                                     affordances=hints) from exc
        except ShellRejected as exc:
            prefix = str(exc).partition(":")[0]
            code = {"write_busy": "shell_write_busy", "invalid_session": "invalid_session",
                    "result_capacity": "shell_result_capacity", "stdin_closed": "stdin_closed"}.get(prefix, "shell_rejected")
            hints = []
            details = {}
            sid = call.args.get('session_id')
            if prefix == 'invalid_session':
                details['session_id'] = sid
                details['next_step'] = 'Consult the previously delivered result or result_handle; do not replay the command.'
            elif prefix in {'write_busy', 'result_capacity'}:
                details['blocking_sessions'] = [
                    {'session_id': key, 'status': item.get('latest_status', 'running'),
                     'watching': item.get('watching', True)}
                    for key, item in self.shell_owner.sessions.items() if item.get('target', 0) == 0]
                if not details['blocking_sessions'] and not hints:
                    hints.append(ToolAffordance(tool='call_tool', arguments={'name': 'shell_status', 'args': {}},
                        reason='The blocking resource is not identified in this result.'))
            elif sid:
                hints.append(ToolAffordance(tool='call_tool', arguments={'name': 'shell_session',
                    'args': {'session_id': sid, 'action': 'read', 'wait_ms': 0}},
                    reason='Read this session only if its current state is needed to resolve the precondition.'))
            raise ToolRejectedError(str(exc), error_code=code, affordances=hints, details=details) from exc

    def _call_record_sync(self, record, *args):
        if record.execution.effect_kind.value not in READ_EFFECTS:
            raise ToolRejectedError("Native shell mode requires asynchronous execution for writes/control.",
                                    error_code="native_async_required")
        return super()._call_record_sync(record, *args)

    def _normalize_invocation_result(self, record, call, raw, **kwargs):
        result = super()._normalize_invocation_result(record, call, raw, **kwargs)
        if not native_action(record):
            return result
        pending = self.shell_owner.pending.get(call.call_id)
        if isinstance(result, (CompleteResult, PagedResult)):
            if pending:
                pending.prepared = True
            payload = raw.structured if isinstance(raw, CapabilityResult) else getattr(raw, "output", None)
            if isinstance(payload, dict):
                updates = {"affordances": result.affordances + session_affordances(payload)}
                if isinstance(result, PagedResult):
                    header = {key: value for key, value in payload.items() if key not in {"stdout", "stderr"}}
                    updates["llm_text"] = render_structured_for_llm(header) + "\nOutput preview (complete snapshot available via result_handle):\n" + result.llm_text
                result = result.model_copy(update=updates)
        return result

    async def _invoke_tool_record_async(self, generation, call, **kwargs):
        result = await super()._invoke_tool_record_async(generation, call, **kwargs)
        record = generation.record_for_alias(call.name)
        if record is None or not native_action(record):
            return result  # call_tool's resolved inner invocation owns the handoff.
        pending = self.shell_owner.pending.get(call.call_id)
        if pending is None:
            return result
        if not isinstance(result, (CompleteResult, PagedResult)):
            # Retry only normalization of retained output, never the tool handler.
            if pending.raw is not None:
                for _ in range(2):
                    try:
                        recovered = self._normalize_invocation_result(record, call, pending.raw, budget=kwargs.get("budget"), turn_id=kwargs.get("turn_id"))
                    except Exception:
                        continue
                    if isinstance(recovered, (CompleteResult, PagedResult)):
                        result = recovered
                        break
            if not isinstance(result, (CompleteResult, PagedResult)):
                pending.failure = "The command result could not be delivered. Do not rerun the command to retrieve output."
                result = result.model_copy(update={"llm_text": pending.failure, "effect": EffectOutcome.APPLIED})
        core = self.shell_owner.core
        if not self.shell_owner.defer_delivery and (core is None or pending.turn_id not in core.state.active_turns):
            # Embedded callers own delivery at the returned result boundary.
            await self.shell_owner.commit(call.call_id)
        return result

    async def acknowledge_tool_result_async(self, call_id, turn_id):
        pending = self.shell_owner.pending.get(call_id)
        if pending is not None and pending.turn_id == turn_id:
            await self.shell_owner.commit(call_id)

    async def interrupt_turn(self, turn_id):
        await self.shell_owner.interrupt(turn_id)
        await super().interrupt_turn(turn_id)

    async def shutdown_async(self):
        if self._owns_shell:
            await self.shell_owner.close()
            super().shutdown()

    async def prepare_shutdown_async(self):
        # Native process handles cannot be checkpointed across a process exit.
        # Quiesce/reap them before the ordinary L1/execution snapshot is saved.
        await self.shell_owner.close()

    def shutdown(self):
        if self._owns_shell and self.shell_owner._shell is not None:
            raise RuntimeError("await shutdown_async() before closing a native execution runtime")
        if self._owns_shell:
            super().shutdown()
