"""Resident target/session ownership; optional plugins supply only a backend port."""
from __future__ import annotations

from pal.shared.diagnostics import exception_diagnostic, diagnostic_text

import asyncio
from dataclasses import dataclass
import itertools
import os
import shlex
import sys
import time
from pathlib import Path
import tempfile
import weakref
from uuid import uuid4

from .adapter import ShellRuntime, ShellRejected, Completion, TERMINAL
from pal_shell_contracts import RemoteFailure
from pal_shell_contracts.remote_lifecycle import can_adopt, poll_decision, snapshot_is_older
from .recovery import LOGGER, retry_read


@dataclass
class Ticket:
    target: int
    epoch: str
    operation_id: str
    origin_turn: str
    native_id: int = 0
    output_id: str = ''
    public_id: int = 0
    kind: str = 'execution'
    latest_result: dict | None = None


class ShellRouter(ShellRuntime):
    def __init__(self, *, owner, **kwargs):
        super().__init__(**kwargs)
        self.owner = owner
        self.remote_ids = itertools.count(1 << 48)
        self.tickets = {}
        self.operations = {}
        self.remote_completions = {}
        self.cursors = {}
        self.poll_task = None
        self._retirement_retry_targets = set()
        self.cache = tempfile.TemporaryDirectory(prefix='pal-shell-output-')
        self.cache_files = {}
        self.cache_sizes = {}
        self.materialize_locks = weakref.WeakValueDictionary()
        self.poll_targets = {}
        self.remote_foreground = set()
        self.operation_context = {}
        self.observation_support = {}
        self.worker_epochs = {}
        self.retired_worker_epochs = {}
        self.rpc_sequence = itertools.count(1)

    async def observe(self, session_id):
        """Best-effort background read; never allocate a control operation."""
        if session_id < 1 << 48:
            return {**await super().session_snapshot(session_id, wait_ms=0), 'target': 0}
        ticket = self.tickets.get(session_id)
        if ticket is None:
            return None
        key = (ticket.target, ticket.epoch)
        if key not in self.observation_support:
            self.observation_support = {k: v for k, v in self.observation_support.items()
                                        if k[0] != ticket.target}
            metadata = await self._rpc(ticket, 'metadata', {})
            self.observation_support[key] = 'observe' in metadata.get('observation_methods', ())
        if not self.observation_support[key]:
            return None  # Old workers continue publishing normal events.
        try:
            result = await self._rpc(ticket, 'observe', {'session_id': ticket.native_id})
        except RemoteFailure as exc:
            if exc.code == 'unknown_method':
                self.observation_support[key] = False
                return None
            raise
        return self._adopt(ticket, result)

    @property
    def remote_work(self):
        return self.execution_work

    @property
    def execution_work(self):
        return any(self.owner.sessions.get(sid, {}).get("latest_status") not in TERMINAL
                   for sid in self.tickets) or any(t.kind == "execution" and not t.native_id and not t.output_id
                                                 for t in self.operations.values()) or any(
                       c.get('control') and not c.get('read_only') and not c.get('resolved')
                       for c in self.operation_context.values()) or any(
                       c.get('transient') and c.get('origin_turn') in self.owner.retiring_transient_turns
                       for oid, c in self.operation_context.items() if oid in self.operations)

    def _port(self):
        if self.owner.remote_port is None:
            raise RemoteFailure('backend_unavailable', 'Remote plugin unavailable; attach it to access retained tickets')
        return self.owner.remote_port

    def _observe_worker_epoch(self, target, epoch, sequence):
        observed = self.worker_epochs.get(target)
        retired = self.retired_worker_epochs.setdefault(target, set())
        if epoch not in retired and (observed is None or observed[1] is None or sequence >= observed[0]):
            if observed and observed[1] is not None and observed[1] != epoch:
                retired.add(observed[1])
            self.worker_epochs[target] = (max(sequence, observed[0] if observed else 0), epoch)

    async def list_targets(self, refresh=False, *, target=None):
        sequence = next(self.rpc_sequence)
        port = self._port()
        items = await port.list(refresh, target=target)
        if port is not self.owner.remote_port:
            raise RemoteFailure('backend_unavailable', 'Remote plugin changed during target inspection')
        for item in items:
            epoch = (item.get('dynamic') or {}).get('runtime_epoch')
            if item.get('reachable') is True and epoch:
                self._observe_worker_epoch(item['target'], epoch, sequence)
        return items

    async def _rpc(self, ticket, method, params):
        sequence = next(self.rpc_sequence)
        try:
            port = self._port()
            result = await port.request(ticket.target, method, params, ticket.epoch or None)
        except RemoteFailure as exc:
            if (ticket.origin_turn in self.owner.retiring_transient_turns and exc.code in {
                'transport_lost', 'transport_timeout', 'transport_closed', 'transport_unavailable',
                'backend_unavailable', 'connection_unavailable',
            }):
                self._retirement_retry_targets.add((ticket.target, ticket.epoch))
            observed = self.worker_epochs.get(ticket.target)
            if exc.code == 'runtime_changed' and (observed is None or observed[1] in {None, ticket.epoch}):
                self.retired_worker_epochs.setdefault(ticket.target, set()).add(ticket.epoch)
                self.worker_epochs[ticket.target] = (max(sequence, observed[0] if observed else 0), None)
            # A read's NOT_STARTED describes only that read, never the command
            # whose receipt/output we are fetching. Preserve its recovery ticket.
            if (method == 'query' and exc.effect == 'not_started') or method == 'output':
                raise RemoteFailure(exc.code, str(exc), operation_id=ticket.operation_id,
                                    effect='applied' if method == 'output' else 'unknown') from exc
            raise
        if self._closed or self.owner.closed or port is not self.owner.remote_port:
            raise RemoteFailure('backend_unavailable', 'Remote plugin changed while awaiting response', effect='unknown')
        if method == 'metadata' and result.get('runtime_epoch'):
            self._observe_worker_epoch(ticket.target, result['runtime_epoch'], sequence)
        return result

    def resume(self):
        if not self._closed and self.owner.remote_port and self.remote_work and (self.poll_task is None or self.poll_task.done()):
            self.poll_task = self.loop.create_task(self._poll())

    def _adopt(self, ticket, result):
        result = dict(result)
        observed = self.worker_epochs.get(ticket.target)
        same_epoch = ((not ticket.epoch or result['runtime_epoch'] == ticket.epoch)
                      and (observed is None or observed[1] == result['runtime_epoch']))
        if not same_epoch:
            raise RemoteFailure('runtime_changed', 'Remote result belongs to another Runtime', effect='unknown')
        if not can_adopt(ticket.operation_id in self.operations,
                         ticket.public_id in self._consumed,
                         same_epoch):
            # A concurrent output acknowledgment can win against a read/control
            # reply. Settle that control without recreating the retired session.
            if self.operation_context.get(ticket.operation_id, {}).get('control'):
                self.operations.pop(ticket.operation_id, None)
                self.operation_context.pop(ticket.operation_id, None)
            raise RemoteFailure('output_released', 'Session output was released while awaiting this response', effect='applied')
        current = self.tickets.get(ticket.public_id)
        previous = (current.latest_result if current else None) or ticket.latest_result
        if previous and snapshot_is_older(result, previous):
            result = dict(previous)
        ticket.latest_result = dict(result)
        ticket.epoch = result['runtime_epoch']
        self.operation_context.setdefault(ticket.operation_id, {}).update(
            resolved=True, latest_status=result['status'])
        ticket.output_id = result.get('output_id', '')
        if result['session_id']:
            ticket.native_id = result['session_id']
            if not ticket.public_id:
                ticket.public_id = next(self.remote_ids)
            self.tickets[ticket.public_id] = ticket
        result.update(target=ticket.target, session_id=ticket.public_id,
                      operation_id=ticket.operation_id,
                      output_id=f'{ticket.target}:{ticket.epoch}:{ticket.output_id}')
        self.resume()
        return result

    async def _outcome(self, ticket, response):
        if response['state'] == 'approval_required':
            self.operations.pop(ticket.operation_id, None)
            self.operation_context.pop(ticket.operation_id, None)
            raise RemoteFailure('authorization_required', 'Execution has not started; request a new command approval')
        if response['state'] == 'pending':
            response = await self._rpc(ticket, 'query', {'operation_id': ticket.operation_id, 'wait_ms': 300000})
        if response.get('error'):
            error = response['error']
            if error.get('effect', 'unknown') == 'not_started':
                self.operations.pop(ticket.operation_id, None)
            raise RemoteFailure(error['code'], error['message'], effect=error.get('effect', 'unknown'), operation_id=ticket.operation_id)
        if response['state'] != 'complete' or response.get('result') is None:
            raise RemoteFailure('execution_unknown', 'Execution remains unresolved; query this operation', effect='unknown', operation_id=ticket.operation_id)
        return self._adopt(ticket, response['result'])

    async def run(self, cmd, *, target=0, sudo=False, delivery_context=None, **kwargs):
        if type(target) is not int or target < 0:
            raise ShellRejected('invalid_target: target must be a non-negative integer')
        from .inputs import validate_stdin
        try:
            validate_stdin(kwargs.get('stdin'))
        except ValueError as exc:
            raise ShellRejected(f'invalid_stdin: {exc}') from exc
        if kwargs.get('stdin') is not None and kwargs.get('tty'):
            raise ShellRejected('stdin_pty_conflict: stdin requires tty=false')
        if target == 0:
            if sudo:
                return await self.local_privileged(cmd, **kwargs)
            return {**await super().run(cmd, **kwargs), 'target': 0}
        if sudo:
            if kwargs.get('tty'):
                raise RemoteFailure('privilege_pty_unsupported', 'A single-command sudo grant cannot open a privileged PTY')
            return await self.privileged(target, 'sudo', cmd=cmd, delivery_context=delivery_context, **kwargs)
        self._port()  # Reject before allocating any request when the plugin is absent.
        await self.sync_target(target)
        ticket = Ticket(target, '', uuid4().hex, kwargs.get('turn_id', ''))
        self.operations[ticket.operation_id] = ticket
        self._capture_context(ticket, cmd, kwargs.get('tty', False), delivery_context)
        submitted = False
        task = asyncio.current_task()
        self.remote_foreground.add(task)
        try:
            metadata = await self._rpc(ticket, 'metadata', {})
            ticket.epoch = metadata['runtime_epoch']
            self._require_privilege_gate(metadata)
            self._require_stdin(metadata, kwargs.get('stdin'))
            args = {k: kwargs[k] for k in ('cwd', 'tty', 'timeout_ms') if kwargs.get(k) is not None}
            if kwargs.get('stdin') is not None:
                args['stdin'] = kwargs['stdin']
            args['wait_ms'] = kwargs.get('wait_ms')
            if args['wait_ms'] is None:
                args['wait_ms'] = 1000 if args.get('tty') else 300000
            submitted = True
            self.operation_context[ticket.operation_id]['submitted'] = True
            args['_hold_output'] = self.owner.require_output_delivery or bool((delivery_context or {}).get('transient'))
            response = await self._rpc(ticket, 'submit', {'operation_id': ticket.operation_id, 'cmd': cmd, **args})
            result = await self._outcome(ticket, response)
            return await self.materialize(result) if kwargs.get('load_output', True) else result
        except RemoteFailure as exc:
            exc.operation_id = ticket.operation_id
            if not submitted:
                exc.effect = 'not_started'
            if exc.effect == 'not_started':
                self.operations.pop(ticket.operation_id, None)
                self.operation_context.pop(ticket.operation_id, None)
                exc.operation_id = ''
            raise
        except asyncio.CancelledError:
            if not submitted:
                self.operations.pop(ticket.operation_id, None)
                self.operation_context.pop(ticket.operation_id, None)
            raise
        except Exception as exc:
            if not submitted:
                self.operations.pop(ticket.operation_id, None)
                self.operation_context.pop(ticket.operation_id, None)
                raise RemoteFailure('remote_prepare_failed',
                    'Target preparation failed before command submission; inspect worker compatibility; cause: ' + exception_diagnostic(exc)) from exc
            raise
        finally:
            self.remote_foreground.discard(task)
            self.resume()

    @staticmethod
    def _require_privilege_gate(metadata):
        if (metadata.get('os', {}).get('family') == 'linux'
                and metadata.get('privilege_gate_version') != 1):
            raise RemoteFailure('native_incompatible',
                'Update this Linux worker to the matching build with the privilege gate before submitting commands')

    @staticmethod
    def _require_stdin(metadata, stdin, *, privileged=False):
        if stdin is None:
            return
        supported = metadata.get('stdin_version') == 1
        if privileged:
            supported = supported and metadata.get('privilege', {}).get('stdin_version') == 1
        if not supported:
            raise RemoteFailure('stdin_unsupported',
                'Update this worker and, for sudo, its protected helper to support stdin')

    async def local_privileged(self, cmd, **kwargs):
        if not sys.platform.startswith('linux'):
            raise ShellRejected('sudo_unsupported: local privilege approval currently requires Linux')
        if kwargs.get('tty'):
            raise ShellRejected('privilege_pty_unsupported: an approved command cannot open a privileged PTY')
        self._check_stdin(kwargs.get('stdin'))
        if self._closed or self._closing is not None or self.owner.closed:
            raise ShellRejected('shell runtime is closed')
        cwd = os.path.abspath(os.path.expanduser(kwargs.get('cwd') or os.getcwd()))
        args = {'action': 'sudo', 'cmd': cmd, 'cwd': cwd}
        if kwargs.get('stdin') is not None:
            args['stdin'] = kwargs['stdin']
        approval = {'operation_id': uuid4().hex, 'worker_id': 'local host',
                    'runtime_epoch': self.epoch, 'expires_at': time.time() + 600}
        task = asyncio.current_task()
        # Include approval waiting in turn interruption and runtime close.
        self._foreground[task] = kwargs.get('turn_id', '')
        try:
            await self.owner.approvals.request(kwargs.get('turn_id', ''), 0, args, approval)
            if time.time() >= approval['expires_at']:
                raise RemoteFailure('approval_expired', 'Approval expired before execution')
            # Approval is not a sudo password. Do not open an authentication PTY
            # or capture a password; the OS may still deny noninteractive sudo.
            command = 'exec ' + shlex.join(['/usr/bin/sudo', '-n', '--', '/bin/sh', '-c', cmd])
            options = {**kwargs, 'cwd': cwd, 'shell': '/bin/sh', '_authorized': True}
            return {**await super().run(command, **options), 'target': 0}
        finally:
            self._foreground.pop(task, None)

    def _capture_context(self, ticket, cmd, tty, context=None):
        core = self.owner.core
        continuation = core.state.active_turns.get(ticket.origin_turn) if core else None
        self.operation_context[ticket.operation_id] = {
            'origin_turn': ticket.origin_turn, 'binding': getattr(continuation, 'delivery_binding', None),
            'budget': None, 'cmd': cmd, 'tty': tty, 'target': ticket.target, 'committed': False, **(context or {}),
        }

    async def privileged(self, target, action, *, cmd='', delivery_context=None, **kwargs):
        port = self._port()
        await self.sync_target(target)
        ticket = Ticket(target, '', uuid4().hex, kwargs.get('turn_id', ''), kind=action if action == 'shutdown' else 'execution')
        self.operations[ticket.operation_id] = ticket
        self._capture_context(ticket, cmd, False, delivery_context)
        self.operation_context[ticket.operation_id].update(privileged=True, signed=False)
        task = asyncio.current_task()
        self.remote_foreground.add(task)
        submitted = False
        try:
            metadata = await self._rpc(ticket, 'metadata', {'refresh': True} if kwargs.get('stdin') is not None else {})
            ticket.epoch = metadata['runtime_epoch']
            self._require_privilege_gate(metadata)
            self._require_stdin(metadata, kwargs.get('stdin'), privileged=True)
            args = {'action': action, 'target': target}
            if action == 'sudo':
                args.update(cmd=cmd, cwd=kwargs.get('cwd', ''), wait_ms=kwargs.get('wait_ms') if kwargs.get('wait_ms') is not None else 300000,
                            timeout_ms=kwargs.get('timeout_ms'))
                if kwargs.get('stdin') is not None:
                    args['stdin'] = kwargs['stdin']
            else:
                identity = await port.call('identity', {})
                args['protected_machine_id'] = identity['machine_identity']
            self.operation_context[ticket.operation_id]['submitted'] = True
            prepared = await self._rpc(ticket, 'prepare_privileged', {'operation_id': ticket.operation_id,
                '_hold_output': self.owner.require_output_delivery or bool((delivery_context or {}).get('transient')), **args})
            args = prepared.get('normalized_args', args)
            await self.owner.approvals.request(ticket.origin_turn, target, args, prepared['approval'])
            if port is not self.owner.remote_port:
                raise RemoteFailure('backend_unavailable', 'Approval belongs to the detached plugin generation')
            submitted = True
            self.operation_context[ticket.operation_id]['signed'] = True
            response = await port.call('approve', {'target': target, 'action': action, 'approval': prepared['approval']})
            if action == 'shutdown':
                if response['state'] == 'pending':
                    response = await self._rpc(ticket, 'query', {'operation_id': ticket.operation_id, 'wait_ms': 1000})
                if response.get('error'):
                    error = response['error']
                    raise RemoteFailure(error['code'], error['message'], effect=error.get('effect', 'unknown'))
                if (response.get('result') or {}).get('status') == 'accepted':
                    self.operations.pop(ticket.operation_id, None)
                    self.operation_context.pop(ticket.operation_id, None)
                return {'target': target, 'operation_id': ticket.operation_id, **(response.get('result') or {'status': 'unknown'})}
            result = await self._outcome(ticket, response)
            return await self.materialize(result) if kwargs.get('load_output', True) else result
        except RemoteFailure as exc:
            exc.operation_id = ticket.operation_id
            if not submitted:
                await self.cancel_prepared(ticket)
                exc.effect = 'not_started'
            if exc.effect == 'not_started':
                self.operations.pop(ticket.operation_id, None)
                self.operation_context.pop(ticket.operation_id, None)
                exc.operation_id = ''
            raise
        except asyncio.CancelledError:
            if not submitted:
                await asyncio.shield(self.cancel_prepared(ticket))
                self.operations.pop(ticket.operation_id, None)
                self.operation_context.pop(ticket.operation_id, None)
            raise
        except Exception as exc:
            if not submitted:
                await self.cancel_prepared(ticket)
                self.operations.pop(ticket.operation_id, None)
                self.operation_context.pop(ticket.operation_id, None)
                raise RemoteFailure('privilege_prepare_failed',
                    'Privilege preparation failed before execution; inspect target compatibility and approval setup') from exc
            raise
        finally:
            self.remote_foreground.discard(task)
            self.resume()

    def execution_records(self, target):
        records = []
        for ticket in self.operations.values():
            if ticket.target != target:
                continue
            context = self.operation_context.get(ticket.operation_id, {})
            hold_output = self.owner.require_output_delivery or bool(context.get('transient'))
            if not context.get('submitted') and not ticket.native_id and not ticket.output_id:
                continue
            if context.get('read_only'):
                continue
            if context.get('control'):
                if context.get('resolved'):
                    continue
                state = 'unknown'
            else:
                state = self.owner.sessions.get(ticket.public_id, {}).get('latest_status', context.get('latest_status'))
            if context.get('control') and not context.get('resolved'):
                state = 'unknown'
            elif ticket.output_id and (not ticket.native_id or state in TERMINAL):
                if not hold_output:
                    continue
                state = 'delivery'
            else:
                state = 'running' if ticket.native_id and state not in TERMINAL else 'unknown'
            records.append(dict(operation_id=ticket.operation_id, epoch=ticket.epoch,
                state=state, session_id=ticket.native_id, output_id=ticket.output_id,
                hold_output=hold_output, control=bool(context.get('control')),
                kind=('control' if context.get('control') else 'shutdown' if ticket.kind == 'shutdown'
                      else 'privileged' if context.get('privileged') else 'execution'),
                **({'unsigned': not context.get('signed')} if context.get('privileged') else {})))
        return records

    async def sync_target(self, target):
        await self._port().call('restore_execution', {'target': target, 'records': self.execution_records(target)})

    async def cancel_prepared(self, ticket):
        try:
            await self._port().call('cancel_prepared', {'target': ticket.target, 'operation_id': ticket.operation_id})
        except Exception:
            LOGGER.warning('Could not retire unsigned approval target=%s', ticket.target)

    async def reconcile(self, operation_id):
        ticket = self.operations.get(operation_id)
        if ticket is None:
            raise RemoteFailure('invalid_operation', 'No operation owned by this shell scope')
        response = await self._rpc(ticket, 'query', {'operation_id': operation_id, 'wait_ms': 0})
        if response.get('state') == 'pending':
            raise RemoteFailure('execution_unknown', 'Original operation is still unresolved', effect='unknown', operation_id=operation_id)
        if response.get('result', {}) and 'session_id' not in response['result']:
            self.operations.pop(operation_id, None)
            self.operation_context.pop(operation_id, None)
            return {'target': ticket.target, 'operation_id': operation_id, **response['result']}
        return await self._outcome(ticket, response)

    async def reconcile_transient(self, turn_id):
        """Resolve original tickets before retiring output; never resubmit work."""
        errors = []
        for oid, ticket in tuple(self.operations.items()):
            context = self.operation_context.get(oid, {})
            if ticket.origin_turn != turn_id or not context.get('transient'):
                continue
            try:
                if not context.get('resolved'):
                    result = await retry_read(lambda: self.reconcile(oid), stage='transient_query', identity=oid)
                elif context.get('control'):
                    continue  # The original output release retires resolved controls.
                elif ticket.latest_result is not None:
                    result = self._adopt(ticket, ticket.latest_result)
                else:
                    raise RemoteFailure('execution_unknown', 'Temporary execution has no confirmed result',
                                        effect='unknown', operation_id=oid)
                if result.get('session_id'):
                    session = self.owner.sessions.setdefault(result['session_id'], dict(context))
                    session['latest_status'] = result['status']
                elif result.get('output_id'):
                    await retry_read(lambda: self.release_output(result), stage='transient_release', identity=oid)
                    self.owner.release_input_lease(context.get('input_lease_id'))
            except RemoteFailure as exc:
                if exc.code == 'output_released' and oid not in self.operations:
                    continue
                errors.append(exc)
            except Exception as exc:
                errors.append(exc)
        return errors

    async def session_snapshot(self, session_id, *, action='read', **kwargs):
        if session_id < 1 << 48:
            return {**await super().session_snapshot(session_id, action=action, **kwargs), 'target': 0}
        ticket = self.tickets.get(session_id)
        if ticket is None:
            raise ShellRejected('invalid_session: unknown remote session in this caller scope')
        if action == 'release':
            await self.release_output({'operation_id': ticket.operation_id, 'target': ticket.target, 'session_id': session_id})
            return {'session_id': session_id, 'target': ticket.target, 'status': 'released'}
        operation_id = uuid4().hex
        control = Ticket(ticket.target, ticket.epoch, operation_id, ticket.origin_turn,
                         ticket.native_id, ticket.output_id, ticket.public_id)
        self.operations[operation_id] = control
        self.operation_context[operation_id] = {
            **self.operation_context.get(ticket.operation_id, self.owner.sessions.get(session_id, {})),
            'control': True, 'submitted': True, 'resolved': False, 'read_only': action == 'read',
        }
        try:
            response = await self._rpc(control, 'session', {'operation_id': operation_id, 'session_id': ticket.native_id,
                'action': action, **{k: v for k, v in kwargs.items() if v is not None}})
            result = await self._outcome(control, response)
            if action in {'watch', 'unwatch'}:
                previous = self.remote_completions.get(session_id)
                if previous and previous.result.get('watch_generation', 0) < result.get('watch_generation', 0):
                    self.remote_completions.pop(session_id, None)
            return result
        except RemoteFailure as exc:
            exc.operation_id = operation_id
            if exc.effect == 'not_started':
                self.operations.pop(operation_id, None)
                self.operation_context.pop(operation_id, None)
            raise

    async def materialize(self, event, *, load_output=True):
        if not event.get('target'):
            return await super().materialize(event, load_output=load_output)
        ticket = self.operations[event['operation_id']]
        async with self.materialize_locks.setdefault(event['output_id'], asyncio.Lock()):
            total = sum(event.get(stream + '_total', 0) for stream in ('stdout', 'stderr'))
            key = event['output_id']
            if total > 8 * 1024 * 1024 or total < 0 or sum(self.cache_sizes.values()) - self.cache_sizes.get(key, 0) + total > 64 * 1024 * 1024:
                raise RemoteFailure('output_capacity', 'Local output cache full; deliver or release retained output', effect='applied')
            self.cache_sizes[key] = max(total, self.cache_sizes.get(key, 0))
            result = dict(event)
            for stream in ('stdout', 'stderr'):
                files = self.cache_files.setdefault(key, [])
                path = next((p for p in files if p.suffix == '.' + stream), None)
                if path is None:
                    path = Path(self.cache.name) / (uuid4().hex + '.' + stream)
                    path.touch(mode=0o600, exist_ok=False)
                    files.append(path)
                size = event.get(stream + '_total', 0)
                with path.open('ab') as file:
                    offset = file.tell()
                    while offset < size:
                        chunk = await self._rpc(ticket, 'output', {'snapshot': event['snapshot'], 'stream': stream,
                            'offset': offset, 'length': min(256 * 1024, size - offset)})
                        data = chunk['data']
                        if (not isinstance(data, bytes) or not data or len(data) > min(256 * 1024, size-offset)
                            or chunk.get('offset', offset) != offset or chunk.get('total', size) != size):
                            raise RemoteFailure('invalid_output', 'Remote output violated snapshot bounds', effect='applied')
                        file.write(data)
                        file.flush()  # Retain validated bytes across a later RPC failure.
                        offset += len(data)
                result[stream + "_path"] = str(path)
                if not load_output:
                    continue
                def prefix(path=path, size=size):
                    with path.open('rb') as file:
                        return file.read(size)
                raw = await asyncio.to_thread(prefix)
                result[stream + '_bytes'] = raw
                result[stream] = raw.decode('utf-8', errors='replace')
            return result

    async def release_output(self, result):
        if not result.get('target'):
            return await super().release_output(result)
        ticket = self.operations.get(result['operation_id'])
        if ticket is None:
            return  # Already acknowledged locally; do not repeat the model delivery.
        await self._rpc(ticket, 'release', {'output_id': ticket.output_id})
        self.forget_output(result)

    def forget_output(self, result):
        if not result.get('target'):
            return super().forget_output(result)
        ticket = self.operations.get(result['operation_id'])
        if ticket is None:
            return
        output_key = f'{ticket.target}:{ticket.epoch}:{ticket.output_id}'
        self.cache_sizes.pop(output_key, None)
        for path in self.cache_files.pop(output_key, ()):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                LOGGER.exception('shell output cache removal failed output=%s', output_key)
        for op, item in list(self.operations.items()):
            if (item.target, item.epoch, item.output_id) == (ticket.target, ticket.epoch, ticket.output_id):
                context = self.operation_context.get(op, {})
                if context.get('control') and not context.get('resolved'):
                    continue
                self.operations.pop(op)
                self.operation_context.pop(op, None)
        self.tickets.pop(ticket.public_id, None)
        if ticket.public_id:
            self._mark_consumed(ticket.public_id)
        self.remote_completions.pop(ticket.public_id, None)

    async def _poll_target(self, target, epoch):
        reachable = False
        while not self._closed and self.owner.remote_port:
            ticket = next((t for t in self.operations.values() if (t.target, t.epoch) == (target, epoch)), None)
            if ticket is None:
                return
            try:
                response = await self._rpc(ticket, 'events', {'after': self.cursors.get((target, epoch), 0), 'wait_ms': 0})
                if not reachable or (target, epoch) in self._retirement_retry_targets:
                    self._retirement_retry_targets.discard((target, epoch))
                    for execution in tuple(self.operations.values()):
                        if (execution.target, execution.epoch) == (target, epoch):
                            self.owner.queue_transient_retirement(execution.origin_turn)
                reachable = True
                for event in response['events']:
                    result = event['result']
                    execution_id = result.get('execution_id')
                    owner = self.operations.get(execution_id) if execution_id else next((
                        t for t in self.operations.values() if t.target == target and t.epoch == epoch
                        and result['session_id'] and t.native_id == result['session_id']
                        and not self.operation_context.get(t.operation_id, {}).get('control')), None)
                    if owner and ((owner.target, owner.epoch) != (target, epoch)
                                  or self.operation_context.get(owner.operation_id, {}).get('control')):
                        owner = None
                    decision = poll_decision(owner is not None, bool(execution_id))
                    if decision == 'defer':
                        break  # Native ownership may still be getting assigned.
                    if decision == 'skip':
                        # All Pal tickets exist before submission. An event for
                        # an unowned execution must not starve later owned ones.
                        self.cursors[(target, epoch)] = event['cursor']
                        continue
                    adopted = self._adopt(owner, result)
                    if owner.origin_turn in self.owner.retiring_transient_turns:
                        if owner.public_id:
                            observed = self.owner.sessions.setdefault(owner.public_id,
                                dict(self.operation_context.get(owner.operation_id, {})))
                            observed['latest_status'] = adopted['status']
                        self.owner.queue_transient_retirement(owner.origin_turn)
                        self.cursors[(target, epoch)] = event['cursor']
                        continue
                    observed = self.owner.sessions.setdefault(owner.public_id,
                        dict(self.operation_context.get(owner.operation_id, {})))
                    observed['latest_status'] = adopted['status']
                    if (owner.public_id not in self._consumed
                        and (adopted['status'] in TERMINAL or (adopted.get('watching', True)
                        and observed.get('watching', True)
                        and adopted.get('watch_generation', 0) >= observed.get('watch_generation', 0)))):
                        self.remote_completions[owner.public_id] = Completion(owner.public_id, owner.origin_turn, adopted)
                        self.owner.notify()
                    self.cursors[(target, epoch)] = event['cursor']
            except RemoteFailure:
                reachable = False
                pass  # Reachability is not execution state. Tickets remain resident.
            await asyncio.sleep(1)

    async def _poll(self):
        try:
            while not self._closed and self.owner.remote_port and self.remote_work:
                groups = {(t.target, t.epoch) for t in self.tickets.values()}
                groups.update((t.target, t.epoch) for t in self.operations.values()
                    if t.epoch and t.kind == 'execution'
                    and self.operation_context.get(t.operation_id, {}).get('submitted')
                    and not self.operation_context.get(t.operation_id, {}).get('control'))
                groups.update((t.target, t.epoch) for t in self.operations.values()
                    if t.epoch and t.origin_turn in self.owner.retiring_transient_turns)
                for key in groups:
                    if key not in self.poll_targets or self.poll_targets[key].done():
                        self.poll_targets[key] = self.loop.create_task(self._poll_target(*key))
                for key in set(self.poll_targets) - groups:
                    task = self.poll_targets.pop(key)
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                await asyncio.sleep(1)
        finally:
            tasks = list(self.poll_targets.values())
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self.poll_targets.clear()

    def drain_completions(self):
        result = super().drain_completions()
        result.extend(self.remote_completions.values())
        self.remote_completions.clear()
        return result

    async def terminate(self, session_id):
        if session_id < 1 << 48:
            return await super().terminate(session_id)
        return await self.session_snapshot(session_id, action='terminate')

    async def _discard_cancelled_session(self, session_id):
        if session_id >= 1 << 48:
            return  # Remote cancellation must be reconciled, never discard its ticket.
        return await super()._discard_cancelled_session(session_id)

    def close_idle(self):
        if self.remote_work or self.remote_foreground:
            raise ShellRejected("execution_busy: remote operations are still active")
        super().close_idle()
        # No retained tickets remain; this task can only be finishing a read-only poll.
        if self.poll_task and not self.poll_task.done():
            self.loop.call_soon_threadsafe(self.poll_task.cancel)
        self.cache.cleanup()

    async def close(self):
        remote_tasks = [task for task in self.remote_foreground if task is not asyncio.current_task()]
        for task in remote_tasks:
            task.cancel()
        await asyncio.gather(*remote_tasks, return_exceptions=True)
        if self.poll_task:
            self.poll_task.cancel()
            await asyncio.gather(self.poll_task, return_exceptions=True)
        await super().close()
        self.cache.cleanup()
