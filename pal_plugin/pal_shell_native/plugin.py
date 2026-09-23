"""The optional native plugin owns local execution, sessions and remote routing."""
from __future__ import annotations

from pal.core.module_registry import ModuleHandle, MODULE_TIER_DETACHABLE
from .runtime import NativeExecutionRuntime


class NativeExtension:
    def __init__(self, runtime_root, *, resident=True):
        self.runtime_root = runtime_root
        self.resident = resident
        self.remote = None

    def build_runtime(self, previous):
        from pal.execution.runtime import ExecutionRuntime
        from pal.memory.service import MemoryService
        if not callable(getattr(MemoryService, 'l1_context_view', None)) or not callable(getattr(ExecutionRuntime, 'prepare_model_context', None)):
            raise RuntimeError('Update Pal to the matching observation delivery contract before attaching Native')
        return NativeExecutionRuntime(runtime_root=previous.runtime_root,
                                      sync_executor=previous.sync_executor,
                                      logical_state=previous.logical_state,
                                      tool_result_pager=previous.tool_result_pager)

    def build_provider(self, runtime):
        return runtime.build_introspection_provider()

    def build_state_port(self, runtime):
        return runtime.build_runtime_state_port()

    def activate(self, context, handle, runtime):
        owner = runtime.shell_owner
        if self.resident:
            from pal_shell_remote.plugin import RemoteHubClient
            self.remote = RemoteHubClient(self.runtime_root, owner)
            handle.ports['remote'] = self.remote.start()
            core = context.port_registry.get('core:core')
            if core is not None:
                from .events import ShellCompletionSource, EVENT, WAIT_EVENT
                owner.core = core
                owner.events = ShellCompletionSource(core, runtime)
                handle.event_sources.append(owner.events)
                handle.event_handlers.update({EVENT: [owner.events], WAIT_EVENT: [owner.events]})
            handle.control_action_handlers['shell_privilege_decision'] = owner.approvals.decide

    def check_detach(self, runtime):
        runtime.shell_owner.check_idle()

    def close(self, runtime):
        runtime.shell_owner.close_idle()
        if self.remote:
            self.remote.close()
            self.remote = None


class NativePlugin:
    plugin_id = 'remote'  # Retain the installed package identity across the 0.4 upgrade.
    version = '0.4.1'

    def __init__(self, runtime_root):
        self.runtime_root = runtime_root

    def start(self, scope):
        from .skills import NativePluginProvider
        return ModuleHandle(module_id='remote', tier=MODULE_TIER_DETACHABLE, detachable=True,
                            introspection_provider=NativePluginProvider(),
                            execution_extension=NativeExtension(self.runtime_root))


def build_plugin(context):
    return NativePlugin(context.runtime_root)


def activate_role(context):
    handle = ModuleHandle(module_id='execution_extension', tier=MODULE_TIER_DETACHABLE,
                          execution_extension=NativeExtension(context.execution_runtime.runtime_root, resident=False))
    context.register_module(handle)
    context.execution_runtime.install(handle.execution_extension, context, handle)
    # The role's execution module owns shutdown, without resident-side remote access.
