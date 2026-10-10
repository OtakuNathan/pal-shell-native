from __future__ import annotations

from typing import Literal
from dataclasses import replace
import asyncio
from pydantic import ConfigDict, Field, model_validator

from pal.execution.capabilities import ExecutionIntrospectionProvider
from pal.execution.contracts import CapabilityResult
from pal.execution.tool_facade import EmptyToolInput, StrictToolModel, StructuredToolOutput, ToolGuidance, NextToolHint, ToolRejectedError
from pal.execution.tool_semantics import DIRECT_CONTROL, INDIRECT_CONTROL, INDIRECT_LOCAL_READ
from pal.shared.result_rendering import render_structured_for_llm
from pal.shared import RuntimeStatus, capability_action

from .tools import RunInput, SessionInput, session_argument_schema
from .guidance import REMOTE_RUN_GUIDANCE, RESIDENT_SESSION_GUIDANCE


class NativeRunInput(RunInput):
    target: int = Field(default=0, ge=0, strict=True, description="Execution target; 0 is local. Discover configured remote target IDs with list_remote when needed; reuse a known target. Paths belong to this target.")
    sudo: bool = Field(default=False, description="Remote targets only; requires tty=false and approval for a supported management command.")


class RemoteStartInput(StrictToolModel):
    target: int = Field(gt=0, strict=True)
    action: str = Field(min_length=1, description="Exact start action advertised for this target by list_remote.")


class RemotePowerInput(StrictToolModel):
    target: int = Field(gt=0, strict=True)
    action: Literal["shutdown"] = "shutdown"


class TargetRunInput(RunInput):
    sudo: bool = Field(default=False, description="Remote targets only; requires tty=false and approval for a supported management command.")


class ListRemoteInput(StrictToolModel):
    refresh: bool = Field(default=False, description="Refresh selected targets without waking them.")
    target: int | None = Field(default=None, ge=0, strict=True, description="Exact target ID; omit to list all. 0 is local. A selected refresh probes only this target.")
    view: Literal["summary", "detail"] = Field(default="summary", description="Summary for target selection and readiness; detail for resources, limits and protocol diagnostics.")


def _target_summary(item):
    """Project decision-relevant facts; unknown observations stay unknown."""
    summary = {key: item[key] for key in (
        "target", "name", "shortcut", "reachable", "probe_error", "needs_start",
        "requires_start", "registered", "execution", "start_actions", "expected_offline",
        "os", "arch", "shell", "probed_at",
    ) if key in item}
    if "static" in item:
        summary["usage"] = item["static"].get("usage", "")
    if "dynamic" in item:
        dynamic = item["dynamic"]
        summary["dynamic"] = None if dynamic is None else {
            key: dynamic.get(key) for key in (
                "os", "worker_arch", "shell_ready", "active_tasks", "observed_at", "required_user_action",
            )
        }
    return summary


class NativeSessionInput(SessionInput):
    model_config = ConfigDict(strict=True, extra="forbid", json_schema_extra={
        **session_argument_schema(),
        "oneOf": [
            {"required": ["session_id"], "properties": {"session_id": {"type": "integer"}, "output_ref": {"type": "null"}}},
            {"required": ["output_ref"], "properties": {"output_ref": {"type": "string"}, "session_id": {"type": "null"},
                "action": {"enum": ["read", "release"]}, "wait_ms": {"type": "null"}}},
        ],
    })
    session_id: int | None = Field(default=None, gt=0, le=9223372036854775807)
    output_ref: str | None = Field(default=None, min_length=1, description="Supply exactly one of session_id or output_ref. With output_ref, omit all controls including wait_ms; only read/release are allowed. Retained output reference returned after an export failure; read retries export without running the command, release explicitly discards it.")

    @model_validator(mode="after")
    def validate_output_reference(self):
        if (self.session_id is None) == (self.output_ref is None):
            raise ValueError("Provide exactly one of session_id or output_ref")
        if self.output_ref is not None and (self.action not in {"read", "release"} or self.wait_ms is not None):
            raise ValueError("output_ref supports only read or release, without waiting")
        return self


