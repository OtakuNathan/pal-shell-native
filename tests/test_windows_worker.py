"""Windows-only native process and worker regression; no machine management."""
import asyncio
import os
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

if os.name != 'nt':
    raise unittest.SkipTest('Windows native prototype acceptance')

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pal_shell_worker.worker import Worker, WorkerConfig
from pal_shell_worker.protocol import RemoteError, TERMINAL


class WindowsWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        key = Ed25519PrivateKey.generate()
        shell = str(Path(os.environ['SystemRoot'])/'System32/WindowsPowerShell/v1.0/powershell.exe')
        self.worker = await Worker(WorkerConfig('test', 'client', key.public_key().public_bytes_raw().hex(),
            self.root/'endpoint.json', shell=shell, tcp_port=0, output_limit=4096,
            shutdown_policy='disabled')).start()

    async def asyncTearDown(self):
        await self.worker.close()
        self.directory.cleanup()

    async def read_session(self, sid, wait_ms=1000):
        oid = uuid4().hex
        await self.worker.handle('session', {'operation_id': oid, 'session_id': sid,
                                            'action': 'read', 'wait_ms': wait_ms})
        reply = await self.worker.handle('query', {'operation_id': oid, 'wait_ms': 5000})
        self.assertIsNone(reply['error'], reply)
        self.assertIsNotNone(reply['result'], reply)
        return reply['result']

    async def run_command(self, cmd, *, complete=True, **kwargs):
        oid = uuid4().hex
        await self.worker.handle('submit', {'operation_id': oid, 'cmd': cmd, 'wait_ms': 1000, **kwargs})
        reply = await self.worker.handle('query', {'operation_id': oid, 'wait_ms': 5000})
        self.assertIsNone(reply['error'], reply)
        result = reply['result']
        # query recovers the submit snapshot, which may precede process exit.
        # Read the existing session instead of assuming a one-second startup.
        if complete:
            async with asyncio.timeout(15):
                while result['status'] not in TERMINAL:
                    result = await self.read_session(result['session_id'], 5000)
        return result

    async def test_unicode_exit_and_metadata(self):
        result = await self.run_command("[Console]::Write('你好'); [Console]::Error.Write('错误'); exit 7")
        self.assertEqual(result['returncode'], 7)
        for stream, value in [('stdout', '你好'), ('stderr', '错误')]:
            reply = await self.worker.handle('output', {'snapshot': result['snapshot'], 'stream': stream})
            self.assertEqual(reply['data'].decode('utf-8'), value)
        info = await self.worker.handle('metadata', {})
        self.assertEqual(info['shell']['family'], 'powershell')
        self.assertFalse(info['shell']['pty'])
        self.assertFalse(info['power']['shutdown'])
        self.assertEqual(self.worker.server.sockets[0].getsockname()[0], '127.0.0.1')

    async def test_unsupported_before_effect(self):
        for action in ['sudo', 'shutdown']:
            with self.assertRaises(RemoteError):
                await self.worker.handle('prepare_privileged', {'operation_id': uuid4().hex, 'action': action, 'target': 1})
        with self.assertRaises(RemoteError):
            await self.worker.handle('submit', {'operation_id': uuid4().hex, 'cmd': 'exit 0', 'tty': True})
        self.assertFalse(self.worker.operations)
        self.assertFalse(self.worker.draining)

    async def test_unicode_working_directory(self):
        directory = self.root/'中文 space'
        directory.mkdir()
        result = await self.run_command('[Console]::Write((Get-Location).Path)', cwd=str(directory))
        reply = await self.worker.handle('output', {'snapshot': result['snapshot'], 'stream': 'stdout'})
        # Hosted runners expose TEMP via an 8.3 alias (RUNNER~1), while
        # PowerShell returns the long path (runneradmin) for the same directory.
        self.assertTrue(Path(reply['data'].decode('utf-8')).samefile(directory))

    async def test_bounded_output_and_timeout(self):
        result = await self.run_command("[Console]::Write('x'*100000)")
        self.assertTrue(result['truncated'])
        self.assertLessEqual(result['stdout_total']+result['stderr_total'], 4096)
        self.assertIn('output_limit_exceeded', result['error'])
        result = await self.run_command('Start-Sleep -Seconds 30', timeout_ms=300)
        self.assertEqual(result['status'], 'timed_out')

    async def test_termination_reaps_descendants(self):
        result = await self.run_command("$p=Start-Process ping.exe -ArgumentList '-n 60 127.0.0.1' -WindowStyle Hidden -PassThru; [Console]::Write($p.Id); Start-Sleep -Seconds 60", complete=False)
        async with asyncio.timeout(15):
            while True:
                output = await self.worker.handle('output', {'snapshot': result['snapshot'], 'stream': 'stdout'})
                if output['data']:
                    break
                self.assertNotIn(result['status'], TERMINAL, result)
                result = await self.read_session(result['session_id'])
        pid = int(output['data'])
        sid = result['session_id']
        for action in ['terminate', 'read']:
            oid = uuid4().hex
            await self.worker.handle('session', {'operation_id': oid, 'session_id': sid, 'action': action,
                                                **({'wait_ms': 5000} if action == 'read' else {})})
            end = await self.worker.handle('query', {'operation_id': oid, 'wait_ms': 5000})
        self.assertEqual(end['result']['status'], 'cancelled')
        probe = await self.run_command(f"if (Get-Process -Id {pid} -ErrorAction SilentlyContinue) {{exit 9}}; exit 0")
        self.assertEqual(probe['returncode'], 0)


if __name__ == '__main__':
    unittest.main()
