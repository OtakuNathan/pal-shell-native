"""File protocol v1 over the existing authenticated RPC and operation journal."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from uuid import uuid4

from ._file_core.file_registry import FileRegistry, FileRegistryError
from .protocol import RemoteError, integer, text

FILE_PROTOCOL_VERSION = 1
CHUNK = 128 * 1024
MAX_BLOB = 32 * 1024 * 1024


class WorkerFiles:
    def __init__(self, worker):
        self.worker = worker
        self.registry = FileRegistry(capacity=worker.config.operation_limit)
        self.blobs = {}
        self.uploads = {}
        self.reserved = 0

    def _reserve(self, size):
        integer(size, 'size', 0, MAX_BLOB)
        if self.reserved + size > self.worker.config.retained_limit:
            raise RemoteError('file_capacity', 'Retained file transfer capacity reached')
        self.reserved += size

    def _store(self, value):
        data = json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        self._reserve(len(data))
        token = uuid4().hex
        self.blobs[token] = data
        return {'blob': token, 'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()}

    def _load(self, ref):
        data = self.blobs.get(ref.get('blob'))
        if data is None or len(data) != ref.get('size') or hashlib.sha256(data).hexdigest() != ref.get('sha256'):
            raise RemoteError('invalid_file_blob', 'File transfer size or digest mismatch')
        return json.loads(data)

    async def execute(self, op):
        try:
            args = op.args
            result = await asyncio.to_thread(self.registry.execute, args['scope'], args['revision'],
                op.operation_id, args['action'], args['args'])
            self.worker._finish(op, self._store(result))
        except FileRegistryError as exc:
            self.worker._finish(op, error=RemoteError('file_authority', str(exc)))
        except Exception:
            self.worker._finish(op, error=RemoteError('file_outcome_unknown',
                'File result could not be retained; reconcile the original operation before further writes',
                effect='unknown', operation_id=op.operation_id))
        finally:
            op.args.pop('args', None)

    async def handle(self, method, args):
        if method == 'file_result_release':
            oid = text(args.get('operation_id'), 'operation_id', limit=128)
            op = self.worker.operations.get(oid)
            if op is None or op.method != 'file_execute' or op.state != 'complete':
                raise RemoteError('invalid_file_operation', 'No completed file receipt')
            payload = self._load(op.result)
            if not payload.get('result_retired'):
                structured = payload.get('structured') or {}
                receipt = {key: structured[key] for key in ('file_path', 'error_code',
                    'bytes_written', 'created', 'operation', 'applied_edit_indices') if key in structured}
                summary = {'status': payload['status'], 'llm_text': 'Original file text retired; operation receipt retained.',
                    'structured': receipt, 'effect_receipt': payload.get('effect_receipt'), 'result_retired': True}
                data = self.blobs.pop(op.result['blob'])
                self.reserved -= len(data)
                op.result = self._store(summary)
            return {}
        if method == 'file_upload_begin':
            token = text(args.get('blob'), 'blob', limit=128)
            size = integer(args.get('size'), 'size', 0, MAX_BLOB)
            sha = text(args.get('sha256'), 'sha256', limit=64)
            if token in self.uploads or token in self.blobs:
                raise RemoteError('file_transfer_conflict', 'Transfer ID already exists')
            self._reserve(size)
            self.uploads[token] = (size, sha, bytearray())
            return {'blob': token}
        if method == 'file_upload_chunk':
            token = text(args.get('blob'), 'blob', limit=128)
            if token not in self.uploads:
                raise RemoteError('invalid_file_blob', 'Upload does not exist')
            size, sha, data = self.uploads[token]
            offset = integer(args.get('offset'), 'offset', 0, size)
            try:
                chunk = base64.b64decode(args['data'], validate=True)
            except (ValueError, TypeError):
                raise RemoteError('invalid_file_blob', 'Invalid file chunk encoding') from None
            if len(chunk) > CHUNK or offset > len(data) or offset + len(chunk) > size:
                raise RemoteError('invalid_file_blob', 'File chunk exceeds transfer bounds')
            if offset < len(data):
                if data[offset:offset + len(chunk)] != chunk:
                    raise RemoteError('file_transfer_conflict', 'Conflicting repeated file chunk')
            else:
                data.extend(chunk)
            if len(data) == size:
                if hashlib.sha256(data).hexdigest() != sha:
                    raise RemoteError('invalid_file_blob', 'File upload digest mismatch')
                self.blobs[token] = bytes(data)
                del self.uploads[token]
            return {'offset': len(data)}
        if method == 'file_blob_read':
            data = self.blobs.get(args.get('blob'))
            if data is None:
                raise RemoteError('invalid_file_blob', 'File transfer is unavailable')
            offset = integer(args.get('offset', 0), 'offset', 0, len(data))
            return {'data': base64.b64encode(data[offset:offset + CHUNK]).decode('ascii')}
        if method == 'file_blob_release':
            token = args.get('blob')
            data = self.blobs.pop(token, None)
            if data is not None:
                self.reserved -= len(data)
            upload = self.uploads.pop(token, None)
            if upload is not None:
                self.reserved -= upload[0]
            return {}
        if method == 'file_sync':
            value = self._load(args['payload'])
            try:
                return await asyncio.to_thread(self.registry.sync, **value)
            except FileRegistryError as exc:
                raise RemoteError('file_authority', str(exc)) from exc
        if method == 'file_execute':
            if self.worker.closed or self.worker.draining:
                raise RemoteError('worker_draining', 'Worker is not admitting new file operations')
            value = self._load(args['payload'])
            oid = text(args.get('operation_id'), 'operation_id', limit=128)
            op, fresh = self.worker._record(oid, 'file_execute', value)
            if fresh:
                task = asyncio.create_task(self.execute(op))
                self.worker.tasks.add(task)
                task.add_done_callback(self.worker.tasks.discard)
            return op.snapshot()
        raise RemoteError('unknown_method', 'Unknown file protocol method')
