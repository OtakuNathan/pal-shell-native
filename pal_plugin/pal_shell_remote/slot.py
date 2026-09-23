from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
import re
import tempfile

from pal.foundation.fd_lease import FdLease, FdCloseOutcome, FdLeaseInvariantError
from pal_shell_worker.client import Connection, load_private_key
from pal_shell_worker.protocol import RemoteError
from .admission import RequestAdmission


@dataclass(frozen=True)
class Target:
    target: int
    name: str
    worker_id: str
    client_id: str
    client_key: str
    socket_path: str
    shortcut: str = ''
    ssh_host: str = ''
    ssh_port: int = 22
    ssh_identity: str = ''
    known_hosts: str = ''
    static: dict = field(default_factory=dict)
    start_actions: dict = field(default_factory=dict)
    worker_port: int = 0

    def __post_init__(self):
        if type(self.target) is not int or self.target <= 0:
            raise ValueError('Remote targets must be positive integers; zero is always local')
        if self.shortcut and not re.fullmatch(r'[A-Za-z0-9_-]{1,54}', self.shortcut):
            raise ValueError('Shortcut must be 1–54 letters, digits, underscores or hyphens')
        if type(self.ssh_port) is not int or not 1 <= self.ssh_port <= 65535:
            raise ValueError('Invalid SSH port')
        if self.ssh_host:
            if not re.fullmatch(r'[A-Za-z0-9_.@-]+', self.ssh_host) or self.ssh_host.startswith('-'):
                raise ValueError('Invalid SSH destination')
            if not self.ssh_identity or not self.known_hosts:
                raise ValueError('SSH requires explicit identity and known_hosts files')
        if type(self.worker_port) is not int or not 0 <= self.worker_port <= 65535:
            raise ValueError('Invalid worker loopback port')
        if self.worker_port and not self.ssh_host:
            raise ValueError('Worker TCP transport requires authenticated SSH forwarding')
        if not self.worker_port and (not Path(self.socket_path).is_absolute() or ':' in self.socket_path or '\n' in self.socket_path):
            raise ValueError('Worker socket must be an absolute forwarding-safe path')
        for argv in self.start_actions.values():
            if not isinstance(argv, list) or not argv or not Path(argv[0]).is_absolute() or not all(isinstance(x, str) for x in argv):
                raise ValueError('Start actions must reference existing executables with literal argv')


async def close_connection(connection):
    await connection.close()
    return FdCloseOutcome.detached()


