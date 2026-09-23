from __future__ import annotations

from typing import Literal
from pydantic import Field

from pal.execution.capabilities import ExecutionIntrospectionProvider
from pal.execution.contracts import CapabilityResult
from pal.execution.tool_facade import EmptyToolInput, StrictToolModel, StructuredToolOutput, ToolGuidance, NextToolHint, ToolRejectedError
from pal.execution.tool_semantics import DIRECT_CONTROL, INDIRECT_CONTROL, INDIRECT_LOCAL_READ
from pal.shared.result_rendering import render_structured_for_llm
from pal.shared import RuntimeStatus, capability_action

from .tools import RunInput, SessionInput
from .guidance import REMOTE_RUN_GUIDANCE, RESIDENT_SESSION_GUIDANCE


class NativeRunInput(RunInput):
    target: int = Field(default=0, ge=0, strict=True, description="Execution target; 0 is local. Discover configured remote target IDs with list_remote when needed; reuse a known target. Paths belong to this target.")
    sudo: bool = False


class RemoteStartInput(StrictToolModel):
    target: int = Field(gt=0, strict=True)
    action: str = Field(min_length=1)


class RemotePowerInput(StrictToolModel):
    target: int = Field(gt=0, strict=True)
    action: Literal["shutdown"] = "shutdown"


class TargetRunInput(RunInput):
    sudo: bool = False


class ListRemoteInput(StrictToolModel):
    refresh: bool = False


NativeSessionInput = SessionInput


