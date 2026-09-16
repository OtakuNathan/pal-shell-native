"""Check the host's native dependency without installing or activating anything."""


def verify(context):
    try:
        import _pal_shell_runtime as native
        import _pal_shell_rpc as rpc
        from pal_shell_worker import PROTOCOL_VERSION
        from pal.execution.extensions import ExecutionSlot
        from pal.core import PalCore  # Initialize the host import boundary before execution.
        from pal.execution.runtime import ExecutionRuntime
        from pal.memory.service import MemoryService
        if (not callable(getattr(ExecutionRuntime, "prepare_model_context", None))
            or not callable(getattr(MemoryService, "l1_context_view", None))):
            raise RuntimeError("Pal lacks indexed request visibility and observation delivery support")
        if PROTOCOL_VERSION != 3 or native.API_VERSION != 2 or not hasattr(native.Runtime, 'watch') or not hasattr(rpc, 'channel_request'):
            raise RuntimeError('Incompatible native Runtime/RPC client')
    except (ImportError, RuntimeError) as exc:
        return {'ok': False, 'detail': f'Install pal-shell-native 0.4.x and Pal execution extension support in the host environment: {exc}'}
    return {'ok': True, 'protocol': PROTOCOL_VERSION}