SESSION_ACTIONS = ("read", "write", "resize", "terminate", "release", "watch", "extend", "unwatch")
SESSION_CAPABILITIES = frozenset(f"op_exec_session_{action}" for action in SESSION_ACTIONS)


class SessionIdentifierInput(StrictToolModel):
    session_id: int = Field(gt=0, le=9223372036854775807)


class RetainedOutputInput(StrictToolModel):
    model_config = ConfigDict(strict=True, extra="forbid", json_schema_extra={"oneOf": [
        {"required": ["session_id"], "properties": {"session_id": {"type": "integer"}, "output_ref": {"type": "null"}}},
        {"required": ["output_ref"], "properties": {"output_ref": {"type": "string"}, "session_id": {"type": "null"}, "wait_ms": {"type": "null"}}},
    ]})
    session_id: int | None = Field(default=None, gt=0, le=9223372036854775807)
    output_ref: str | None = Field(default=None, min_length=1, description="Exactly one of session_id or output_ref. A retained output reference permits no waiting and never reruns its producer.")

    @model_validator(mode="after")
    def validate_identifier(self):
        if (self.session_id is None) == (self.output_ref is None):
            raise ValueError("Provide exactly one of session_id or output_ref")
        if self.output_ref is not None and getattr(self, "wait_ms", None) is not None:
            raise ValueError("output_ref does not accept wait_ms")
        return self


class ReadSessionInput(RetainedOutputInput):
    wait_ms: int | None = Field(default=None, ge=0, le=300000, description="Wait for exit, default zero. Only with session_id; does not extend deadlines or rearm notifications.")


class WriteSessionInput(SessionIdentifierInput):
    text: str = Field(description="Exact PTY input; include newline to submit. At most 65536 UTF-8 bytes. Acceptance does not prove processing; inspect output before resending uncertain input.")

    @model_validator(mode="after")
    def validate_input_bytes(self):
        if len(self.text.encode("utf-8")) > 65536:
            raise ValueError("PTY input must fit the 64 KiB input queue")
        return self


class ResizeSessionInput(SessionIdentifierInput):
    rows: int = Field(ge=1, le=65535)
    columns: int = Field(ge=1, le=65535)


class WatchSessionInput(SessionIdentifierInput):
    wait_ms: int = Field(gt=0, le=300000, description="Arm one background decision event; returns immediately.")
    extend_by_ms: int | None = Field(default=None, ge=0, le=2147483647, description="Optional atomic addition to an unexpired extendable finite deadline; does not create a deadline.")


class ExtendSessionInput(SessionIdentifierInput):
    extend_by_ms: int = Field(gt=0, le=2147483647, description="Add to an existing unexpired extendable finite deadline; no deadline cannot be extended.")


STATUS_GUIDANCE = ToolGuidance(
    search_objects=("session", "sessions"),
    purpose="Show current shell sessions and their last observed execution states.",
    use_when="A session ID or execution state is needed.",
    do_not_use_when="The returned result already contains the session and next operation you need.",
    failure_next_steps="Use inspect_execution_state and observe_core if the execution backend itself is unavailable.",
    next_tool_hints=(NextToolHint(name="manage_shell_session", use_when="Inspect or control a listed session."),),
)


