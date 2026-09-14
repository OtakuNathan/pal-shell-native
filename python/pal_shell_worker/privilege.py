"""Single-use signed approval, independent of a transport or a sudo timestamp."""
from __future__ import annotations

import asyncio
import base64
import os
from pathlib import Path
import secrets
import shlex
import time

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .protocol import RemoteError, canonical, text, TERMINAL


def trusted_executable(path):
    p = Path(path)
    if not p.is_absolute():
        return False
    try:
        for part in (p, *p.parents):
            s = part.lstat()
            if part.is_symlink() or s.st_uid != 0 or s.st_mode & 0o022:
                return False
        return p.is_file() and os.access(p, os.X_OK)
    except OSError:
        return False


def approval_payload(worker, op):
    return {'protocol': 'pal-shell-approval.v1', 'client_id': worker.config.client_id,
            'worker_id': worker.config.worker_id, 'runtime_epoch': worker.epoch,
            'operation_id': op.operation_id, 'fingerprint': op.fingerprint,
            'nonce': op.nonce, 'expires_at': op.expires_at, 'target': op.args['target']}


async def execute_privileged(worker, op):
    try:
        args = op.args
        if args['action'] == 'shutdown':
            # Admission and the busy check have no intervening await.
            worker.draining = True
            if worker.reservations or worker.outputs or any(x.state == 'pending' and x is not op for x in worker.operations.values()):
                worker.draining = False
                raise RemoteError('target_busy', 'Active tasks, unresolved operations or undelivered outputs prevent shutdown')
            try:
                process = await asyncio.create_subprocess_exec(*worker.config.shutdown_argv,
                    stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            except OSError:
                worker.draining = False
                raise RemoteError('shutdown_not_started', 'Configured shutdown action could not start')
            worker._finish(op, {'status': 'accepted', 'operation_id': op.operation_id,
                                'runtime_epoch': worker.epoch, 'power_state': 'unconfirmed'})
            code = await process.wait()
            if code:
                worker.draining = False
                worker._finish(op, error=RemoteError('shutdown_failed', 'Configured shutdown action failed', effect='applied'))
            return
        worker.reservations.add(op.operation_id)
        # Only the trusted sudo process reads askpass output. It never shares
        # authentication stdin with the approved child command, even on cache hits.
        envelope = {'approval': approval_payload(worker, op), 'args': op.args, 'signature': op.approval_signature}
        grant = base64.urlsafe_b64encode(canonical(envelope)).decode()
        command = 'exec ' + shlex.join(['/usr/bin/env', 'SUDO_ASKPASS=' + worker.config.askpass_helper,
            'PAL_SHELL_APPROVAL=' + grant,
            '/usr/bin/sudo', '-A', '-k', '--', worker.config.privilege_helper,
            worker.config.shell, args['cmd']])
        result = await worker._native('run_limited', worker.config.shell, command, args.get('cwd', ''),
            False, args.get('wait_ms', 300000), args.get('timeout_ms') or 0, 0, worker.config.output_limit)
        worker.native_owners[result['output_id']] = op.operation_id
        snapshot = worker._snapshot(result)
        if result['output_id'] in worker.events:
            worker.events[result['output_id']]['result']['execution_id'] = op.operation_id
        worker._finish(op, snapshot)
    except RemoteError as exc:
        worker.reservations.discard(op.operation_id)
        worker._finish(op, error=exc)
    except Exception:
        worker._finish(op, error=RemoteError('execution_unknown', 'Privileged operation outcome is unknown', effect='unknown', operation_id=op.operation_id))


async def handle_privileged(worker, method, args):
    if os.name == 'nt':
        raise RemoteError('management_unsupported', 'Windows target does not support power or privilege management')
    if method == 'prepare_privileged':
        params = {k: v for k, v in args.items() if k != 'operation_id'}
        action = params.get('action')
        if action not in {'sudo', 'shutdown'} or set(params) - {'action', 'target', 'cmd', 'cwd', 'wait_ms', 'timeout_ms', 'protected_machine_id'}:
            raise RemoteError('invalid_request', 'Unsupported privilege request')
        from .protocol import integer
        integer(params.get('target'), 'target', 1)
        if action == 'sudo':
            text(params.get('cmd'), 'cmd', limit=32768)
            text(params.get('cwd', ''), 'cwd', empty=True)
            integer(params.get('wait_ms', 300000), 'wait_ms', 0, 300000)
            integer(params.get('timeout_ms') or 0, 'timeout_ms')
            if not (trusted_executable(worker.config.privilege_helper) and trusted_executable(worker.config.askpass_helper)):
                raise RemoteError('privilege_unavailable', 'Install the protected privilege and authentication helpers on this target')
        else:
            if not worker.config.shutdown_argv or worker.config.shutdown_policy == 'disabled':
                raise RemoteError('shutdown_unsupported', 'Target does not permit shutdown')
            own_machine = worker.info.get('machine_identity')
            protected = set(worker.config.protected_machine_ids) | {params.get('protected_machine_id')}
            if not own_machine or not params.get('protected_machine_id') or own_machine in protected:
                raise RemoteError('protected_host', 'Cannot shut down an unidentified machine or the client host')
        op, fresh = worker._record(args.get('operation_id'), 'privileged', params)
        if fresh:
            op.state = 'approval_required'
            op.nonce, op.expires_at = secrets.token_hex(32), time.time() + 600
        return {**op.snapshot(), 'approval': approval_payload(worker, op)}
    op = worker.operations.get(text(args.get('operation_id'), 'operation_id', limit=128))
    if op is None or op.method != 'privileged':
        raise RemoteError('invalid_operation', 'No privilege preparation for this operation')
    if op.state != 'approval_required':
        return op.snapshot()  # A consumed grant cannot repeat its effect.
    if time.time() > op.expires_at:
        worker._finish(op, error=RemoteError('approval_expired', 'Approval expired before execution'))
        return op.snapshot()
    signature = args.get('signature', '')
    try:
        key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(worker.config.client_public_key))
        key.verify(bytes.fromhex(signature), canonical(approval_payload(worker, op)))
    except Exception as exc:
        raise RemoteError('approval_invalid', 'Single-use client approval signature rejected') from exc
    if worker.draining:
        raise RemoteError('worker_draining', 'Worker is draining')
    if op.args['action'] == 'sudo' and (len(worker.reservations)+1)*worker.config.output_limit > worker.config.retained_limit:
        raise RemoteError('output_capacity', 'Release retained output before approving execution')
    op.approval_signature = signature
    op.state = 'pending'
    # Reserve at admission, not in the asynchronously scheduled executor.
    if op.args['action'] == 'sudo':
        worker.reservations.add(op.operation_id)
    worker._task(execute_privileged(worker, op))
    return op.snapshot()


def consume_authentication(worker, args):
    # This one unauthenticated RPC is usable only with the already signed grant.
    # It returns permission, never a credential. The protected askpass launcher
    # pins this endpoint in its root-owned configuration.
    grant = args.get('approval', {})
    op = worker.operations.get(grant.get('operation_id'))
    if op is None or op.method != 'privileged' or op.args['action'] != 'sudo':
        raise RemoteError('approval_invalid', 'No approved sudo operation')
    if (op.state != 'pending' or op.auth_consumed or time.time() > op.expires_at or
        grant != approval_payload(worker, op)):
        raise RemoteError('approval_invalid', 'Authentication grant is unavailable or consumed')
    try:
        key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(worker.config.client_public_key))
        key.verify(bytes.fromhex(args['signature']), canonical(grant))
    except Exception as exc:
        raise RemoteError('approval_invalid', 'Authentication grant signature rejected') from exc
    op.auth_consumed = True
    return {'authorized': True}
