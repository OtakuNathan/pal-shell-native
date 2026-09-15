"""Bounded RPC envelopes over a caller-owned native libuv loop."""
import asyncio
import struct
import socket
import _pal_shell_rpc as wire
from .protocol import RemoteError, MAX_FRAME


class Executor:
    def __init__(self):
        self.native = wire.loop_new()
        self.closed = False

    async def close(self):
        if not self.closed:
            self.closed = True
            await asyncio.to_thread(wire.loop_close, self.native)


class Channel:
    def __init__(self, executor, sock, *, accepted=None):
        self.loop = asyncio.get_running_loop()
        self.executor, self.socket = executor, sock
        if hasattr(socket, "SO_NOSIGPIPE"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_NOSIGPIPE, 1)
        self.frames = asyncio.Queue(33)
        self.closed = False
        self.error = ''
        self.on_response = None
        self.accepted = accepted
        self.stopped = self.loop.create_future()
        self.native = wire.channel_open(executor.native, sock.fileno(), self._event, accepted is not None)

    def _event(self, kind, data, error):
        self.loop.call_soon_threadsafe(self._deliver, kind, data, error)

    def _deliver(self, kind, data, error):
        if kind == 'accepted':
            sock = socket.socket(fileno=struct.unpack('!Q', data)[0])
            sock.setblocking(False)
            if self.closed or self.executor.closed:
                sock.close()
            else:
                self.accepted(sock)
        elif kind == 'closed':
            self.closed = True
            self.error = error
            if not self.stopped.done():
                self.stopped.set_result(None)
            if not self.frames.full():
                self.frames.put_nowait(None)
        elif kind == 'response':
            if self.on_response:
                self.on_response(struct.unpack('!Q', data[:8])[0], data[8:], error)
        elif not self.closed:
            if self.frames.full():
                wire.channel_close(self.native)
            else:
                self.frames.put_nowait(data)

    def request(self, identifier, payload, timeout):
        if self.closed or self.executor.closed:
            raise RemoteError('transport_closed', 'RPC channel is closed')
        try:
            wire.channel_request(self.native, identifier, payload, timeout)
        except RuntimeError as exc:
            raise RemoteError('transport_capacity', str(exc)) from exc

    def cancel(self, identifier):
        if not self.closed:
            wire.channel_cancel(self.native, identifier)

    def send(self, request_id, payload):
        if self.closed or self.executor.closed:
            raise RemoteError('transport_closed', 'RPC channel is closed')
        frame = struct.pack('!Q', request_id) + payload if request_id else payload
        if len(frame) > MAX_FRAME:
            raise RemoteError('frame_limit', 'RPC envelope exceeds frame limit')
        try:
            wire.channel_send(self.native, frame)
        except RuntimeError as exc:
            raise RemoteError('transport_capacity', str(exc)) from exc

    async def receive(self):
        if self.closed and self.frames.empty():
            raise RemoteError('transport_lost', self.error, effect='unknown')
        frame = await self.frames.get()
        if frame is None:
            raise RemoteError('transport_lost', self.error, effect='unknown')
        # Only hello/authentication and the Mac askpass one-shot use legacy frames.
        if frame[:4] in (b'DRPC', b'DRPR'):
            return 0, frame
        if len(frame) < 9:
            raise RemoteError('invalid_frame', 'RPC request ID is missing', effect='unknown')
        identifier = struct.unpack('!Q', frame[:8])[0]
        if not identifier:
            raise RemoteError('invalid_frame', 'RPC request ID must be nonzero', effect='unknown')
        return identifier, frame[8:]

    async def close(self):
        if not self.closed:
            wire.channel_close(self.native)
        await asyncio.shield(self.stopped)
        self.socket.close()
        self.native = None


class Listener:
    """Accept and connected socket I/O share the native RPC executor."""
    def __init__(self, executor, sock, connected):
        self.sockets = [sock]
        self.closing = False
        self.task = None
        def accepted(client):
            if self.closing:
                client.close()
            else:
                connected(client)
        self.channel = Channel(executor, sock, accepted=accepted)

    def close(self):
        if not self.closing:
            self.closing = True
            self.task = asyncio.create_task(self.channel.close())

    async def wait_closed(self):
        if self.task:
            await self.task