class NativeExecutionProvider(ExecutionIntrospectionProvider):
    @capability_action(
        namespace="operation", scope="module", family="exec", action_name="shell", aliases=("run_shell",),
        InputModel=NativeRunInput, OutputModel=StructuredToolOutput, execution=DIRECT_CONTROL,
        async_handler_name="shell_async", metadata={"background_execution": True, "preserve_role_contract": True, "native_shell_action": "run"}, guidance=REMOTE_RUN_GUIDANCE,
    )
    def shell(self, call):
        raise RuntimeError("native shell requires asynchronous execution")

    async def shell_async(self, call):
        execution = call.meta["execution_runtime"]
        owner = execution.shell_owner
        budget = call.meta.get("budget")
        limit = execution._resolve_char_limit(budget) if budget is not None else None
        turn_id = str(call.meta.get("turn_id") or "")
        continuation = owner.core.state.active_turns.get(turn_id) if owner.core is not None else None
        lease_id = call.meta["tool_call"].call_id
        owner.input_leases[lease_id] = (execution.result_snapshots, execution.result_snapshots.lease_request(turn_id))
        delivery_context = {"input_lease_id": lease_id, "origin_turn": turn_id, "budget": budget, "binding": getattr(continuation, "delivery_binding", None),
                            "committed": False, "cmd": call.args["cmd"], "tty": bool(call.args.get("tty"))}
        try:
            result = await owner.shell.run(**dict(call.args), turn_id=turn_id, delivery_context=delivery_context, retain_output=True, load_output=False,
                                           inline_limit=-1 if limit is None else min(limit, 2147483647))
        except BaseException as exc:
            from .adapter import ShellRejected
            if (isinstance(exc, ShellRejected) or getattr(exc, "effect", "") == "not_started"
                or isinstance(exc, asyncio.CancelledError) and not call.args.get("target", 0)):
                # The local adapter reaps a cancelled submission before raising.
                owner.release_input_lease(lease_id)
            # Unknown remote outcomes retain their input lease until reconciliation
            # or owner shutdown; a transport failure does not prove execution ended.
            raise
        sid = result["session_id"]
        if sid:
            continuation = owner.core.state.active_turns.get(turn_id) if owner.core is not None else None
            owner.sessions[sid] = {
                "input_lease_id": lease_id, "origin_turn": turn_id, "budget": budget,
                "binding": getattr(continuation, "delivery_binding", None),
                "committed": False, "cmd": call.args["cmd"], "tty": bool(call.args.get("tty")), "target": result.get("target", 0),
            }
        from .adapter import TERMINAL
        if not sid or result["status"] in TERMINAL:
            owner.release_input_lease(lease_id)
        return await owner.stage(call, result)

    @capability_action(
        namespace="operation", scope="module", family="exec", action_name="session", aliases=("manage_shell_session",),
        InputModel=NativeSessionInput, OutputModel=StructuredToolOutput, execution=INDIRECT_CONTROL,
        examples=({"session_id": 1, "action": "read"},),
        async_handler_name="session_async", metadata={"background_execution": True, "preserve_role_invocation_mode": True, "native_shell_action": "session"}, guidance=RESIDENT_SESSION_GUIDANCE,
    )
    def session(self, call):
        raise RuntimeError("native shell requires asynchronous execution")

    async def session_async(self, call):
        owner = call.meta["execution_runtime"].shell_owner
        args = dict(call.args)
        output_ref = args.pop("output_ref", None)
        if output_ref is not None:
            pending = owner.pending.get(output_ref)
            if pending is None or not pending.delivered or not pending.failure:
                raise ToolRejectedError("Retained output reference is unavailable", error_code="invalid_output_ref")
            if args.get("action", "read") == "release":
                await owner.shell.release_output(pending.result)
                owner.pending.pop(output_ref, None)
                owner.forget_session(pending.result.get("session_id", 0))
                return self._result({"status": "released"})
            return await owner.stage(call, pending.result, recovery_of=output_ref)
        if args.get("action", "read") == "read":
            for old_id, pending in tuple(owner.pending.items()):
                if pending.delivered and pending.failure and pending.result.get("session_id") == args["session_id"]:
                    return await owner.stage(call, pending.result, recovery_of=old_id)
        if args.get("action") == "release" and args["session_id"] >= 1 << 48:
            from .recovery import retry_read
            result = await retry_read(lambda: owner.shell.session_snapshot(**args),
                                      stage="release", identity=args["session_id"])
        else:
            result = await owner.shell.session_snapshot(**args)
        owner.observations.record(result)
        tracked = owner.sessions.get(args["session_id"])
        if tracked is not None:
            tracked["latest_status"] = result["status"]
        if result["status"] == "terminating" and owner.events is not None:
            owner.events.invalidate(args["session_id"], result)
        if args.get("action") in {"watch", "extend", "unwatch"}:
            sid = args["session_id"]
            session = owner.sessions.get(sid)
            if session is not None:
                session.update(watching=result.get("watching", True), latest_status=result["status"],
                               watch_generation=result.get("watch_generation", 0))
                if args.get("action") == "watch":
                    session.pop("output_failure_reported", None)
            if owner.events is not None:
                owner.events.invalidate(sid, result)
            from .output_contract import output_result, PendingOutput
            raw = output_result(result)
            owner.pending[call.meta["tool_call"].call_id] = PendingOutput(
                result, str(call.meta.get("turn_id") or ""), raw=raw, covers_output=False)
            return raw
        if result["status"] == "released":
            owner.forget_session(result["session_id"])
            return self._result({"session_id": result["session_id"], "status": "released"})
        return await owner.stage(call, result)


    @capability_action(
        namespace="operation", scope="module", family="exec", action_name="session_read",
        aliases=("read_shell_session",), InputModel=ReadSessionInput, OutputModel=StructuredToolOutput,
        execution=INDIRECT_CONTROL, async_handler_name="session_read_async",
        examples=({'session_id': 1},),
        metadata={"background_execution": True, "preserve_role_invocation_mode": True, "native_shell_action": "session", "native_shell_session_action": "read"},
        guidance=ToolGuidance(search_objects=('session', 'sessions', 'output'),
            purpose='Read an existing shell session or retry export of retained output.', use_when='Use a returned identifier for a needed fresh snapshot or retained-output recovery. Successful delivery updates output acknowledgement; do not poll repeatedly or rerun the command.',
            do_not_use_when="Do not invent identifiers or replay the original command to retrieve output.",
            failure_next_steps="Inspect previously delivered output or inspect_shell_status. A missing session does not prove the command never ran; reconcile uncertain controls before repeating them.",
        ),
    )
    def session_read(self, call):
        raise RuntimeError("native shell requires asynchronous execution")

    async def session_read_async(self, call):
        return await self.session_async(replace(call, args={**call.args, "action": "read"}))

    @capability_action(
        namespace="operation", scope="module", family="exec", action_name="session_write",
        aliases=("write_shell_session",), InputModel=WriteSessionInput, OutputModel=StructuredToolOutput,
        execution=INDIRECT_CONTROL, async_handler_name="session_write_async",
        examples=({'session_id': 1, 'text': '\n'},),
        metadata={"background_execution": True, "preserve_role_invocation_mode": True, "native_shell_action": "session", "native_shell_session_action": "write"},
        guidance=ToolGuidance(search_objects=('session', 'sessions', 'output'),
            purpose='Send exact input to a live PTY shell session.', use_when='Requires a live PTY with open stdin. Acceptance only confirms queued input; inspect output before repeating uncertain input.',
            do_not_use_when="Do not invent identifiers or replay the original command to retrieve output.",
            failure_next_steps="Inspect previously delivered output or inspect_shell_status. A missing session does not prove the command never ran; reconcile uncertain controls before repeating them.",
        ),
    )
    def session_write(self, call):
        raise RuntimeError("native shell requires asynchronous execution")

    async def session_write_async(self, call):
        return await self.session_async(replace(call, args={**call.args, "action": "write"}))

    @capability_action(
        namespace="operation", scope="module", family="exec", action_name="session_resize",
        aliases=("resize_shell_session",), InputModel=ResizeSessionInput, OutputModel=StructuredToolOutput,
        execution=INDIRECT_CONTROL, async_handler_name="session_resize_async",
        examples=({'session_id': 1, 'rows': 24, 'columns': 80},),
        metadata={"background_execution": True, "preserve_role_invocation_mode": True, "native_shell_action": "session", "native_shell_session_action": "resize"},
        guidance=ToolGuidance(search_objects=('session', 'sessions', 'output'),
            purpose='Resize a live PTY shell session.', use_when='Requires a live PTY; rows and columns are terminal dimensions.',
            do_not_use_when="Do not invent identifiers or replay the original command to retrieve output.",
            failure_next_steps="Inspect previously delivered output or inspect_shell_status. A missing session does not prove the command never ran; reconcile uncertain controls before repeating them.",
        ),
    )
    def session_resize(self, call):
        raise RuntimeError("native shell requires asynchronous execution")

    async def session_resize_async(self, call):
        return await self.session_async(replace(call, args={**call.args, "action": "resize"}))

    @capability_action(
        namespace="operation", scope="module", family="exec", action_name="session_terminate",
        aliases=("terminate_shell_session",), InputModel=SessionIdentifierInput, OutputModel=StructuredToolOutput,
        execution=INDIRECT_CONTROL, async_handler_name="session_terminate_async",
        examples=({'session_id': 1},),
        metadata={"background_execution": True, "preserve_role_invocation_mode": True, "native_shell_action": "session", "native_shell_session_action": "terminate"},
        guidance=ToolGuidance(search_objects=('session', 'sessions', 'output'),
            purpose='Request cancellation of an existing shell session.', use_when='Cancellation acceptance does not establish exit; read terminal status to confirm.',
            do_not_use_when="Do not invent identifiers or replay the original command to retrieve output.",
            failure_next_steps="Inspect previously delivered output or inspect_shell_status. A missing session does not prove the command never ran; reconcile uncertain controls before repeating them.",
        ),
    )
    def session_terminate(self, call):
        raise RuntimeError("native shell requires asynchronous execution")

    async def session_terminate_async(self, call):
        return await self.session_async(replace(call, args={**call.args, "action": "terminate"}))

    @capability_action(
        namespace="operation", scope="module", family="exec", action_name="session_release",
        aliases=("release_shell_session",), InputModel=RetainedOutputInput, OutputModel=StructuredToolOutput,
        execution=INDIRECT_CONTROL, async_handler_name="session_release_async",
        examples=({'session_id': 1},),
        metadata={"background_execution": True, "preserve_role_invocation_mode": True, "native_shell_action": "session", "native_shell_session_action": "release"},
        guidance=ToolGuidance(search_objects=('session', 'sessions', 'output'),
            purpose='Discard completed shell session output or retained failed export.', use_when='Release only when output is no longer needed. A running session cannot be released.',
            do_not_use_when="Do not invent identifiers or replay the original command to retrieve output.",
            failure_next_steps="Inspect previously delivered output or inspect_shell_status. A missing session does not prove the command never ran; reconcile uncertain controls before repeating them.",
        ),
    )
    def session_release(self, call):
        raise RuntimeError("native shell requires asynchronous execution")

    async def session_release_async(self, call):
        return await self.session_async(replace(call, args={**call.args, "action": "release"}))

    @capability_action(
        namespace="operation", scope="module", family="exec", action_name="session_watch",
        aliases=("watch_shell_session",), InputModel=WatchSessionInput, OutputModel=StructuredToolOutput,
        execution=INDIRECT_CONTROL, async_handler_name="session_watch_async",
        examples=({'session_id': 1, 'wait_ms': 1000},),
        metadata={"background_execution": True, "preserve_role_invocation_mode": True, "native_shell_action": "session", "native_shell_session_action": "watch"},
        guidance=ToolGuidance(search_objects=('session', 'sessions', 'output'),
            purpose='Arm one background shell session decision event.', use_when='Returns immediately without stopping execution; optional extension applies only to an unexpired extendable finite deadline.',
            do_not_use_when="Do not invent identifiers or replay the original command to retrieve output.",
            failure_next_steps="Inspect previously delivered output or inspect_shell_status. A missing session does not prove the command never ran; reconcile uncertain controls before repeating them.",
        ),
    )
    def session_watch(self, call):
        raise RuntimeError("native shell requires asynchronous execution")

    async def session_watch_async(self, call):
        return await self.session_async(replace(call, args={**call.args, "action": "watch"}))

    @capability_action(
        namespace="operation", scope="module", family="exec", action_name="session_extend",
        aliases=("extend_shell_session",), InputModel=ExtendSessionInput, OutputModel=StructuredToolOutput,
        execution=INDIRECT_CONTROL, async_handler_name="session_extend_async",
        examples=({'session_id': 1, 'extend_by_ms': 1000},),
        metadata={"background_execution": True, "preserve_role_invocation_mode": True, "native_shell_action": "session", "native_shell_session_action": "extend"},
        guidance=ToolGuidance(search_objects=('session', 'sessions', 'output', 'deadline', 'deadlines'),
            purpose='Extend an existing finite shell session deadline.', use_when='Requires an unexpired extendable finite deadline. No deadline needs no extension.',
            do_not_use_when="Do not invent identifiers or replay the original command to retrieve output.",
            failure_next_steps="Inspect previously delivered output or inspect_shell_status. A missing session does not prove the command never ran; reconcile uncertain controls before repeating them.",
        ),
    )
    def session_extend(self, call):
        raise RuntimeError("native shell requires asynchronous execution")

    async def session_extend_async(self, call):
        return await self.session_async(replace(call, args={**call.args, "action": "extend"}))

    @capability_action(
        namespace="operation", scope="module", family="exec", action_name="session_unwatch",
        aliases=("unwatch_shell_session",), InputModel=SessionIdentifierInput, OutputModel=StructuredToolOutput,
        execution=INDIRECT_CONTROL, async_handler_name="session_unwatch_async",
        examples=({'session_id': 1},),
        metadata={"background_execution": True, "preserve_role_invocation_mode": True, "native_shell_action": "session", "native_shell_session_action": "unwatch"},
        guidance=ToolGuidance(search_objects=('session', 'sessions', 'output'),
            purpose='Disable unsolicited notifications from a shell session.', use_when='Does not stop the process or discard output; watch_shell_session restores attention.',
            do_not_use_when="Do not invent identifiers or replay the original command to retrieve output.",
            failure_next_steps="Inspect previously delivered output or inspect_shell_status. A missing session does not prove the command never ran; reconcile uncertain controls before repeating them.",
        ),
    )
    def session_unwatch(self, call):
        raise RuntimeError("native shell requires asynchronous execution")

    async def session_unwatch_async(self, call):
        return await self.session_async(replace(call, args={**call.args, "action": "unwatch"}))

    @capability_action(
        namespace="operation", scope="module", family="exec", action_name="status", aliases=("inspect_shell_status",),
        InputModel=EmptyToolInput, OutputModel=StructuredToolOutput, execution=INDIRECT_LOCAL_READ,
        guidance=STATUS_GUIDANCE, async_handler_name="shell_status_async", metadata={"background_execution": True, "preserve_role_invocation_mode": True, "native_shell_action": "status"},
    )
    def shell_status(self, call):
        owner = call.meta["execution_runtime"].shell_owner
        return self._result({"sessions": [
            {"session_id": sid, "cmd": item["cmd"], "watching": item.get("watching", True),
             "last_observed_status": item.get("latest_status", "running"), "target": item.get("target", 0),
             **({"observation_error": item["observation_error"]} if item.get("observation_error") else {})}
            for sid, item in owner.sessions.items()]})

    async def shell_status_async(self, call):
        from .adapter import TERMINAL
        owner = call.meta['execution_runtime'].shell_owner
        payload = self.shell_status(call).structured
        local_work = owner.completion_blocked_for(0) or any(
            item.get('target', 0) == 0 and item.get('latest_status') not in TERMINAL
            for item in owner.sessions.values())
        targets = [{'target': 0, 'execution': {'blocked': False, 'has_work': bool(local_work)}}]
        if owner.remote_port:
            if owner._shell:
                for target in {t.target for t in owner.shell.operations.values()}:
                    await owner.shell.sync_target(target)
            targets.extend({'target': item['target'], 'execution': item.get('execution', {})}
                           for item in await owner.remote_port.list(False))
        return self._result({**payload, 'targets': targets})

    @capability_action(
        namespace="operation", scope="module", family="exec", action_name="remote_list", aliases=("list_remote",),
        InputModel=ListRemoteInput, OutputModel=StructuredToolOutput, execution=INDIRECT_LOCAL_READ,
        async_handler_name="list_remote_async", metadata={"background_execution": True, "native_shell_action": "list_remote"}, guidance=ToolGuidance(search_objects=('target', 'targets'),
            purpose="List execution targets and their readiness; defaults to a compact summary.",
            use_when="Find a target or inspect readiness. Reuse a known target ID to limit refresh. Choose view=detail only when resource, privilege or protocol details are needed. Unreachable entries remain valid targets. Inspect probe_error; connection or authentication failure does not prove a startup action is needed.",
            do_not_use_when="A returned session already fixes its target.",
            failure_next_steps="Unreachable entries remain valid targets. Inspect probe_error; connection or authentication failure does not prove a startup action is needed. For deeper diagnosis, repeat with the target ID and view=detail.",
        ),
    )
    def list_remote(self, call):
        raise RuntimeError("Use asynchronous target discovery")

    async def list_remote_async(self, call):
        owner = call.meta["execution_runtime"].shell_owner
        import os
        import platform
        items = [{"target": 0, "name": "local", "requires_start": False, "registered": True,
                  "os": platform.system(), "arch": platform.machine(), "logical_cpus": os.cpu_count(),
                  "shell": {"executable": "/bin/bash", "invocation": ["-lc"]}}]
        target = call.args.get("target")
        if target not in (None, 0):
            items = []
        if owner.remote_port is not None and target != 0:
            items.extend(await owner.remote_port.list(call.args.get("refresh", False), target=target))
        if target is not None and not items:
            raise ToolRejectedError("Target is not configured", error_code="invalid_target")
        view = call.args.get("view", "summary")
        if view == "summary":
            items = [_target_summary(item) for item in items]
        result = self._result({"targets": items, "remote_attached": owner.remote_port is not None, "view": view})
        startable = [(item["target"], item.get("name"), item.get("start_actions"))
                     for item in items if item.get("target") and item.get("needs_start") and item.get("start_actions")]
        if startable:
            from pal.execution.tool_facade import ToolAffordance
            from dataclasses import replace
            result = replace(result, affordances=[
                ToolAffordance(tool="call_tool", arguments={"name": "start_remote_target", "args": {
                    "target": target_id, "action": actions[0]}},
                    reason=f"Target {name} reports needs_start; configured start action '{actions[0]}' is available.")
                for target_id, name, actions in startable])
        return result

    @capability_action(
        namespace="operation", scope="module", family="exec", action_name="remote_start", aliases=("start_remote_target",),
        InputModel=RemoteStartInput, OutputModel=StructuredToolOutput, execution=INDIRECT_CONTROL,
        async_handler_name="remote_start_async", metadata={"background_execution": True, "native_shell_action": "remote_start"}, guidance=ToolGuidance(
            search_objects=("target", "targets"),
            search_terms=("wake", "waking", "boot", "startup"),
            purpose="Explicitly invoke one preconfigured target startup action (wake or service start). Completion does not prove readiness; refresh target metadata to verify.",
            use_when="list_remote reports a configured wake or user-service start action that is needed.",
            do_not_use_when="A target is already available; this is not command replay or worker restart.",
            failure_next_steps="Refresh target metadata to verify readiness; action completion alone does not prove readiness.",
        ),
    )
    def remote_start(self, call):
        raise RuntimeError("Use asynchronous remote management")

    async def remote_start_async(self, call):
        owner = call.meta["execution_runtime"].shell_owner
        payload = dict(await owner.shell._port().call('start', dict(call.args)))
        payload["readiness"] = "unverified"
        result = self._result(payload, status=RuntimeStatus.ERROR if payload.get("returncode", 0) != 0 else RuntimeStatus.OK)
        from pal.execution.tool_facade import ToolAffordance
        from dataclasses import replace
        return replace(result, affordances=[ToolAffordance(tool="call_tool", arguments={"name": "list_remote", "args": {
            "target": call.args["target"], "refresh": True}}, reason="Verify target readiness after the startup action.")])

    @capability_action(
        namespace="operation", scope="module", family="exec", action_name="remote_power", aliases=("shutdown_remote_target",),
        InputModel=RemotePowerInput, OutputModel=StructuredToolOutput, execution=INDIRECT_CONTROL,
        async_handler_name="remote_power_async", metadata={"background_execution": True, "native_shell_action": "remote_power"}, guidance=ToolGuidance(search_objects=('target', 'targets'),
            purpose="Request target shutdown after a single human approval and atomic worker busy check. Accepted does not prove power-off; unknown is an unconfirmed outcome, not success.",
            use_when="list_remote reports management.shutdown.supported=true and the target owner authorizes shutdown.",
            do_not_use_when="Only disconnecting the plugin or stopping one command is intended; never shut down this Pal host.",
            failure_next_steps="Accepted does not prove power-off. An unknown outcome must not be treated as success; busy rejects without scheduling later shutdown.",
        ),
    )
    def remote_power(self, call):
        raise RuntimeError("Use asynchronous remote management")

    async def remote_power_async(self, call):
        owner = call.meta["execution_runtime"].shell_owner
        payload = dict(await owner.shell.privileged(call.args['target'], 'shutdown', turn_id=str(call.meta.get('turn_id') or '')))
        payload["shutdown_confirmed"] = False
        payload["outcome_note"] = "Accepted does not prove power-off; an unknown outcome must not be repeated automatically."
        return self._result(payload, status=RuntimeStatus.ERROR if payload.get("status") == "unknown" else RuntimeStatus.OK)

    @staticmethod
    def _result(payload, *, status=RuntimeStatus.OK):
        text = render_structured_for_llm(payload)
        return CapabilityResult(status=status, structured=payload, text=text, llm_text=text)


