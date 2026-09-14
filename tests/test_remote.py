"""Real native/RPC service acceptance without Pal, SSH, or privileged actions."""
import asyncio
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from pal_shell_worker.client import Connection
from pal_shell_worker.worker import Worker, WorkerConfig
from pal_shell_worker.protocol import RemoteError


class RemoteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name)
        self.key = Ed25519PrivateKey.generate()
        self.worker = Worker(WorkerConfig('test-worker', 'test-client', self.key.public_key().public_bytes_raw().hex(), self.path/'worker.sock', output_limit=4096, retained_limit=8192))
        await self.worker.start()
        self.clients = []
        self.client = await self.connect()

    async def connect(self, **overrides):
        args = dict(client_id='test-client', private_key=self.key, worker_id='test-worker')
        args.update(overrides)
        c = Connection(self.path/'worker.sock', **args)
        self.clients.append(c)
        return await c.connect()

    async def asyncTearDown(self):
        for c in self.clients:
            await c.close()
        await self.worker.close()
        self.directory.cleanup()

    async def submit(self, cmd, wait=0, tty=False):
        oid = uuid4().hex
        await self.client.request('submit', {'operation_id': oid, 'cmd': cmd, 'wait_ms': wait, 'tty': tty})
        return oid

    async def query(self, oid, client=None):
        return await (client or self.client).request('query', {'operation_id': oid, 'wait_ms': 5000}, timeout_ms=6000)

    async def test_disconnect_and_deduplicate(self):
        file = self.path/'count'
        args = {'operation_id': uuid4().hex, 'cmd': f'echo x >> {file}; sleep 0.1; printf done', 'wait_ms': 1000}
        await self.client.request('submit', args)
        await self.client.close()
        other = await self.connect(epoch=self.worker.epoch)
        await other.request('submit', args)
        result = await self.query(args['operation_id'], other)
        self.assertEqual(result['result']['status'], 'exited')
        self.assertEqual(file.read_text(), 'x\n')
        chunk = await other.request('output', {'snapshot': result['result']['snapshot'], 'stream': 'stdout'})
        self.assertEqual(chunk['data'], b'done')
        with self.assertRaises(RemoteError) as error:
            await other.request('submit', {**args, 'cmd': 'false'})
        self.assertEqual(error.exception.code, 'operation_conflict')

    async def test_pty_and_completion(self):
        oid = await self.submit('read x; printf "got:%s" "$x"', tty=True)
        result = await self.query(oid)
        sid = result['result']['session_id']
        write = uuid4().hex
        await self.client.request('session', {'operation_id': write, 'session_id': sid, 'action': 'write', 'text': 'hello\n'})
        await self.query(write)
        control = uuid4().hex
        await self.client.request('session', {'operation_id': control, 'session_id': sid, 'action': 'read', 'wait_ms': 5000})
        terminal = (await self.query(control))['result']
        self.assertEqual(terminal['status'], 'exited')
        output = await self.client.request('output', {'snapshot': terminal['snapshot'], 'stream': 'stdout'})
        self.assertIn(b'got:hello', output['data'])

    async def test_output_quota_and_release_retry(self):
        oid = await self.submit('yes abcdef', 1000)
        result = (await self.query(oid))['result']
        self.assertEqual(result['status'], 'failed')
        self.assertTrue(result['truncated'])
        self.assertLessEqual(result['stdout_total'] + result['stderr_total'], 4096)
        self.assertEqual(Path(self.worker.outputs[result['output_id']]['event']['stdout_path']).stat().st_size, 4096)
        await self.client.request('release', {'output_id': result['output_id']})
        await self.client.request('release', {'output_id': result['output_id']})
        self.assertFalse(self.worker.reservations)

    async def test_wrong_identity_and_epoch(self):
        with self.assertRaises(RemoteError):
            await self.connect(private_key=Ed25519PrivateKey.generate())
        with self.assertRaises(RemoteError) as error:
            await self.connect(epoch='old')
        self.assertEqual(error.exception.code, 'runtime_changed')

    async def test_forged_snapshot(self):
        oid = await self.submit('printf ok', 1000)
        result = (await self.query(oid))['result']
        with self.assertRaises(RemoteError):
            await self.client.request('output', {'snapshot': result['snapshot'][:-2]+'xx', 'stream': 'stdout'})

    async def test_shutdown_requires_bound_grant_and_refuses_busy(self):
        from dataclasses import replace
        from pal_shell_worker.protocol import canonical
        self.worker.config = replace(self.worker.config, shutdown_argv=('/usr/bin/true',))
        args = {'operation_id': uuid4().hex, 'action': 'shutdown', 'target': 1, 'protected_machine_id': 'different-client-machine'}
        prepared = await self.client.request('prepare_privileged', args)
        waiting = await asyncio.wait_for(self.query(args['operation_id']), 1)
        self.assertEqual(waiting['state'], 'approval_required')
        with self.assertRaises(RemoteError):
            await self.client.request('commit_privileged', {'operation_id': args['operation_id'], 'signature': '00'*64})
        grant = prepared['approval']
        mutated = {**grant, 'target': 2}
        with self.assertRaises(RemoteError):
            await self.client.request('commit_privileged', {'operation_id': args['operation_id'], 'signature': self.key.sign(canonical(mutated)).hex()})
        running = await self.submit('sleep 10')
        await self.query(running)
        request = {'operation_id': args['operation_id'], 'signature': self.key.sign(canonical(grant)).hex()}
        await self.client.request('commit_privileged', request)
        outcome = await self.query(args['operation_id'])
        self.assertEqual(outcome['error']['code'], 'target_busy')
        self.assertFalse(self.worker.draining)
        self.assertEqual((await self.client.request('commit_privileged', request))['state'], 'failed')

    async def test_shutdown_cannot_alias_client_host(self):
        from dataclasses import replace
        self.worker.config = replace(self.worker.config, shutdown_argv=('/usr/bin/true',))
        with self.assertRaises(RemoteError) as error:
            await self.client.request('prepare_privileged', {'operation_id': uuid4().hex, 'action': 'shutdown',
                'target': 9, 'protected_machine_id': self.worker.info['machine_identity']})
        self.assertEqual(error.exception.code, 'protected_host')

    async def test_expired_approval_never_starts_shutdown(self):
        from dataclasses import replace
        from pal_shell_worker.protocol import canonical
        self.worker.config = replace(self.worker.config, shutdown_argv=('/usr/bin/true',))
        args = {'operation_id': uuid4().hex, 'action': 'shutdown', 'target': 1, 'protected_machine_id': 'other'}
        prepared = await self.client.request('prepare_privileged', args)
        self.worker.operations[args['operation_id']].expires_at = 0
        response = await self.client.request('commit_privileged', {'operation_id': args['operation_id'],
            'signature': self.key.sign(canonical(prepared['approval'])).hex()})
        self.assertEqual(response['error']['code'], 'approval_expired')
        self.assertFalse(self.worker.draining)

    async def test_release_running_output_is_rejected(self):
        op = await self.submit('sleep 10')
        result = (await self.query(op))['result']
        with self.assertRaises(RemoteError):
            await self.client.request('release', {'output_id': result['output_id']})
        self.assertTrue(self.worker.reservations)

    async def test_transport_cancellation_drains_without_killing_session(self):
        oid = await self.submit('sleep 0.2; printf still-here')
        initial = (await self.query(oid))['result']
        task = asyncio.create_task(self.client.request('events', {'after': 0, 'wait_ms': 30000}))
        await asyncio.sleep(.01)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await self.client.close()
        self.client = await self.connect(epoch=self.worker.epoch)
        control = uuid4().hex
        await self.client.request('session', {'operation_id': control, 'session_id': initial['session_id'], 'wait_ms': 5000})
        result = (await self.query(control))['result']
        self.assertEqual(result['status'], 'exited')

    async def test_rpc_closed_fd_and_timeout_are_errors_not_process_crashes(self):
        import socket
        import _pal_shell_rpc as rpc
        first, second = socket.socketpair()
        try:
            with self.assertRaises(RuntimeError):
                await asyncio.to_thread(rpc.exchange, first.fileno(), rpc.pack(b'payload'), 10)
            second.close()
            with self.assertRaises(RuntimeError):
                await asyncio.to_thread(rpc.exchange, first.fileno(), rpc.pack(b'payload'), 100)
        finally:
            first.close()
            second.close()

    async def test_authentication_grant_is_single_use_and_never_a_password(self):
        from pal_shell_worker.privilege import approval_payload, consume_authentication
        from pal_shell_worker.protocol import canonical
        import time
        params = {'action': 'sudo', 'target': 1, 'cmd': 'printf approved', 'cwd': '', 'wait_ms': 0}
        op, _ = self.worker._record(uuid4().hex, 'privileged', params)
        op.nonce, op.expires_at = 'nonce', time.time()+60
        grant = approval_payload(self.worker, op)
        signed = {'approval': grant, 'signature': self.key.sign(canonical(grant)).hex()}
        result = consume_authentication(self.worker, signed)
        self.assertEqual(result, {'authorized': True})
        with self.assertRaises(RemoteError):
            consume_authentication(self.worker, signed)
        op.auth_consumed = False
        op.state = 'complete'  # E.g. sudo needed no authentication; no spare grant survives.
        with self.assertRaises(RemoteError):
            consume_authentication(self.worker, signed)

    async def test_askpass_refuses_ordinary_parent_before_contacting_store(self):
        import base64
        import os
        import time
        from unittest.mock import patch
        from pal_shell_worker.askpass import authorize
        from pal_shell_worker.privilege import approval_payload
        from pal_shell_worker.protocol import canonical
        params = {'action': 'sudo', 'target': 1, 'cmd': 'printf approved'}
        op, _ = self.worker._record(uuid4().hex, 'privileged', params)
        op.nonce, op.expires_at = 'nonce', time.time()+60
        grant = approval_payload(self.worker, op)
        envelope = {'args': params, 'approval': grant, 'signature': self.key.sign(canonical(grant)).hex()}
        config = {'client_public_key': self.worker.config.client_public_key, 'worker_id': 'test-worker',
                  'client_id': 'test-client', 'privilege_helper': '/protected/helper', 'shell': '/bin/bash'}
        with patch.dict(os.environ, {'PAL_SHELL_APPROVAL': base64.urlsafe_b64encode(canonical(envelope)).decode()}):
            self.assertFalse(authorize(config))  # Parent is this user's test runner, not privileged sudo.

    async def test_metadata_reports_actual_shell(self):
        data = await self.client.request('metadata')
        self.assertEqual(data['shell']['family'], 'bash')
        self.assertEqual(data['shell']['invocation'], ['-lc'])
        self.assertIn('worker_arch', data)
        self.assertIn('available_bytes', data['memory'])


if __name__ == '__main__':
    unittest.main()