STATUS_GUIDANCE = ToolGuidance(
    purpose="Show current shell sessions and their last observed execution states.",
    use_when="A session ID or execution state is needed.",
    do_not_use_when="The returned result already contains the session and next operation you need.",
    failure_next_steps="Inspect exec_show and core_observe if the execution backend itself is unavailable.",
    next_tool_hints=(NextToolHint(name="shell_session", use_when="Inspect or control a listed session."),),
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
        delivery_context = {"origin_turn": turn_id, "budget": budget, "binding": getattr(continuation, "delivery_binding", None),
                            "committed": False, "cmd": call.args["cmd"], "tty": bool(call.args.get("tty"))}
        result = await owner.shell.run(**dict(call.args), turn_id=turn_id, delivery_context=delivery_context, retain_output=True, load_output=False,
                                       inline_limit=-1 if limit is None else min(limit, 2147483647))
        sid = result["session_id"]
        if sid:
            continuation = owner.core.state.active_turns.get(turn_id) if owner.core is not None else None
            owner.sessions[sid] = {
                "origin_turn": turn_id, "budget": budget,
                "binding": getattr(continuation, "delivery_binding", None),
                "committed": False, "cmd": call.args["cmd"], "tty": bool(call.args.get("tty")), "target": result.get("target", 0),
            }
        return await owner.stage(call, result)

    @capability_action(
        namespace="operation", scope="module", family="exec", action_name="session", aliases=("shell_session",),
        InputModel=NativeSessionInput, OutputModel=StructuredToolOutput, execution=INDIRECT_CONTROL,
        async_handler_name="session_async", metadata={"background_execution": True, "preserve_role_invocation_mode": True, "native_shell_action": "session"}, guidance=RESIDENT_SESSION_GUIDANCE,
    )
    def session(self, call):
        raise RuntimeError("native shell requires asynchronous execution")

    async def session_async(self, call):
        owner = call.meta["execution_runtime"].shell_owner
        args = dict(call.args)
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
            from .runtime import output_result, PendingOutput
            raw = output_result(result)
            owner.pending[call.meta["tool_call"].call_id] = PendingOutput(
                result, str(call.meta.get("turn_id") or ""), raw=raw, covers_output=False)
            return raw
        if result["status"] == "released":
            owner.forget_session(result["session_id"])
            return self._result({"session_id": result["session_id"], "status": "released"})
        return await owner.stage(call, result)

    @capability_action(
        namespace="operation", scope="module", family="exec", action_name="status", aliases=("shell_status",),
        InputModel=EmptyToolInput, OutputModel=StructuredToolOutput, execution=INDIRECT_LOCAL_READ,
        guidance=STATUS_GUIDANCE, async_handler_name="shell_status_async", metadata={"background_execution": True, "preserve_role_invocation_mode": True, "native_shell_action": "status"},
    )
    def shell_status(self, call):
        owner = call.meta["execution_runtime"].shell_owner
        return self._result({"sessions": [
            {"session_id": sid, "cmd": item["cmd"], "watching": item.get("watching", True),
             "last_observed_status": item.get("latest_status", "running"), "target": item.get("target", 0)}
            for sid, item in owner.sessions.items()]})

    async def shell_status_async(self, call):
        owner = call.meta['execution_runtime'].shell_owner
        payload = self.shell_status(call).structured
        targets = [{'target': 0, 'execution': {'blocked': owner.completion_blocked_for(0)}}]
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
        async_handler_name="list_remote_async", metadata={"background_execution": True, "native_shell_action": "list_remote"}, guidance=ToolGuidance(
            purpose="List legal execution targets with configured facts and timestamped observed resources.",
            use_when="Choose a machine using OS, CPU architecture, shell and available compute; refresh probes without waking.",
            do_not_use_when="A returned session already fixes its target.",
            failure_next_steps="Offline entries remain valid targets; use only their configured explicit start actions.",
        ),
    )
    def list_remote(self, call):
        raise RuntimeError("Use asynchronous target discovery")

    async def list_remote_async(self, call):
        owner = call.meta["execution_runtime"].shell_owner
        import os
        import platform
        items = [{"target": 0, "name": "local", "requires_wake": False, "registered": True,
                  "os": platform.system(), "arch": platform.machine(), "logical_cpus": os.cpu_count(),
                  "shell": {"executable": "/bin/bash", "invocation": ["-lc"]}}]
        if owner.remote_port is not None:
            items.extend(await owner.remote_port.list(call.args.get("refresh", False)))
        return self._result({"targets": items, "remote_attached": owner.remote_port is not None})

    @capability_action(
        namespace="operation", scope="module", family="exec", action_name="remote_start", aliases=("remote_start",),
        InputModel=RemoteStartInput, OutputModel=StructuredToolOutput, execution=INDIRECT_CONTROL,
        async_handler_name="remote_start_async", metadata={"background_execution": True, "native_shell_action": "remote_start"}, guidance=ToolGuidance(
            purpose="Explicitly invoke one preconfigured target startup action.",
            use_when="list_remote reports a configured wake or user-service start action that is needed.",
            do_not_use_when="A target is already available; this is not command replay or worker restart.",
            failure_next_steps="Refresh target metadata to verify readiness; action completion alone does not prove readiness.",
        ),
    )
    def remote_start(self, call):
        raise RuntimeError("Use asynchronous remote management")

    async def remote_start_async(self, call):
        owner = call.meta["execution_runtime"].shell_owner
        return self._result(await owner.shell._port().call('start', dict(call.args)))

    @capability_action(
        namespace="operation", scope="module", family="exec", action_name="remote_power", aliases=("remote_power",),
        InputModel=RemotePowerInput, OutputModel=StructuredToolOutput, execution=INDIRECT_CONTROL,
        async_handler_name="remote_power_async", metadata={"background_execution": True, "native_shell_action": "remote_power"}, guidance=ToolGuidance(
            purpose="Request target shutdown after a single human approval and atomic worker busy check.",
            use_when="list_remote reports management.shutdown.supported=true and the target owner authorizes shutdown.",
            do_not_use_when="Only disconnecting the plugin or stopping one command is intended; never shut down this Pal host.",
            failure_next_steps="Accepted does not prove power-off. An unknown outcome must not be treated as success; busy rejects without scheduling later shutdown.",
        ),
    )
    def remote_power(self, call):
        raise RuntimeError("Use asynchronous remote management")

    async def remote_power_async(self, call):
        owner = call.meta["execution_runtime"].shell_owner
        return self._result(await owner.shell.privileged(call.args['target'], 'shutdown', turn_id=str(call.meta.get('turn_id') or '')))

    @staticmethod
    def _result(payload):
        text = render_structured_for_llm(payload)
        return CapabilityResult(status=RuntimeStatus.OK, structured=payload, text=text, llm_text=text)


def build_provider(runtime):
    """Compile configured projections without extending Pal's alias semantics."""
    import tomllib
    from pathlib import Path
    from dataclasses import replace
    from pal_shell_remote.slot import Target
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
