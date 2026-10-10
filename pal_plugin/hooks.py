"""Check the host's native dependency without installing or activating anything."""


def verify(context):
    try:
        import _pal_shell_runtime as native
        import _pal_shell_rpc as rpc
        from pal_shell_worker import PROTOCOL_VERSION
        from pal.execution.extensions import ExecutionSlot
        from pal.core import PalCore  # Initialize the host import boundary before execution.
        from pal.execution.runtime import ExecutionRuntime
        from pal.execution.tool_facade import ToolGuidance
        from pal.memory.service import MemoryService
        from pal.shared.diagnostics import diagnostic_text, exception_diagnostic
        from pal.bunshin.verification_builder import SHELL_EVIDENCE_CAPABILITIES
        if not callable(diagnostic_text) or not callable(exception_diagnostic) or not SHELL_EVIDENCE_CAPABILITIES:
            raise RuntimeError('Pal lacks diagnostic helpers or Bunshin shell verification support')
        if 'search_enum_fields' not in ToolGuidance.model_fields:
            raise RuntimeError('Pal lacks enum-field tool discovery support')
        if (not callable(getattr(ExecutionRuntime, "prepare_model_context", None))
            or not callable(getattr(MemoryService, "l1_context_view", None))):
            raise RuntimeError("Pal lacks indexed request visibility and observation delivery support")
        if PROTOCOL_VERSION != 3 or native.API_VERSION != 2 or not hasattr(native.Runtime, 'watch') or not hasattr(rpc, 'channel_request'):
            raise RuntimeError('Incompatible native Runtime/RPC client')
    except (ImportError, RuntimeError) as exc:
        return {'ok': False, 'detail': f'Install pal-shell-native 0.4.x and Pal baseline 9d8cbe906906522e1f4a0d2a50a709385a4209f3 or a compatible later revision (execution extensions, diagnostics, Bunshin verification and enum-field tool discovery) in the host environment: {exc}'}
    return {'ok': True, 'protocol': PROTOCOL_VERSION}
