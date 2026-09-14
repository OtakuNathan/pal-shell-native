from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass, field
import hashlib
import hmac
import itertools
import os
from pathlib import Path
import secrets
import stat
import time
from uuid import uuid4

import _pal_shell_runtime as native
import _pal_shell_rpc as wire
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from . import PROTOCOL_VERSION
from .metadata import probe, shell_info
from .protocol import (RemoteError, TERMINAL, OUTPUT_BYTES, RETAINED_BYTES, CHUNK_BYTES,
                       auth_message, canonical, decode, digest, integer, read_frame, send_response, text)


@dataclass(frozen=True)
class WorkerConfig:
    worker_id: str
    client_id: str
    client_public_key: str
    socket_path: Path
    shell: str = '/bin/bash'
    output_limit: int = OUTPUT_BYTES
    retained_limit: int = RETAINED_BYTES
    operation_limit: int = 4096
    shutdown_argv: tuple[str, ...] = ()
    protected_machine_ids: tuple[str, ...] = ()
    shutdown_policy: str = 'approval'
    approvers: tuple[str, ...] = ()
    credential_ref: str = ''
    privilege_helper: str = ''
    askpass_helper: str = ''
    tcp_port: int | None = None

    def __post_init__(self):
        if self.tcp_port is not None:
            integer(self.tcp_port, 'tcp_port', 0, 65535)
        if os.name == 'nt' and (self.shutdown_argv or self.privilege_helper or self.askpass_helper):
            raise ValueError('Windows worker has no power or privilege management')
        text(self.worker_id, 'worker_id', limit=128)
        text(self.client_id, 'client_id', limit=128)
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(self.client_public_key))
        if not Path(self.socket_path).is_absolute():
            raise ValueError('Worker socket must be absolute')
        if self.shutdown_argv and (not all(isinstance(x, str) and '\0' not in x for x in self.shutdown_argv) or not Path(self.shutdown_argv[0]).is_absolute()):
            raise ValueError('Shutdown action requires an absolute executable and literal argv')
        if not Path(self.shell).is_absolute():
            raise ValueError('Worker shell must be absolute')
        integer(self.output_limit, 'output_limit', 1, OUTPUT_BYTES)
        integer(self.retained_limit, 'retained_limit', self.output_limit, 2**31 - 1)
        integer(self.operation_limit, 'operation_limit', 1, 100000)
        if self.shutdown_policy not in {'approval', 'preauthorized', 'disabled'}:
            raise ValueError('Invalid shutdown policy')


@dataclass
class Operation:
    operation_id: str
    fingerprint: str
    method: str
    args: dict
    state: str = 'pending'
    result: dict | None = None
    error: dict | None = None
    done: asyncio.Event = field(default_factory=asyncio.Event)
    nonce: str = ''
    expires_at: float = 0
    auth_consumed: bool = False
    approval_signature: str = ''

    def snapshot(self):
        return {'operation_id': self.operation_id, 'state': self.state,
                'result': self.result, 'error': self.error}


