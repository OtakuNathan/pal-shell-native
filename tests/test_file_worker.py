"""Standalone worker file behavior; no Pal installation is required."""
import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pal_shell_worker.worker import Worker, WorkerConfig
from pal_shell_worker.protocol import RemoteError
from pal_shell_worker.files import CHUNK


class FileWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        key = Ed25519PrivateKey.generate()
        shell = str(Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe') if os.name == 'nt' else '/bin/bash'
        self.worker = Worker(WorkerConfig('worker', 'client', key.public_key().public_bytes_raw().hex(),
            self.root / 'worker.sock', shell=shell))
        self.revision = 0
        self.live = {}

    async def asyncTearDown(self):
        await self.worker.close()
        self.directory.cleanup()

    async def upload(self, value):
        data = json.dumps(value, ensure_ascii=False).encode()
        ref = {'blob': uuid4().hex, 'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
        await self.worker.handle('file_upload_begin', ref)
        for offset in range(0, len(data), CHUNK):
            args = {'blob': ref['blob'], 'offset': offset,
                    'data': base64.b64encode(data[offset:offset + CHUNK]).decode()}
            await self.worker.handle('file_upload_chunk', args)
            if offset == 0 and len(data) > CHUNK:
                await self.worker.handle('file_upload_chunk', args)  # duplicate chunk
        return ref

    async def sync(self, pending=(), closed=False):
        ref = await self.upload({'scope': 'test', 'revision': self.revision,
            'live': self.live, 'pending': list(pending), 'closed': closed})
        try:
            return await self.worker.handle('file_sync', {'payload': ref})
        finally:
            await self.worker.handle('file_blob_release', {'blob': ref['blob']})

    async def query(self, oid):
        response = await self.worker.handle('query', {'operation_id': oid, 'wait_ms': 5000})
        self.assertEqual(response['state'], 'complete', response)
        ref = response['result']
        data = bytearray()
        while len(data) < ref['size']:
            chunk = await self.worker.handle('file_blob_read', {'blob': ref['blob'], 'offset': len(data)})
            data.extend(base64.b64decode(chunk['data']))
        self.assertEqual(hashlib.sha256(data).hexdigest(), ref['sha256'])
        return json.loads(data)

    async def execute(self, action, args, oid=None):
        oid = oid or uuid4().hex
        ref = await self.upload({'scope': 'test', 'revision': self.revision, 'action': action, 'args': args})
        request = {'operation_id': oid, 'payload': ref}
        await self.worker.handle('file_execute', request)
        result = await self.query(oid)
        # A duplicate transport request returns the existing operation.
        await self.worker.handle('file_execute', request)
        self.assertEqual(await self.query(oid), result)
        await self.worker.handle('file_blob_release', {'blob': ref['blob']})
        return oid, result

    async def test_ack_visibility_revocation_and_exact_newlines(self):
        path = self.root / 'source.txt'
        path.write_bytes(b'\xef\xbb\xbfhello\r\nworld\r\n')
        await self.sync(['read'])
        oid, result = await self.execute('read', {'file_path': str(path), 'limit': 1}, 'read')
        args = {'file_path': str(path), 'edits': [{'old_string': 'hello', 'new_string': 'greetings'}]}
        _, denied = await self.execute('edit', args)
        self.assertNotEqual(denied['status'], 'ok')
        self.live['r'] = {'operation_id': oid, 'manifest': result['context_delivery']}
        self.revision += 1
        await self.sync()
        _, denied = await self.execute('edit', {'file_path': str(path),
            'edits': [{'old_string': 'world', 'new_string': 'hidden'}]})
        self.assertNotEqual(denied['status'], 'ok')
        _, edited = await self.execute('edit', args)
        self.assertEqual(edited['status'], 'ok', edited)
        self.assertEqual(path.read_bytes(), b'\xef\xbb\xbfgreetings\r\nworld\r\n')
        self.live.clear()
        self.revision += 1
        await self.sync(closed=True)
        with self.assertRaises(RemoteError):
            self.revision -= 1
            await self.sync()

    async def test_large_transfer_journal_fingerprint_and_lost_reply(self):
        await self.sync()
        path = self.root / 'large.txt'
        content = '字' * 300000
        oid, written = await self.execute('write', {'file_path': str(path), 'content': content})
        self.assertEqual(written['status'], 'ok', written)
        self.assertEqual(path.read_bytes(), content.encode('utf-8'))
        self.assertEqual(await self.query(oid), written)
        ref = await self.upload({'scope': 'test', 'revision': self.revision, 'action': 'write',
            'args': {'file_path': str(path), 'content': 'different'}})
        with self.assertRaises(RemoteError) as error:
            await self.worker.handle('file_execute', {'operation_id': oid, 'payload': ref})
        self.assertEqual(error.exception.code, 'operation_conflict')
        self.assertEqual(path.read_bytes(), content.encode('utf-8'))

    async def test_corrupted_upload_has_no_file_effect(self):
        ref = {'blob': 'bad', 'size': 3, 'sha256': '0' * 64}
        await self.worker.handle('file_upload_begin', ref)
        with self.assertRaises(RemoteError):
            await self.worker.handle('file_upload_chunk', {'blob': 'bad', 'offset': 0, 'data': 'YWJj'})
        with self.assertRaises(RemoteError):
            await self.worker.handle('file_execute', {'operation_id': 'bad', 'payload': ref})
        self.assertNotIn('bad', self.worker.operations)


if __name__ == '__main__':
    unittest.main()
