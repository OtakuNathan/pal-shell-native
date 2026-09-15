"""Multiplexing and lifecycle against real native libuv/FF transport."""
import asyncio
import socket
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pal_shell_worker.worker import Worker, WorkerConfig
from pal_shell_worker.client import Connection
from pal_shell_worker.transport import Executor
from pal_shell_worker.protocol import RemoteError


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name)/'worker.sock'
        self.key = Ed25519PrivateKey.generate()
        self.worker = await Worker(WorkerConfig('worker','client',self.key.public_key().public_bytes_raw().hex(),self.path)).start()
        self.executor = Executor()
        self.client = await self.connect()

    async def connect(self):
        return await Connection(self.path,client_id='client',private_key=self.key,worker_id='worker',executor=self.executor).connect()

    async def asyncTearDown(self):
        await self.client.close()
        await self.worker.close()
        await self.executor.close()
        self.directory.cleanup()

    async def test_slow_request_does_not_block_fast_and_responses_match(self):
        original = self.worker.handle
        started, release = asyncio.Event(), asyncio.Event()
        async def handle(method,args):
            if method=='test_slow':
                started.set(); await release.wait(); return {'label':'slow'}
            if method=='test_fast': return {'label':'fast'}
            return await original(method,args)
        self.worker.handle=handle
        slow=asyncio.create_task(self.client.request('test_slow'))
        await started.wait()
        fast=await asyncio.wait_for(self.client.request('test_fast'),1)
        self.assertEqual(fast,{'label':'fast'}); self.assertFalse(slow.done())
        release.set(); self.assertEqual(await slow,{'label':'slow'})

    async def test_cancel_only_one_request_and_drop_late_response(self):
        started, release=asyncio.Event(),asyncio.Event()
        original=self.worker.handle
        async def handle(method,args):
            if method=='slow': started.set(); await release.wait(); return {'old':True}
            return await original(method,args)
        self.worker.handle=handle
        slow=asyncio.create_task(self.client.request('slow'))
        await started.wait(); slow.cancel()
        with self.assertRaises(asyncio.CancelledError): await slow
        release.set()
        for _ in range(3):
            self.assertEqual((await self.client.request('metadata'))['worker_id'],'worker')
        self.assertFalse(self.client.closed)

    async def test_timeout_does_not_close_shared_channel(self):
        with self.assertRaises(RemoteError) as e:
            await self.client.request('events',{'after':0,'wait_ms':100},timeout_ms=10)
        self.assertEqual(e.exception.effect,'unknown')
        self.assertEqual((await self.client.request('metadata'))['worker_id'],'worker')
        await asyncio.sleep(.12)
        self.assertEqual((await self.client.request('metadata'))['worker_id'],'worker')

    async def test_capacity_rejects_before_effect(self):
        tasks=[asyncio.create_task(self.client.request('events',{'after':0,'wait_ms':1000})) for _ in range(32)]
        await asyncio.sleep(.05)
        try:
            with self.assertRaises(RemoteError) as error:
                await self.client.request('metadata')
            self.assertEqual(error.exception.effect,'not_started')
        finally:
            for t in tasks:t.cancel()
            await asyncio.gather(*tasks,return_exceptions=True)

    async def test_disconnect_fails_pending_and_worker_survives(self):
        pending=asyncio.create_task(self.client.request('events',{'after':0,'wait_ms':300000}))
        await asyncio.sleep(.02)
        epoch=self.worker.epoch
        await self.client.close()
        with self.assertRaises(RemoteError): await pending
        self.client=await self.connect()
        self.assertEqual((await self.client.request('metadata'))['runtime_epoch'],epoch)

    async def test_multiple_connections_share_executor(self):
        second=await self.connect()
        try:
            values=await asyncio.gather(*(c.request('metadata') for c in (self.client,second)*8))
            self.assertEqual({v['worker_id'] for v in values},{'worker'})
            await second.close()
            self.assertEqual((await self.client.request('metadata'))['worker_id'],'worker')
        finally: await second.close()

    async def test_old_protocol_rejected_before_submit(self):
        from unittest.mock import patch
        with patch('pal_shell_worker.client.PROTOCOL_VERSION',1):
            with self.assertRaises(RemoteError) as error: await self.connect()
        self.assertEqual(error.exception.code,'protocol_mismatch')
        self.assertFalse(self.worker.operations)

    async def test_malformed_peer_frame_during_handshake_fails_cleanly(self):
        from pal_shell_worker.transport import Channel
        left, right = socket.socketpair()
        left.setblocking(False); right.setblocking(False)
        channel = Channel(self.executor, left)
        results = asyncio.Queue()
        channel.on_response = lambda *result: results.put_nowait(result)
        try:
            import _pal_shell_rpc as wire
            channel.request(0, wire.pack(b'hello'), 1000)
            await asyncio.get_running_loop().sock_sendall(right, b'\x00\x00\x00\x01x')
            identifier, _, error = await asyncio.wait_for(results.get(), 2)
            self.assertEqual(identifier, 0)
            self.assertIn('invalid request envelope', error)
        finally:
            await channel.close()
            right.close()

    @unittest.skipUnless(__import__('os').environ.get('PAL_TEST_IDLE_RPC') == '1', '95-second idle acceptance is opt-in')
    async def test_authenticated_connection_survives_old_idle_deadline(self):
        epoch = self.client.epoch
        await asyncio.sleep(95)
        self.assertEqual((await self.client.request('metadata'))['runtime_epoch'], epoch)
        self.assertFalse(self.client.closed)


if __name__ == '__main__':
    unittest.main()
