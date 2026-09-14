"""Detached-value port; no resident shell object depends on plugin implementation."""
from pal.foundation.sidecar import SidecarRpcClient
from pal.execution.native_shell.remote_contract import RemoteFailure


class HubPort:
    def __init__(self, endpoint):
        self.client = SidecarRpcClient(endpoint, request_timeout_seconds=315, unix_only=True) if endpoint else None
        self.closed = False

    async def call(self, method, params):
        if self.closed:
            raise RemoteFailure('backend_unavailable', 'Remote plugin is detached')
        if self.client is None:
            if method == 'list':
                return {'targets': []}
            raise RemoteFailure('invalid_target', 'No remote targets configured; add config/remote.toml and reload remote')
        try:
            response = await self.client.request(method, params)
        except Exception as exc:
            raise RemoteFailure('hub_unavailable', 'Hub could not confirm the response', effect='unknown') from exc
        if self.closed:
            raise RemoteFailure('backend_unavailable', 'Remote plugin generation retired during request', effect='unknown')
        if 'error' in response:
            error = response['error']
            raise RemoteFailure(error['code'], error['message'], effect=error.get('effect', 'unknown'))
        return response['result']

    async def request(self, target, method, params, epoch=None):
        return await self.call('request', {'target': target, 'method': method, 'params': params, 'runtime_epoch': epoch})

    async def list(self, refresh=False):
        return (await self.call('list', {'refresh': refresh}))['targets']
