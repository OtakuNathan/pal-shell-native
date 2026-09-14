from __future__ import annotations

import asyncio
import hashlib
import json
import msgpack
import _pal_shell_rpc as wire

MAX_FRAME = 1024 * 1024
CHUNK_BYTES = 256 * 1024
OUTPUT_BYTES = 8 * 1024 * 1024
RETAINED_BYTES = 128 * 1024 * 1024
CACHE_BYTES = 64 * 1024 * 1024
TERMINAL = frozenset({'exited', 'cancelled', 'timed_out', 'failed'})


class RemoteError(RuntimeError):
    def __init__(self, code: str, message: str, *, effect: str = 'not_started', operation_id: str = ''):
        super().__init__(message)
        self.code, self.effect, self.operation_id = code, effect, operation_id

    def payload(self):
        return {'code': self.code, 'message': str(self), 'effect': self.effect, 'operation_id': self.operation_id}


def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode()


def digest(value) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def auth_message(nonce, worker_id, epoch, client_id) -> bytes:
    return canonical(['pal-shell-auth-v1', nonce, worker_id, epoch, client_id])


def encode(value) -> bytes:
    raw = msgpack.packb(value, use_bin_type=True)
    if len(raw) > MAX_FRAME - 128:
        raise RemoteError('frame_limit', 'RPC payload exceeds its resource limit')
    return raw


def decode(raw) -> dict:
    if len(raw) > MAX_FRAME:
        raise RemoteError('frame_limit', 'RPC payload exceeds its resource limit')
    value = msgpack.unpackb(raw, raw=False, strict_map_key=True)
    if not isinstance(value, dict):
        raise RemoteError('invalid_request', 'RPC payload must be an object')
    return value


async def read_frame(reader):
    size = int.from_bytes(await reader.readexactly(4), 'big')
    if not 0 < size <= MAX_FRAME:
        raise RemoteError('frame_limit', 'Invalid RPC frame length')
    return await reader.readexactly(size)


async def send_response(writer, request, result):
    frame = wire.respond(request, encode(result))
    if len(frame) > MAX_FRAME:
        raise RemoteError('frame_limit', 'RPC response exceeds its resource limit')
    writer.write(len(frame).to_bytes(4, 'big') + frame)
    await writer.drain()


def integer(value, name, low=0, high=2**31 - 1):
    if type(value) is not int or not low <= value <= high:
        raise RemoteError('invalid_request', f'{name} must be an integer in {low}..{high}')
    return value


def text(value, name, *, empty=False, limit=65536):
    if not isinstance(value, str) or '\0' in value or (not empty and not value) or len(value.encode()) > limit:
        raise RemoteError('invalid_request', f'Invalid {name}')
    return value
