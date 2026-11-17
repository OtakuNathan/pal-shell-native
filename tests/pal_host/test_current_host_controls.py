"""Exercise native's actual control contribution against the current dispatcher."""
import asyncio
from unittest.mock import AsyncMock, patch

from pal.control.contracts import ControlAction
from pal.control.runtime_contracts import CORE_CONTROL
from pal.core.main_context import MainContext
from pal.core.module_registry import ModuleHandle
from pal.core.runtime import PalCore
from pal_shell_native.plugin import NativeExtension
from pal_shell_native.runtime import NativeExecutionRuntime


def test_native_approval_dispatch_delivers_reply_through_core(tmp_path):
    async def run():
        runtime = NativeExecutionRuntime(runtime_root=tmp_path)
        core = PalCore(context=MainContext(execution_runtime=runtime))
        core.context.port_registry.register(CORE_CONTROL, core.control_runtime)
        handle = ModuleHandle('remote', 'detachable')
        extension = NativeExtension(tmp_path)
        reply = AsyncMock()
        try:
            with patch('pal_shell_remote.plugin.RemoteHubClient') as remote, patch.object(core.control_runtime, 'reply', reply):
                remote.return_value.start.return_value = object()
                extension.activate(core.context, handle, runtime)
                core.context.register_module(handle)
                core.context.publish_module_control(handle)
                action = ControlAction('shell_privilege_decision', 'remote', 'expired')
                assert await core.context.control_action_registry.handle(action)
                reply.assert_awaited_once_with(action, 'Approval is unavailable or this actor/route is not its owner.')
        finally:
            core.context.unregister_module(handle)
            await runtime.shutdown_async()
            core.close()
    asyncio.run(run())
