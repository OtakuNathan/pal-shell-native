"""Check the host's native dependency without installing or activating anything."""


def verify(context):
    try:
        import _pal_shell_runtime as native
        import _pal_shell_rpc as rpc
        from pal_shell_worker import PROTOCOL_VERSION
        from pal.execution.native_shell.remote_contract import RemoteFailure
        if PROTOCOL_VERSION != 2 or not hasattr(native.Runtime, 'run_limited') or not hasattr(rpc, 'channel_request'):
            raise RuntimeError('Incompatible native Runtime/RPC client')
    except (ImportError, RuntimeError) as exc:
        return {'ok': False, 'detail': f'Install matching pal-shell-native >=0.3 and Pal native target support in the host environment: {exc}'}
    return {'ok': True, 'protocol': PROTOCOL_VERSION}
