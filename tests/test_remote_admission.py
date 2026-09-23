"""Slot queue acceptance through real RPC, without SSH or privileged effects."""
import asyncio
from pathlib import Path
import tempfile
import unittest

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pal_shell_remote.admission import RequestAdmission
from pal_shell_remote.hub import RemoteHub
from pal_shell_remote.slot import Target
from pal_shell_worker.protocol import RemoteError
from pal_shell_worker.worker import Worker, WorkerConfig


class AdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_waiters_are_admitted_in_fifo_order(self):
        gate = RequestAdmission(capacity=1)
        await gate.acquire()
        order = []
        async def wait(number):
            async with gate.permit():
                order.append(number)
                await asyncio.sleep(0)
        waiters = [asyncio.create_task(wait(number)) for number in range(8)]
        await asyncio.sleep(0)
        self.assertEqual(order, [])
        gate.release()
        await asyncio.gather(*waiters)
        self.assertEqual(order, list(range(8)))
        self.assertEqual(gate.active, 0)

    async def test_cancel_after_grant_returns_permit(self):
        gate = RequestAdmission(capacity=1)
        await gate.acquire()
        waiter = asyncio.create_task(gate.acquire())
        await asyncio.sleep(0)
        gate.release()
        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        self.assertEqual(gate.active, 0)
        self.assertFalse(gate.waiters)
        await gate.acquire()
        gate.release()


class SlotQueueTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        key = Ed25519PrivateKey.generate()
        identity = root/'key'
        identity.write_text(key.private_bytes_raw().hex())
        identity.chmod(0o600)
        self.worker = await Worker(WorkerConfig('worker','pal',
            key.public_key().public_bytes_raw().hex(),root/'worker.sock')).start()
        self.hub = RemoteHub([Target(1,'test','worker','pal',str(identity),str(root/'worker.sock'))])
        self.slot = self.hub.slots[1]
        self.tasks = []
        self.started = []
        self.releases = [asyncio.Event() for _ in range(32)]
        original = self.worker.handle
        async def handle(method, args):
            if method == 'hold':
                self.started.append(args['number'])
                await self.releases[args['number']].wait()
                return {'number':args['number']}
            return await original(method, args)
        self.worker.handle = handle

    async def asyncTearDown(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.hub.close()
        await self.worker.close()
        self.directory.cleanup()

    def task(self, method, args):
        task = asyncio.create_task(self.slot.request(method, args))
        self.tasks.append(task)
        return task

    async def saturated(self):
        for number in range(32):
            self.task('hold', {'number':number})
        async with asyncio.timeout(3):
            while len(self.started) != 32:
                await asyncio.sleep(.001)

    async def queued(self, count=1):
        async with asyncio.timeout(3):
            while len(self.slot.admission.waiters) != count:
                await asyncio.sleep(.001)

    async def test_33rd_request_waits_and_executes_once_after_capacity_returns(self):
        await self.saturated()
        queued = self.task('submit', {'operation_id':'queued','cmd':'printf queued','wait_ms':0})
        await self.queued()
        self.assertNotIn('queued', self.worker.operations)
        self.assertFalse(queued.done())
        self.releases[0].set()
        await asyncio.wait_for(queued, 3)
        self.assertIn('queued', self.worker.operations)
        self.assertEqual(len(self.started), 32)

    async def test_cancelled_waiter_is_never_sent(self):
        await self.saturated()
        queued = self.task('submit', {'operation_id':'cancelled','cmd':'printf cancelled','wait_ms':0})
        await self.queued()
        queued.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await queued
        self.releases[0].set()
        self.assertEqual((await self.slot.request('metadata', {}))['worker_id'], 'worker')
        self.assertNotIn('cancelled', self.worker.operations)
        self.assertFalse(self.slot.execution.operations)

    async def test_timeout_and_queue_limit_are_known_not_started(self):
        await self.saturated()
        self.slot.admission.timeout = .1
        self.slot.admission.queue_limit = 1
        queued = self.task('submit', {'operation_id':'expired','cmd':'printf expired','wait_ms':0})
        await self.queued()
        reply = await self.hub.call('request', {'target':1,'method':'metadata',
            'params':{}})
        self.assertEqual(reply['error']['code'], 'transport_capacity')
        self.assertEqual(reply['error']['effect'], 'not_started')
        with self.assertRaises(RemoteError) as error:
            await queued
        self.assertEqual(error.exception.code, 'transport_queue_timeout')
        self.assertEqual(error.exception.effect, 'not_started')
        self.assertFalse(self.worker.operations)

    async def test_detach_wakes_queued_and_inflight_calls(self):
        await self.saturated()
        queued = self.task('submit', {'operation_id':'detached','cmd':'printf detached','wait_ms':0})
        await self.queued()
        await asyncio.wait_for(self.slot.close(), 3)
        with self.assertRaises(RemoteError) as error:
            await queued
        self.assertEqual(error.exception.effect, 'not_started')
        self.assertEqual(self.slot.admission.active, 0)
        self.assertNotIn('detached', self.worker.operations)
        self.assertFalse(self.worker.closed)


if __name__ == '__main__':
    unittest.main()
