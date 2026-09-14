"""One optional sidecar with a long-lived loop, containing all configured slots."""
from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path
import signal
import tomllib

from pal.foundation.sidecar import (SidecarEndpoint, start_sidecar_server, cleanup_sidecar_endpoint,
                                    handle_sidecar_client, dispatch_sidecar_request)
from pal_shell_worker.protocol import RemoteError
from .slot import RemoteSlot, Target


class RemoteHub:
    def __init__(self, targets):
        self.slots = {}
        if sum(t.shortcut == 'desktop' for t in targets) > 1:
            raise ValueError('Only one target may bind the desktop shortcut')
        for config in targets:
            if config.target in self.slots:
                raise ValueError('Duplicate remote target')
            self.slots[config.target] = RemoteSlot(config)

    async def call(self, method, params):
        try:
            if method == 'health':
                result = {'pid': os.getpid(), 'protocol': 1}
            elif method == 'identity':
                from pal_shell_worker.metadata import machine_identity
                result = {'machine_identity': machine_identity()}
            elif method == 'list':
                result = {'targets': await asyncio.gather(*(s.describe(params.get('refresh', False)) for s in self.slots.values()))}
            else:
                slot = self.slots.get(params.get('target'))
                if slot is None:
                    raise RemoteError('invalid_target', 'Target is not configured')
                if method == 'start':
                    result = await slot.start(params.get('action'))
                elif method == 'approve':
                    from pal_shell_worker.client import load_private_key
                    from pal_shell_worker.protocol import canonical
                    grant = params['approval']
                    c = slot.config
                    if (grant['client_id'] != c.client_id or grant['worker_id'] != c.worker_id or
                        grant['target'] != c.target or grant['runtime_epoch'] != slot.epoch):
                        raise RemoteError('approval_invalid', 'Approval identity changed')
                    signature = load_private_key(c.client_key).sign(canonical(grant)).hex()
                    result = await slot.request('commit_privileged', {'operation_id': grant['operation_id'], 'signature': signature}, grant['runtime_epoch'])
                    if params.get('action') == 'shutdown':
                        if result.get('state') == 'pending':
                            result = await slot.request('query', {'operation_id': grant['operation_id'], 'wait_ms': 1000}, grant['runtime_epoch'])
                        if (result.get('result') or {}).get('status') == 'accepted':
                            slot.expected_offline = True
                elif method == 'request':
                    result = await slot.request(params['method'], params.get('params', {}), params.get('runtime_epoch'))
                else:
                    raise RemoteError('unknown_method', 'Unknown hub method')
            return {'result': result}
        except RemoteError as exc:
            return {'error': exc.payload()}

    async def close(self):
        await asyncio.gather(*(slot.close() for slot in self.slots.values()))


async def serve(config, directory):
    targets = [Target(**item) for item in tomllib.loads(config.read_text()).get('targets', [])]
    hub = RemoteHub(targets)
    endpoint = SidecarEndpoint(directory, 'remote', runtime_dir_override=directory)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stop.set)
    async def dispatch(request):
        return await dispatch_sidecar_request(request, hub.call)
    clients = set()
    async def connected(reader, writer):
        task = asyncio.current_task()
        clients.add(task)
        try:
            await handle_sidecar_client(reader, writer, dispatch)
        finally:
            clients.discard(task)
    server, transport = await start_sidecar_server(endpoint, connected)
    if transport['transport'] != 'unix':
        server.close()
        await server.wait_closed()
        await cleanup_sidecar_endpoint(endpoint)
        raise RuntimeError('Remote hub requires a private Unix endpoint')
    try:
        await stop.wait()
    finally:
        server.close()
        for task in list(clients):
            task.cancel()
        await asyncio.gather(*clients, return_exceptions=True)
        await server.wait_closed()
        await hub.close()
        await cleanup_sidecar_endpoint(endpoint)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--directory', required=True, type=Path)
    args = parser.parse_args()
    asyncio.run(serve(args.config, args.directory))


if __name__ == '__main__':
    main()
