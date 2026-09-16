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
    def __init__(self, path, *, client_id, private_key, worker_id, epoch=None, executor=None):
        from .transport import Executor
        self.path = str(path)
        self.client_id, self.key, self.worker_id = client_id, private_key, worker_id
        self.epoch = epoch
        self.executor = executor or Executor()
        self.owns_executor = executor is None
        self.channel = None
        self.socket = None
        self.reader = None
        self.closed = False
        self.pending = {}
        self.sequence = 0

    async def connect(self):
        from .transport import Channel
        if self.closed:
            raise RemoteError('backend_unavailable', 'Connection has been retired')
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.setblocking(False)
        self.socket = sock
        try:
            await asyncio.wait_for(asyncio.get_running_loop().sock_connect(sock, self.path), 5)
            self.channel = Channel(self.executor, sock)
            self.channel.on_response = self._response
            self.reader = asyncio.create_task(self._receive())
            hello = await self._exchange('hello', {}, 5000, legacy=True)
            if hello.get('protocol_version') != PROTOCOL_VERSION:
                raise RemoteError('protocol_mismatch', 'Install matching protocol-v3 worker and client')
            if hello.get('worker_id') != self.worker_id:
                raise RemoteError('worker_identity_mismatch', 'Connected worker is not the configured identity')
            if self.epoch and hello.get('runtime_epoch') != self.epoch:
                raise RemoteError('runtime_changed', 'Worker Runtime changed; reconcile prior effects', effect='unknown')
            self.epoch = hello['runtime_epoch']
            signature = self.key.sign(auth_message(hello['nonce'], self.worker_id, self.epoch, self.client_id)).hex()
            await self._exchange('authenticate', {'client_id': self.client_id, 'signature': signature}, 5000, legacy=True)
        except BaseException:
            await self.close()
            raise
        return self

    def _response(self, identifier, frame, error):
        future = self.pending.get(identifier)
        if future is None or future.done():
            return
        if error:
            effect = 'not_started' if 'before send' in error else 'unknown'
            future.set_exception(RemoteError('transport_timeout' if 'timed out' in error else 'transport_lost', error, effect=effect))
            return
        try:
            future.set_result(decode(wire.response_payload(frame)))
        except (RuntimeError, ValueError) as exc:
            future.set_exception(RemoteError('invalid_response', str(exc), effect='unknown'))

    async def _receive(self):
        failure = RemoteError('transport_lost', 'RPC connection closed before confirmation', effect='unknown')
        try:
            while True:
                identifier, frame = await self.channel.receive()
                future = self.pending.get(identifier)
                if future is None:  # Timed-out/cancelled response, never reassign it.
                    if identifier > self.sequence:
                        raise RemoteError('invalid_response', 'Unexpected RPC request ID', effect='unknown')
                    continue
                if future.done():
                    raise RemoteError('invalid_response', 'Duplicate RPC response', effect='unknown')
                future.set_result(decode(wire.response_payload(frame)))
        except RemoteError as exc:
            failure = exc
        except (RuntimeError, ValueError) as exc:
            failure = RemoteError('invalid_response', str(exc), effect='unknown')
        finally:
            self.closed = True
            for future in tuple(self.pending.values()):
                if not future.done():
                    future.set_exception(failure)

    async def _exchange(self, method, params, timeout, *, legacy=False):
        if self.closed or self.channel is None or self.channel.closed:
            raise RemoteError('backend_unavailable', 'RPC connection is closed')
        if len(self.pending) >= 32:
            raise RemoteError('transport_capacity', 'RPC in-flight request limit reached')
        self.sequence += 1
        identifier = 0 if legacy else self.sequence
        future = asyncio.get_running_loop().create_future()
        self.pending[identifier] = future
        try:
            self.channel.request(identifier, wire.pack(encode({'method': method, 'params': params})), timeout)
            try:
                response = await future
            except asyncio.CancelledError:
                self.channel.cancel(identifier)
                raise
            if not response.get('ok'):
                error = response['error']
                raise RemoteError(error['code'], error['message'], effect=error.get('effect', 'unknown'), operation_id=error.get('operation_id', ''))
            return response['result']
        finally:
            self.pending.pop(identifier, None)
            if not future.done():
                future.cancel()

    async def request(self, method, params=None, *, timeout_ms=35000):
        return await self._exchange(method, {**(params or {}), 'runtime_epoch': self.epoch}, timeout_ms)

    async def close(self):
        self.closed = True
        if self.channel:
            await self.channel.close()
            self.channel = None
        elif self.socket:
            self.socket.close()
        if self.reader:
            await asyncio.gather(self.reader, return_exceptions=True)
            self.reader = None
        if self.owns_executor:
            await self.executor.close()


def load_private_key(path):
    path = Path(path)
    import os
    if path.is_symlink() or path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
        raise ValueError('Client identity file must be private to its owner')
    return Ed25519PrivateKey.from_private_bytes(bytes.fromhex(path.read_text().strip()))
