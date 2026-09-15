"""Bounded, cancellable admission before a request touches the transport."""
import asyncio
from collections import deque
from contextlib import asynccontextmanager

from pal_shell_worker.protocol import RemoteError


class RequestAdmission:
    def __init__(self, capacity=32, queue_limit=128, timeout=30):
        self.capacity = capacity
        self.queue_limit = queue_limit
        self.timeout = timeout
        self.active = 0
        self.waiters = deque()
        self.closed = False

    async def acquire(self):
        if self.closed:
            raise RemoteError('backend_unavailable', 'Target admission is closed')
        if self.active < self.capacity and not self.waiters:
            self.active += 1
            return
        if len(self.waiters) >= self.queue_limit:
            raise RemoteError('transport_capacity', 'Target request queue is full; no request was sent')
        future = asyncio.get_running_loop().create_future()
        self.waiters.append(future)
        try:
            await asyncio.wait_for(future, self.timeout)
        except BaseException as exc:
            # Cancellation may race with a release that already granted this
            # future its permit. Return that permit instead of leaking capacity.
            if future.done() and not future.cancelled() and future.exception() is None:
                self.release()
            else:
                try:
                    self.waiters.remove(future)
                except ValueError:
                    pass
            if isinstance(exc, TimeoutError):
                raise RemoteError('transport_queue_timeout',
                    'Target request queue wait expired; no request was sent') from exc
            raise

    def release(self):
        self.active -= 1
        while self.waiters and not self.closed:
            future = self.waiters.popleft()
            if not future.done():
                self.active += 1
                future.set_result(None)
                break

    def close(self):
        self.closed = True
        while self.waiters:
            future = self.waiters.popleft()
            if not future.done():
                future.set_exception(RemoteError('backend_unavailable',
                    'Target detached while queued; no request was sent'))

    @asynccontextmanager
    async def permit(self):
        await self.acquire()
        try:
            yield
        finally:
            self.release()