class Worker:
    def __init__(self, config: WorkerConfig):
        self.config = config
        self.epoch = uuid4().hex
        self.loop = asyncio.get_running_loop()
        self.runtime = native.Runtime(32)
        if not hasattr(self.runtime, "run_limited"):
            self.runtime.close()
            raise RemoteError("native_incompatible", "Worker requires the matching bounded-output native extension")
        self.sequence = itertools.count(1)
        self.pending = {}
        self.operations: dict[str, Operation] = {}
        self.outputs = {}
        self.native_outputs = {}
        self.native_owners = {}
        self.events = {}
        self.cursor = 0
        self.changed = asyncio.Event()
        self.tasks = set()
        self.connections = set()
        self.draining = False
        self.closed = False
        self.reservations = set()
        self.snapshot_key = secrets.token_bytes(32)
        self.info = None
        self.server = None
        self.socket_identity = None
        native.connect(self.runtime, lambda event: self.loop.call_soon_threadsafe(self._deliver, dict(event)))

    async def start(self):
        shell = await asyncio.to_thread(shell_info, self.config.shell)
        self.info = await asyncio.to_thread(probe, shell)
        if not shell['verified']:
            raise RemoteError('shell_unsupported', 'Configured executable is not a supported verified shell')
        path = self.config.socket_path
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name != 'nt' and (path.parent.stat().st_uid != os.getuid() or path.parent.stat().st_mode & 0o077):
            raise RemoteError('unsafe_endpoint', 'Worker socket directory must be private to its user')
        # Never unlink an endpoint that may still belong to a live worker.
        if path.exists() or path.is_symlink():
            raise RemoteError('endpoint_exists', 'Worker endpoint already exists; inspect its owner before removal')
        if os.name == 'nt':
            import json
            if self.config.tcp_port is None:
                raise RemoteError('endpoint_unsupported', 'Windows worker requires a loopback tcp_port')
            self.server = await asyncio.start_server(self._connection, host='127.0.0.1', port=self.config.tcp_port, limit=1024 * 1024)
            with path.open('x') as endpoint:
                json.dump({'port': self.server.sockets[0].getsockname()[1], 'pid': os.getpid(), 'runtime_epoch': self.epoch}, endpoint)
        else:
            self.server = await asyncio.start_unix_server(self._connection, path=str(path), limit=1024 * 1024)
            os.chmod(path, 0o600)
        self.socket_identity = (path.stat().st_dev, path.stat().st_ino)
        return self

    async def close(self):
        self.closed = True
        self.changed.set()
        if self.server:
            self.server.close()
        for writer in list(self.connections):
            writer.close()
        if self.server:
            await self.server.wait_closed()
        await asyncio.to_thread(self.runtime.close)
        await asyncio.sleep(0)
        for task in list(self.tasks):
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        for future in self.pending.values():
            if not future.done():
                future.set_exception(RemoteError('worker_closed', 'Worker stopped', effect='unknown'))
        path = self.config.socket_path
        try:
            s = path.lstat()
            if (s.st_dev, s.st_ino) == self.socket_identity:
                path.unlink()
        except FileNotFoundError:
            pass

    def _task(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    async def _connection(self, reader, writer):
        self.connections.add(writer)
        nonce, deadline, authenticated = secrets.token_hex(32), time.monotonic() + 30, False
        try:
            while not self.closed:
                frame = await asyncio.wait_for(read_frame(reader), timeout=90 if authenticated else 30)
                try:
                    request = decode(wire.unpack(frame))
                    method, args = request.get('method'), request.get('params', {})
                    if not isinstance(args, dict):
                        raise RemoteError('invalid_request', 'params must be an object')
                    if not authenticated:
                        if method == 'consume_privileged':
                            from .privilege import consume_authentication
                            result = consume_authentication(self, args)
                        elif method == 'hello':
                            result = {'nonce': nonce, 'worker_id': self.config.worker_id,
                                      'runtime_epoch': self.epoch, 'protocol_version': PROTOCOL_VERSION}
                        elif method == 'authenticate' and time.monotonic() <= deadline:
                            if args.get('client_id') != self.config.client_id:
                                raise RemoteError('unauthorized', 'Client identity is not enrolled')
                            try:
                                key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(self.config.client_public_key))
                                key.verify(bytes.fromhex(args['signature']), auth_message(nonce, self.config.worker_id, self.epoch, self.config.client_id))
                            except Exception as exc:
                                raise RemoteError('unauthorized', 'Client signature rejected') from exc
                            authenticated = True
                            result = {'authenticated': True, 'runtime_epoch': self.epoch}
                        else:
                            raise RemoteError('unauthorized', 'Authenticated client connection required')
                    else:
                        if args.get('runtime_epoch') != self.epoch:
                            raise RemoteError('runtime_changed', 'Runtime identity changed; old execution effects are unknown', effect='unknown', operation_id=str(args.get('operation_id', '')))
                        result = await self.handle(method, {k: v for k, v in args.items() if k != 'runtime_epoch'})
                    await send_response(writer, frame, {'ok': True, 'result': result})
                except RemoteError as exc:
                    await send_response(writer, frame, {'ok': False, 'error': exc.payload()})
                except (ValueError, TypeError, KeyError) as exc:
                    await send_response(writer, frame, {'ok': False, 'error': RemoteError('invalid_request', 'Malformed shell request').payload()})
        except (OSError, asyncio.IncompleteReadError, TimeoutError, RemoteError, RuntimeError):
            pass
        finally:
            self.connections.discard(writer)
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass

    async def _native(self, method, *args):
        request = next(self.sequence)
        future = self.loop.create_future()
        self.pending[request] = future
        try:
            getattr(self.runtime, method)(request, *args)
            event = await asyncio.shield(future)
            if event['status'] == 'rejected':
                raise RemoteError(event['error'].partition(':')[0], event['error'])
            return event
        finally:
            self.pending.pop(request, None)

    def _deliver(self, event):
        request = event['request_id']
        if request:
            future = self.pending.get(request)
            if future is not None and not future.done():
                future.set_result(event)
        elif event['output_id']:
            result = self._snapshot(event)
            self.cursor += 1
            self.events[event['output_id']] = {'cursor': self.cursor, 'result': result}
            self.changed.set()

    def _snapshot(self, event):
        output = event['output_id']
        result = {k: v for k, v in event.items() if not k.endswith(('_path', '_bytes')) and k != 'request_id'}
        if output:
            token = self.native_outputs.setdefault(output, uuid4().hex)
            previous = self.outputs.get(token)
            if previous is None or previous['event']['status'] not in TERMINAL or event['status'] in TERMINAL:
                self.outputs[token] = {'native_id': output, 'event': dict(event)}
            lengths = [token, event['stdout_total'], event['stderr_total'], self.epoch]
            raw = canonical(lengths)
            signature = hmac.new(self.snapshot_key, raw, hashlib.sha256).digest()
            result.update(output_id=token, snapshot=base64.urlsafe_b64encode(signature + raw).decode(),
                          output_complete=not event.get('truncated', False))
        result['runtime_epoch'] = self.epoch
        result['shell'] = self.info['shell'] if self.info else {'executable': self.config.shell}
        result['execution_id'] = self.native_owners.get(output, '')
        return result

    def _record(self, operation_id, method, args):
        operation_id = text(operation_id, 'operation_id', limit=128)
        fingerprint = digest([method, args])
        previous = self.operations.get(operation_id)
        if previous:
            if previous.fingerprint != fingerprint:
                raise RemoteError('operation_conflict', 'Operation ID already has different arguments')
            return previous, False
        if len(self.operations) >= self.config.operation_limit:
            raise RemoteError('operation_capacity', 'Operation journal full; existing records remain queryable')
        op = Operation(operation_id, fingerprint, method, dict(args))
        self.operations[operation_id] = op
        return op, True

    def _finish(self, op, result=None, error=None):
        op.state = 'failed' if error else 'complete'
        op.result, op.error = result, error.payload() if error else None
        op.done.set()
        self.changed.set()

    async def _execute(self, op):
        try:
            args = op.args
            if op.method == 'submit':
                result = await self._native('run_limited', self.config.shell, args['cmd'], args.get('cwd', ''),
                    args.get('tty', False), args['wait_ms'], args.get('timeout_ms') or 0, 0, self.config.output_limit)
                if result.get('output_id'):
                    self.native_owners[result['output_id']] = op.operation_id
                snapshot = self._snapshot(result)
                # Completion can be queued before this task resumes from the initial result.
                if result['output_id'] in self.events:
                    self.events[result['output_id']]['result']['execution_id'] = op.operation_id
                self._finish(op, snapshot)
            else:
                sid, action = args['session_id'], args['action']
                arguments = {'read': (sid, args.get('wait_ms', 0)), 'write': (sid, args.get('text', '')),
                             'resize': (sid, args.get('rows'), args.get('columns')), 'terminate': (sid,)}
                self._finish(op, self._snapshot(await self._native(action, *arguments[action])))
        except RemoteError as exc:
            if op.method == 'submit':
                self.reservations.discard(op.operation_id)
            self._finish(op, error=exc)
        except Exception:
            self._finish(op, error=RemoteError('execution_unknown', 'Worker could not confirm operation outcome', effect='unknown', operation_id=op.operation_id))

    async def handle(self, method, args):
        if method == 'metadata':
            from .privilege import trusted_executable
            if args.get('refresh'):
                self.info = await asyncio.to_thread(probe, await asyncio.to_thread(shell_info, self.config.shell))
            active = sum(item['event']['status'] not in TERMINAL for item in self.outputs.values())
            return {**self.info, 'worker_id': self.config.worker_id, 'runtime_epoch': self.epoch,
                    'protocol_version': PROTOCOL_VERSION, 'active_tasks': active, 'draining': self.draining,
                    'retained_outputs': len(self.outputs), 'reserved_bytes': len(self.reservations) * self.config.output_limit,
                    'limits': {'output_bytes': self.config.output_limit, 'retained_bytes': self.config.retained_limit,
                               'chunk_bytes': CHUNK_BYTES}, 'privilege': {'configured': bool(self.config.privilege_helper),
                               'supported': trusted_executable(self.config.privilege_helper) and trusted_executable(self.config.askpass_helper),
                               'credential_state': 'remote_only_not_probed', 'approval_required': True}, 'power': {'shutdown': bool(self.config.shutdown_argv),
                               'policy': self.config.shutdown_policy}}
        if method == 'query':
            op = self.operations.get(text(args.get('operation_id'), 'operation_id', limit=128))
            if not op:
                raise RemoteError('execution_unknown', 'No operation record; this is not proof it never executed', effect='unknown', operation_id=args['operation_id'])
            wait = integer(args.get('wait_ms', 0), 'wait_ms', 0, 300000)
            if op.state == 'pending' and not op.done.is_set() and wait:
                try:
                    await asyncio.wait_for(op.done.wait(), wait / 1000)
                except TimeoutError:
                    pass
            return op.snapshot()
        if method in {'submit', 'session'}:
            params = {k: v for k, v in args.items() if k != 'operation_id'}
            if method == 'submit':
                if set(params) - {'cmd', 'cwd', 'tty', 'wait_ms', 'timeout_ms'}:
                    raise RemoteError('invalid_request', 'Unsupported execution arguments')
                text(params.get('cmd'), 'cmd')
                text(params.get('cwd', ''), 'cwd', empty=True)
                if type(params.get('tty', False)) is not bool:
                    raise RemoteError('invalid_request', 'tty must be boolean')
                if os.name == 'nt' and params.get('tty'):
                    raise RemoteError('pty_unsupported', 'Windows prototype does not provide interactive ConPTY')
                params['wait_ms'] = integer(params.get('wait_ms', 1000 if params.get('tty') else 300000), 'wait_ms', 0, 300000)
                integer(params.get('timeout_ms') or 0, 'timeout_ms')
            else:
                sid = integer(params.get('session_id'), 'session_id', 1, 2**63-1)
                if sid not in self.native_outputs:
                    raise RemoteError('invalid_session', 'Session is unknown or released')
                action = params.get('action', 'read')
                allowed = {'read': {'wait_ms'}, 'write': {'text'}, 'resize': {'rows', 'columns'}, 'terminate': set()}
                if action not in allowed or set(params) - {'session_id', 'action'} - allowed[action]:
                    raise RemoteError('invalid_request', 'Unsupported session action or arguments')
                params['action'] = action
                if action == 'read':
                    integer(params.get('wait_ms', 0), 'wait_ms', 0, 300000)
                if action == 'write':
                    text(params.get('text'), 'text', empty=True)
                if action == 'resize':
                    integer(params.get('rows'), 'rows', 1, 65535)
                    integer(params.get('columns'), 'columns', 1, 65535)
            op, fresh = self._record(args.get('operation_id'), method, params)
            if fresh:
                if self.draining:
                    self._finish(op, error=RemoteError('worker_draining', 'Worker is not accepting new operations'))
                elif method == 'submit' and (len(self.reservations)+1) * self.config.output_limit > self.config.retained_limit:
                    self._finish(op, error=RemoteError('output_capacity', 'Release retained output before submitting'))
                else:
                    if method == 'submit':
                        self.reservations.add(op.operation_id)
                    self._task(self._execute(op))
            return op.snapshot()
        if method == 'events':
            after = integer(args.get('after', 0), 'after', 0, 2**63-1)
            wait = integer(args.get('wait_ms', 30000), 'wait_ms', 0, 30000)
            if not any(e['cursor'] > after for e in self.events.values()) and wait:
                self.changed.clear()
                try:
                    await asyncio.wait_for(self.changed.wait(), wait/1000)
                except TimeoutError:
                    pass
            return {'events': sorted((e for e in self.events.values() if e['cursor'] > after), key=lambda e: e['cursor'])[:32], 'runtime_epoch': self.epoch}
        if method == 'output':
            try:
                raw = base64.urlsafe_b64decode(args['snapshot'].encode())
                if not hmac.compare_digest(raw[:32], hmac.new(self.snapshot_key, raw[32:], hashlib.sha256).digest()):
                    raise ValueError()
                import json
                token, out, err, epoch = json.loads(raw[32:])
                if epoch != self.epoch:
                    raise ValueError()
            except (KeyError, ValueError, TypeError):
                raise RemoteError('invalid_output', 'Invalid output snapshot')
            item = self.outputs.get(token)
            if not item:
                raise RemoteError('output_released', 'Output was already released')
            stream = args.get('stream')
            if stream not in {'stdout', 'stderr'}:
                raise RemoteError('invalid_request', 'Unknown output stream')
            size = out if stream == 'stdout' else err
            offset = integer(args.get('offset', 0), 'offset', 0, size)
            length = integer(args.get('length', CHUNK_BYTES), 'length', 1, CHUNK_BYTES)
            path = item['event'][stream + '_path']
            def load():
                fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0))
                try:
                    if not stat.S_ISREG(os.fstat(fd).st_mode):
                        raise OSError('Output is not a regular file')
                    if hasattr(os, 'pread'):
                        data = os.pread(fd, min(length, size - offset), offset)
                    else:
                        os.lseek(fd, offset, os.SEEK_SET)
                        data = os.read(fd, min(length, size - offset))
                    if len(data) != min(length, size-offset):
                        raise OSError('Output shorter than snapshot')
                    return data
                finally:
                    os.close(fd)
            try:
                return {'data': await asyncio.to_thread(load), 'offset': offset, 'total': size}
            except OSError as exc:
                raise RemoteError('output_unavailable', 'Retained output cannot currently be read', effect='applied') from exc
        if method == 'release':
            token = text(args.get('output_id'), 'output_id', limit=128)
            item = self.outputs.get(token)
            if item:
                nid = item['native_id']
                await self._native('release_output', nid)
                self.outputs.pop(token, None)
                self.native_outputs.pop(nid, None)
                self.events.pop(nid, None)
                self.reservations.discard(self.native_owners.pop(nid, ''))
            return {'status': 'output_released'}
        if method in {'prepare_privileged', 'commit_privileged'}:
            from .privilege import handle_privileged
            return await handle_privileged(self, method, args)
        raise RemoteError('unknown_method', 'Unknown worker operation')