class RemoteSlot:
    def __init__(self, config, executor=None):
        from pal_shell_worker.transport import Executor
        self.executor = executor or Executor()
        self.owns_executor = executor is None
        self.connection = None
        from .execution_gate import ExecutionGate
        self.execution = ExecutionGate(config.target)
        self.previous_epoch = None
        self.session_locks = {}
        self.config = config
        self.lock = asyncio.Lock()
        self.admission = RequestAdmission()
        self.lease = None
        self.epoch = None
        self.tunnel = None
        self.directory = None
        self.cached = None
        self.last_error = ''
        self.expected_offline = False
        self.closed = False

    async def _disconnect(self):
        if self.lease:
            await self.lease.force_revoke_async('transport retired')
            if not self.lease.closed:
                raise RemoteError('transport_quarantined', 'Prior transport has not drained')
            self.lease = None
        self.connection = None
        if self.tunnel:
            if self.tunnel.returncode is None:
                self.tunnel.terminate()
                try:
                    await asyncio.wait_for(self.tunnel.wait(), 5)
                except TimeoutError:
                    self.tunnel.kill()
                    await self.tunnel.wait()
            self.tunnel = None
        if self.directory:
            self.directory.cleanup()
            self.directory = None

    async def _connect(self):
        if self.closed:
            raise RemoteError('backend_unavailable', 'Target connection has been detached')
        c = self.config
        path = c.socket_path
        if c.ssh_host:
            self.directory = tempfile.TemporaryDirectory(prefix='pal-remote-')
            path = str(Path(self.directory.name) / 'rpc.sock')
            self.tunnel = await asyncio.create_subprocess_exec(
                'ssh', '-N', '-T', '-p', str(c.ssh_port), '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
                '-o', 'IdentitiesOnly=yes', '-o', 'PasswordAuthentication=no',
                '-o', 'KbdInteractiveAuthentication=no', '-o', 'ExitOnForwardFailure=yes',
                '-o', 'ConnectTimeout=5', '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=2',
                '-o', 'ControlMaster=no', '-o', 'ControlPath=none',
                '-o', 'UserKnownHostsFile=' + c.known_hosts, '-i', c.ssh_identity,
                '-L', path + ':' + ('127.0.0.1:' + str(c.worker_port) if c.worker_port else c.socket_path), c.ssh_host,
                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            async with asyncio.timeout(8):
                while not Path(path).exists():
                    if self.tunnel.returncode is not None:
                        raise RemoteError('ssh_unavailable', 'SSH authentication or forwarding failed; inspect configured identity and host enrollment')
                    await asyncio.sleep(.05)
        connection = Connection(path, client_id=c.client_id, private_key=load_private_key(c.client_key),
                                worker_id=c.worker_id, executor=self.executor)
        try:
            await connection.connect()
        except BaseException:
            await connection.close()
            raise
        self.connection = connection
        if self.epoch and self.epoch != connection.epoch:
            self.previous_epoch = self.epoch
            self.cached = None
            for item in self.execution.operations.values():
                if item.get('epoch') == self.epoch:
                    item['state'] = 'unknown'
        self.epoch = connection.epoch
        self.lease = FdLease('remote_rpc', connection, capacity=32, closer_async=close_connection,
                             hard_closer_async=close_connection, close_drain_timeout=5)

    async def request(self, method, params, epoch=None):
        params = dict(params)
        hold = params.pop('_hold_output', False)
        oid = params.get('operation_id', '')
        claim = method in {'submit', 'prepare_privileged'}
        if claim:
            self.execution.claim(oid, epoch or self.epoch, hold)
            if method == 'prepare_privileged':
                self.execution.operations[oid]['unsigned'] = True
        if method == 'commit_privileged' and oid in self.execution.operations:
            self.execution.operations[oid]['unsigned'] = False
        lock = None
        if method == 'session' and params.get('action') not in {'read', 'release', 'terminate'}:
            if any(item['state'] == 'unknown' for item in self.execution.operations.values()):
                raise RemoteError('target_busy', 'Reconcile unknown target operations before another session mutation')
        if method == 'session' and params.get('action') != 'read':
            lock = self.session_locks.setdefault((epoch, params['session_id']), asyncio.Lock())
            if lock.locked():
                raise RemoteError('target_busy', f'Target {self.config.target} session control is busy')
            await lock.acquire()
        control = method == 'session' and params.get('action') not in {'read', 'release'}
        if control:
            self.execution.operations[oid] = dict(operation_id=oid, epoch=epoch, state='submitting',
                session_id=params['session_id'], output_id='', hold_output=False, control=True)
        admitted = False
        try:
            async with self.admission.permit():
                admitted = True
                result = await self._request(method, params, epoch)
            if method == 'prepare_privileged' and oid in self.execution.operations:
                self.execution.operations[oid]['state'] = 'awaiting_approval'
            self.execution.result(oid, result, epoch or self.epoch)
            if method == 'query' and result.get('state') == 'approval_required':
                self.execution.operations.pop(oid, None)
            if control and result.get('state') == 'complete' and not result.get('error'):
                self.execution.operations.pop(oid, None)
            if method in {'session', 'observe'}:
                self.execution.snapshot(result.get('result') or result, epoch or self.epoch)
            if method == 'events':
                for event in result.get('events', ()):
                    self.execution.snapshot(event['result'], epoch or self.epoch)
            if method == 'release':
                self.execution.release(params['output_id'], epoch or self.epoch)
            return result
        except RemoteError as exc:
            if claim or control or method == 'commit_privileged':
                self.execution.failed(oid, exc.effect)
            raise
        except BaseException:
            if claim or control or method == 'commit_privileged':
                self.execution.failed(oid, 'unknown' if admitted else 'not_started')
            raise
        finally:
            if lock is not None:
                lock.release()
                self.session_locks.pop((epoch, params['session_id']), None)

    async def _request(self, method, params, epoch=None):
        async with self.lock:
            if self.closed:
                raise RemoteError('backend_unavailable', 'Target connection has been detached')
            if self.connection and self.connection.closed:
                await self._disconnect()
            if epoch and self.epoch and epoch != self.epoch:
                raise RemoteError('runtime_changed', 'Ticket belongs to another Runtime instance', effect='unknown')
            if not self.lease:
                try:
                    await self._connect()
                except (OSError, TimeoutError, ValueError) as exc:
                    await self._disconnect()
                    raise RemoteError('connection_unavailable',
                        'Connection setup failed before command submission; inspect SSH and enrolled identity') from exc
                except BaseException:
                    await self._disconnect()
                    raise
            if epoch and epoch != self.epoch:
                raise RemoteError('runtime_changed', 'Ticket belongs to another Runtime instance', effect='unknown')
            lease, connection = self.lease, self.connection
            task = asyncio.current_task()
            loop = asyncio.get_running_loop()
            try:
                capability = lease.acquire(operation_id=method,
                    interrupt=lambda connection, reason: loop.call_soon_threadsafe(task.cancel))
            except FdLeaseInvariantError as exc:
                raise RemoteError('transport_unavailable',
                    'Transport admission changed before sending the request') from exc
        try:
            result = await capability.call_async(lambda c: c.request(method, params,
                timeout_ms=max(35000, int(params.get('wait_ms') or 0) + 5000)))
            self.last_error = ''
            return result
        finally:
            await capability.release_async(reuse=not connection.closed)
            if connection.closed:
                async with self.lock:
                    if self.lease is lease:
                        await self._disconnect()

    async def describe(self, refresh=False):
        reachable = None
        if refresh:
            try:
                self.cached = await self.request('metadata', {'refresh': True})
                reachable = True
                if not self.cached.get('draining'):
                    self.expected_offline = False
            except RemoteError as exc:
                reachable = False
                self.last_error = exc.code
        power = (self.cached or {}).get('power')
        shutdown = None if power is None else bool(power.get('shutdown') and power.get('policy') != 'disabled')
        return {'target': self.config.target, 'name': self.config.name, 'shortcut': self.config.shortcut, 'static': self.config.static,
                'dynamic': self.cached, 'reachable': reachable, 'probe_error': self.last_error,
                'management': {
                    'start': {'supported': bool(self.config.start_actions),
                              'reason': '' if self.config.start_actions else 'Start is not supported: no startup action is configured'},
                    'shutdown': {'supported': shutdown,
                                 'reason': 'Shutdown support has not been probed' if shutdown is None else
                                           ('' if shutdown else 'Shutdown is not supported by this target'),
                                 'observed_at': (self.cached or {}).get('observed_at')},
                },
                'execution': self.execution.status(), 'runtime_changed': self.previous_epoch is not None,
                'expected_offline': self.expected_offline, 'start_actions': list(self.config.start_actions),
                'needs_wake': self.expected_offline or (reachable is False and bool(self.config.start_actions))}

    async def start(self, action):
        async with self.lock:
            argv = self.config.start_actions.get(action)
            if argv is None:
                raise RemoteError('unsupported_start', 'No such configured start action')
            process = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            try:
                code = await asyncio.wait_for(process.wait(), 30)
            except TimeoutError:
                process.kill()
                await process.wait()
                raise RemoteError('start_unknown', 'Start action timed out; inspect target before retrying', effect='unknown')
            self.expected_offline = False
            return {'status': 'start_action_completed', 'returncode': code, 'target': self.config.target}

    async def close(self):
        self.closed = True
        self.admission.close()
        async with self.lock:
            await self._disconnect()
        if self.owns_executor:
            await self.executor.close()