def build_provider(runtime):
    """Compile configured projections without extending Pal's alias semantics."""
    import tomllib
    from pathlib import Path
    from dataclasses import replace
    from pal_shell_contracts import Target
    if runtime.runtime_root is None:
        return NativeExecutionProvider(runtime=runtime)
    path = Path(runtime.runtime_root) / 'config' / 'remote.toml'
    targets = [Target(**item) for item in tomllib.loads(path.read_text()).get('targets', [])] if path.exists() else []
    aliases = set()
    ids = set()
    methods = {}
    for target in targets:
        if target.target in ids:
            raise ValueError('Duplicate remote target')
        ids.add(target.target)
        if not target.shortcut:
            continue
        alias = 'run_shell_' + target.shortcut
        if alias in aliases:
            raise ValueError('Target shortcuts must be unique')
        aliases.add(alias)
        name = 'shortcut_' + target.shortcut
        async_name = name + '_async'
        def sync(self, call):
            raise RuntimeError('Use asynchronous native shell')
        async def run(self, call, target_id=target.target):
            return await self.shell_async(replace(call, args={**call.args, 'target': target_id}))
        sync.__name__ = name
        methods[name] = capability_action(namespace='operation', scope='module', family='exec',
            action_name=name, aliases=(alias,), InputModel=TargetRunInput,
            OutputModel=StructuredToolOutput, execution=DIRECT_CONTROL,
            async_handler_name=async_name,
            metadata={'background_execution': True, 'preserve_role_contract': True,
                      'native_shell_action': 'run', 'native_shell_target': target.target},
            guidance=REMOTE_RUN_GUIDANCE.model_copy(update={
                'purpose': f'Run on configured target {target.target} ({target.name}). The target is fixed.',
            }))(sync)
        methods[async_name] = run
    provider_type = type('ConfiguredNativeExecutionProvider', (NativeExecutionProvider,), methods)
    return provider_type(runtime=runtime)
