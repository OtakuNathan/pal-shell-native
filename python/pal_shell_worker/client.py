"""Authenticated shell RPC client. Own sockets, never remote processes."""
from __future__ import annotations

import asyncio
from pathlib import Path
import socket

import _pal_shell_rpc as wire
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from . import PROTOCOL_VERSION
from .protocol import RemoteError, auth_message, decode, encode


class Connection:
    def __init__(self, path, *, client_id, private_key, worker_id, epoch=None):
        self.path = str(path)
        self.client_id, self.key, self.worker_id = client_id, private_key, worker_id
        self.epoch = epoch
        self.socket = None
        self.lock = asyncio.Lock()
        self.io_task = None
        self.closed = False

    async def connect(self):
        if self.closed:
            raise RemoteError('backend_unavailable', 'Connection has been retired')
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.setblocking(False)
        self.socket = sock
        try:
            await asyncio.wait_for(asyncio.get_running_loop().sock_connect(sock, self.path), 5)
            hello = await self._exchange('hello', {}, 5000)
            if hello.get('protocol_version') != PROTOCOL_VERSION:
                raise RemoteError('protocol_mismatch', 'Worker protocol is incompatible')
            if hello.get('worker_id') != self.worker_id:
                raise RemoteError('worker_identity_mismatch', 'Connected worker is not the configured identity')
            if self.epoch and hello.get('runtime_epoch') != self.epoch:
                raise RemoteError('runtime_changed', 'Worker Runtime changed; reconcile prior effects', effect='unknown')
            self.epoch = hello['runtime_epoch']
            signature = self.key.sign(auth_message(hello['nonce'], self.worker_id, self.epoch, self.client_id)).hex()
            await self._exchange('authenticate', {'client_id': self.client_id, 'signature': signature}, 5000)
        except BaseException:
            await self.close()
            raise
        return self

    async def _exchange(self, method, params, timeout):
        async with self.lock:
            if self.closed or self.socket is None:
                raise RemoteError('backend_unavailable', 'RPC connection is closed')
            frame = wire.pack(encode({'method': method, 'params': params}))
            task = asyncio.create_task(asyncio.to_thread(wire.exchange, self.socket.fileno(), frame, timeout))
            self.io_task = task
            try:
                raw = await asyncio.shield(task)
            except asyncio.CancelledError:
                # The native awaitable owns a duplicate fd. shutdown interrupts it;
                # do not free/replace the graph before its completion has drained.
                self.closed = True
                try:
                    self.socket.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                await asyncio.gather(task, return_exceptions=True)
                raise
            except RuntimeError as exc:
                self.closed = True
                raise RemoteError('transport_lost', 'RPC transport could not confirm the response', effect='unknown') from exc
            finally:
                self.io_task = None
            response = decode(raw)
            if not response.get('ok'):
                error = response['error']
                raise RemoteError(error['code'], error['message'], effect=error.get('effect', 'unknown'), operation_id=error.get('operation_id', ''))
            return response['result']

    async def request(self, method, params=None, *, timeout_ms=35000):
        return await self._exchange(method, {**(params or {}), 'runtime_epoch': self.epoch}, timeout_ms)

    async def close(self):
        self.closed = True
        if self.socket is not None:
            try:
                self.socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        if self.io_task:
            await asyncio.gather(self.io_task, return_exceptions=True)
        if self.socket is not None:
            self.socket.close()
            self.socket = None


def load_private_key(path):
    path = Path(path)
    import os
    if path.is_symlink() or path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
        raise ValueError('Client identity file must be private to its owner')
    return Ed25519PrivateKey.from_private_bytes(bytes.fromhex(path.read_text().strip()))
