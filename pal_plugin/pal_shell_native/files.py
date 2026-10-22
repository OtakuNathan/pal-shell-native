"""Pal context references and delivery intent for worker-owned file authority."""
from __future__ import annotations

import asyncio
import base64
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from uuid import uuid4

from pal.execution.contracts import CapabilityResult
from pal.execution.session_state import FileDeliveryManifest
from pal.execution.tool_facade import (EffectOutcome, EffectReceipt, RetryDirective,
                                      ToolExecutionError, ToolRejectedError)
from pal_shell_contracts import RemoteFailure

CHUNK = 128 * 1024
MAX_BLOB = 32 * 1024 * 1024


class RemoteFiles:
    def __init__(self, owner):
        self.owner = owner
        self.scopes = {}
        self.calls = {}
        self.locks = {}
        self.tasks = set()
        self.histories = {}
        self.history_refs = {}
        self.handed_off = False

    @property
    def has_work(self):
        return any(state['unresolved'] or state['pending'] for state in self.scopes.values())

    def reconnect(self):
        for state in self.scopes.values():
            if state.get('acked', -1) != state['revision'] or state.get('garbage') or state.get('receipt_releases'):
                self._schedule_sync(state)

    def detach_histories(self):
        for history, listener in self.histories.values():
            history.remove_change_listener(listener)
        self.histories.clear()
        self.history_refs.clear()

    def unbind_history(self, history):
        previous = self.history_refs.pop(id(history), {})
        pair = self.histories.pop(id(history), None)
        if pair:
            history.remove_change_listener(pair[1])
        for state in self.scopes.values():
            removed = [rid for rid in previous if rid in state['live']
                       and not any(rid in refs for refs in self.history_refs.values())]
            if removed:
                for rid in removed:
                    del state['live'][rid]
                state['revision'] += 1
                self._schedule_sync(state)

    @staticmethod
    def lifetime_for(runtime, turn):
        lifetime = runtime.logical_context_for_turn(turn).execution_lifetime_id
        if getattr(runtime, 'transient', False):
            lifetime = f'transient:{runtime._file_namespace}:{lifetime}'
        return lifetime

    def bind_history(self, history):
        if id(history) in self.histories:
            return
        def references(turns):
            refs = {}
            for turn in turns:
                for message in turn.messages:
                    for result in message.parts:
                        remote = (getattr(result, 'context_delivery', None) or {}).get('remote') or {}
                        if remote.get('scope'):
                            refs[result.call_id] = remote['scope']
                for rid in dict(turn.metadata or {}).get('compact_retained_result_refs', ()):
                    refs.setdefault(str(rid), '')
            return refs
        def changed(old, new):
            previous, current = references(old), references(new)
            self.history_refs[id(history)] = current
            for result_id in previous.keys() - current.keys():
                if any(result_id in refs for refs in self.history_refs.values()):
                    continue
                for state in self.scopes.values():
                    if (not previous[result_id] or state['scope'] == previous[result_id]) and result_id in state['live']:
                        del state['live'][result_id]
                        state['revision'] += 1
                        self._schedule_sync(state)
        history.add_change_listener(changed)
        self.histories[id(history)] = (history, changed)
        self.history_refs[id(history)] = references(history.turns)

    def project(self, runtime, turn):
        lifetime = self.lifetime_for(runtime, turn)
        visible = runtime.execution_sessions.projected_visible_results(turn)
        for state in self.scopes.values():
            if state['lifetime'] == lifetime and not state['closed']:
                self._set_visible(state, visible)

    def _set_visible(self, state, visible):
        projected = sorted(visible) if visible is not None else None
        if state.get('visible') != projected:
            state['visible'] = projected
            state['revision'] += 1
            self._schedule_sync(state)

    async def _request(self, state, method, params):
        port = self.owner.remote_port
        if port is None:
            raise RemoteFailure('backend_unavailable', 'Remote plugin is unavailable')
        result = await port.request(state['target'], method, params, state['epoch'], binding=state['binding'])
        if port is not self.owner.remote_port or port.closed:
            raise RemoteFailure('backend_unavailable', 'Remote plugin changed during file operation', effect='unknown')
        return result

    async def _upload(self, state, value):
        data = json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        if len(data) > MAX_BLOB:
            raise ToolRejectedError('File transfer exceeds 32 MiB', error_code='file_transfer_limit')
        ref = {'blob': uuid4().hex, 'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
        try:
            await self._request(state, 'file_upload_begin', ref)
            for offset in range(0, len(data), CHUNK):
                await self._request(state, 'file_upload_chunk', {'blob': ref['blob'], 'offset': offset,
                    'data': base64.b64encode(data[offset:offset + CHUNK]).decode('ascii')})
        except BaseException:
            await self._release(state, ref)
            raise
        return ref

    async def _release(self, state, ref):
        try:
            await self._request(state, 'file_blob_release', {'blob': ref['blob']})
        except Exception:
            garbage = state.setdefault('garbage', [])
            if ref['blob'] not in garbage:
                garbage.append(ref['blob'])

    async def _flush_releases(self, state):
        for token in tuple(state.get('garbage', ())):
            await self._request(state, 'file_blob_release', {'blob': token})
            state['garbage'].remove(token)
        for oid in tuple(state.get('receipt_releases', ())):
            await self._request(state, 'file_result_release', {'operation_id': oid})
            state['receipt_releases'].remove(oid)

    async def _download(self, state, ref):
        if type(ref.get('size')) is not int or not 0 <= ref['size'] <= MAX_BLOB:
            raise RemoteFailure('invalid_file_blob', 'Invalid file result size', effect='unknown')
        data = bytearray()
        while len(data) < ref['size']:
            chunk = await self._request(state, 'file_blob_read', {'blob': ref['blob'], 'offset': len(data)})
            raw = base64.b64decode(chunk['data'], validate=True)
            if not raw or len(raw) > CHUNK or len(data) + len(raw) > ref['size']:
                raise RemoteFailure('invalid_file_blob', 'Invalid file result chunk', effect='unknown')
            data.extend(raw)
        if hashlib.sha256(data).hexdigest() != ref['sha256']:
            raise RemoteFailure('invalid_file_blob', 'File result digest mismatch', effect='unknown')
        return json.loads(data)

    async def _sync(self, state):
        await self._flush_releases(state)
        # Take one immutable revision before awaiting transport. A concurrent
        # retirement changes the revision and forces another synchronization.
        while state.get('acked', -1) != state['revision']:
            revision = state['revision']
            value = deepcopy({key: state[key] for key in ('scope', 'revision', 'live', 'pending', 'closed')})
            value['visible'] = deepcopy(state.get('visible'))
            ref = await self._upload(state, value)
            try:
                result = await self._request(state, 'file_sync', {'payload': ref})
                if result.get('revision') != revision:
                    raise RemoteFailure('file_authority', 'Worker did not confirm file context revision')
                state['acked'] = revision
            finally:
                await self._release(state, ref)

    async def _query(self, state, oid):
        response = await self._request(state, 'query', {'operation_id': oid, 'wait_ms': 1000})
        while response['state'] == 'pending':
            response = await self._request(state, 'query', {'operation_id': oid, 'wait_ms': 1000})
        if response.get('error'):
            error = response['error']
            return {'status': 'error', 'llm_text': error['message'], 'text': error['message'],
                    'structured': {'error_code': error['code'], 'operation_id': oid},
                    'effect_receipt': {'outcome': error.get('effect', 'unknown'), 'receipt': {'operation_id': oid}}}
        payload = await self._download(state, response['result'])
        # The operation journal keeps the outcome; full output is now owned by
        # Pal delivery/snapshot retention, not a second worker text cache.
        try:
            await self._request(state, 'file_result_release', {'operation_id': oid})
        except RemoteFailure:
            pending = state.setdefault('receipt_releases', [])
            if oid not in pending:
                pending.append(oid)
        return payload

    async def execute(self, runtime, action, call):
        if getattr(runtime, '_file_scope_closed', False):
            raise ToolRejectedError('Temporary file context has ended', error_code='file_scope_closed')
        port = self.owner.remote_port
        if port is None:
            raise ToolRejectedError('Remote plugin is unavailable', error_code='unsupported_target')
        route = await port.resolve(call.args.get('target', 0))
        metadata = await port.request(route['target'], 'metadata', {}, binding=route['binding'])
        if port is not self.owner.remote_port:
            raise ToolRejectedError('Remote plugin changed', error_code='backend_unavailable')
        if metadata.get('file_protocol_version') != 1:
            raise ToolRejectedError('This worker requires an upgrade for remote file tools.', error_code='file_protocol_unsupported')
        if metadata.get('worker_id') != route['worker_id']:
            raise ToolRejectedError('Worker identity does not match target', error_code='worker_identity_mismatch')
        turn = str(call.meta.get('turn_id') or call.meta.get('direct_context_id') or '')
        if not turn:
            raise ToolRejectedError('File tools require a logical context', error_code='missing_execution_lifetime')
        lifetime = self.lifetime_for(runtime, turn)
        epoch = metadata['runtime_epoch']
        key = json.dumps([route['binding'], epoch, lifetime])
        state = self.scopes.get(key)
        if state is not None and state['closed'] and not state['unresolved']:
            # A reset can reuse the logical lifetime name. Its new scope must
            # never revive the old worker tombstone or a late old delivery.
            retired_key = key + ':retired:' + state['scope']
            self.scopes[retired_key] = state
            for item in self.calls.values():
                if item['key'] == key:
                    item['key'] = retired_key
            state = None
        if state is None:
            state = {**route, 'epoch': epoch, 'lifetime': lifetime, 'scope': uuid4().hex,
                     'revision': 0, 'acked': -1, 'live': {}, 'pending': [], 'unresolved': {}, 'closed': False}
            self.scopes[key] = state
        async with self.locks.setdefault(state['scope'], asyncio.Lock()):
            self._set_visible(state, runtime.execution_sessions.projected_visible_results(turn))
            # Query the existing journal before admitting another mutation.
            # Return the recovered receipt to the caller; never replay its args.
            if state['unresolved']:
                oid = next(iter(state['unresolved']))
                try:
                    recovered = await self._query(state, oid)
                except RemoteFailure as exc:
                    raise self._unknown(exc, oid) from exc
                del state['unresolved'][oid]
                if oid in state['pending']:
                    state['pending'].remove(oid)
                    state['revision'] += 1
                raise ToolExecutionError('Recovered the previous file operation. Inspect its receipt and read the current file before another mutation.',
                    error_code='file_operation_reconciled', retry=RetryDirective.DO_NOT_RETRY,
                    effect_receipt=EffectReceipt(outcome=EffectOutcome.NONE),
                    details={'operation_id': oid, 'previous_result': recovered})
            oid = uuid4().hex
            call_id = getattr(call.meta.get('tool_call'), 'call_id', '') or oid
            state['pending'].append(oid)
            state['revision'] += 1
            self.calls[call_id] = {'key': key, 'operation_id': oid, 'turn': turn}
            try:
                await self._sync(state)
                value = {'scope': state['scope'], 'revision': state['revision'], 'action': action,
                         'args': {k: v for k, v in call.args.items() if k != 'target'}}
                ref = await self._upload(state, value)
            except BaseException:
                self.discard(call_id)
                raise
            if value['revision'] != state['revision']:
                # Upload can yield while history retirement or projection
                # changes the context. Do not dispatch with its old authority.
                self.discard(call_id)
                await self._release(state, ref)
                raise ToolRejectedError('File context changed before execution; inspect the current context before retrying.',
                                        error_code='file_context_changed')
            state['unresolved'][oid] = {'action': action, 'call_id': call_id}
            try:
                try:
                    await self._request(state, 'file_execute', {'operation_id': oid, 'payload': ref})
                except RemoteFailure as exc:
                    if exc.effect != 'not_started':
                        raise
                    # Only the submission's own refusal proves no file effect.
                    # A query refusal cannot settle an already submitted write.
                    del state['unresolved'][oid]
                    self.discard(call_id)
                    raise ToolRejectedError(str(exc), error_code=exc.code,
                                            details={'operation_id': oid}) from exc
                payload = await self._query(state, oid)
            except RemoteFailure as exc:
                # A failure to query says nothing about the preceding write.
                raise self._unknown(exc, oid) from exc
            finally:
                await self._release(state, ref)
            del state['unresolved'][oid]
            manifest = FileDeliveryManifest.from_dict(payload.get('context_delivery'))
            if manifest is not None:
                payload['context_delivery'] = replace(manifest,
                    remote={'scope': state['scope'], 'operation_id': oid}).to_dict()
            else:
                self.discard(call_id)
            receipt = payload.get('effect_receipt')
            if receipt:
                payload['effect_receipt'] = EffectReceipt(outcome=EffectOutcome(receipt['outcome']), receipt=receipt.get('receipt', {}))
            payload['structured'] = {**(payload.get('structured') or {}),
                'target': route['target'], 'target_name': route['target_name'], 'operation_id': oid}
            payload['llm_text'] += f"\nTarget: {route['target_name']} ({route['target']})"
            from pal.execution.file_capabilities import _reject_pre_effect_failure, _READ_RECOVERY_HINTS, _EDIT_RECOVERY_HINTS, _WRITE_RECOVERY_HINTS
            result = _reject_pre_effect_failure(CapabilityResult(**payload),
                {'read': _READ_RECOVERY_HINTS, 'edit': _EDIT_RECOVERY_HINTS, 'write': _WRITE_RECOVERY_HINTS}[action])
            return result

    @staticmethod
    def _unknown(exc, oid):
        return ToolExecutionError(str(exc), error_code='file_operation_unresolved',
            retry=RetryDirective.RECONCILE_FIRST,
            effect_receipt=EffectReceipt(outcome=EffectOutcome.UNKNOWN, receipt={'operation_id': oid}),
            details={'operation_id': oid}, recovery_hint='Do not repeat the write. The next file call queries this operation first.')

    def commit(self, delivery, result_id):
        remote = delivery.get('remote') or {}
        state = next((s for s in self.scopes.values() if s['scope'] == remote.get('scope')), None)
        if state is None or state['closed']:
            raise RuntimeError('Remote file delivery has no active scope')
        oid = remote['operation_id']
        manifest = dict(delivery)
        manifest.pop('remote', None)
        item = {'operation_id': oid, 'manifest': manifest}
        if result_id in state['live']:
            if state['live'][result_id] == item:
                return
            raise RuntimeError('Remote file result identity is immutable')
        if oid not in state['pending']:
            raise RuntimeError('Remote file result was retired before delivery')
        state['live'][result_id] = item
        state['pending'].remove(oid)
        state['revision'] += 1

    async def acknowledge(self, call_id):
        item = self.calls.get(call_id)
        if item:
            state = self.scopes[item['key']]
            # A result may have lost all source proof during output budgeting.
            # Reaching L1 with no manifest must retire its staged read too.
            if item['operation_id'] in state['pending']:
                self.discard(call_id)
            # Delivery is already in L1. Failure leaves a pending revision that
            # the next dependent operation must synchronize successfully.
            try:
                async with self.locks.setdefault(state['scope'], asyncio.Lock()):
                    await self._sync(state)
            except (RemoteFailure, OSError):
                return

    def _schedule_sync(self, state):
        async def flush():
            try:
                async with self.locks.setdefault(state['scope'], asyncio.Lock()):
                    await self._sync(state)
            except Exception:
                pass  # Keep the revision dirty; writes cannot bypass _sync.
        try:
            task = asyncio.get_running_loop().create_task(flush())
        except RuntimeError:
            return
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    def discard(self, call_id):
        item = self.calls.get(call_id)
        if item:
            state = self.scopes[item['key']]
            if item['operation_id'] in state['pending']:
                state['pending'].remove(item['operation_id'])
                state['revision'] += 1
                self._schedule_sync(state)

    def retire(self, lifetime, result_ids):
        for state in self.scopes.values():
            if state['lifetime'] != lifetime:
                continue
            changed = False
            for rid in result_ids:
                changed |= state['live'].pop(rid, None) is not None
            if changed:
                state['revision'] += 1
                self._schedule_sync(state)

    def retire_scope(self, lifetime=None):
        for state in self.scopes.values():
            if lifetime is not None and state['lifetime'] != lifetime or state['closed']:
                continue
            state.update(live={}, pending=[], closed=True, revision=state['revision'] + 1)
            self._schedule_sync(state)

    def snapshot(self):
        return deepcopy({'scopes': self.scopes, 'calls': self.calls})

    def restore(self, value):
        self.validate_snapshot(value)
        self.scopes = deepcopy(value.get('scopes', {}))
        self.calls = deepcopy(value.get('calls', {}))
        for state in self.scopes.values():
            # Restore cannot assume that any prior ACK is still current.
            state['acked'] = -1

    @staticmethod
    def validate_snapshot(value):
        if not isinstance(value, dict) or not isinstance(value.get('scopes', {}), dict) or not isinstance(value.get('calls', {}), dict):
            raise ValueError('Invalid remote file checkpoint')
        scopes = value.get('scopes', {})
        for key, state in scopes.items():
            if (not isinstance(key, str) or not isinstance(state, dict)
                    or type(state.get('target')) is not int or state['target'] <= 0
                    or type(state.get('revision')) is not int or state['revision'] < 0
                    or not all(isinstance(state.get(k), str) and state[k]
                               for k in ('scope', 'epoch', 'binding', 'worker_id', 'lifetime'))
                    or not isinstance(state.get('live'), dict)
                    or not isinstance(state.get('pending'), list)
                    or not isinstance(state.get('unresolved'), dict)
                    or (state.get('visible') is not None and (not isinstance(state['visible'], list)
                        or not all(isinstance(rid, str) and rid for rid in state['visible'])))
                    or type(state.get('closed')) is not bool):
                raise ValueError('Invalid remote file scope checkpoint')
            for rid, item in state['live'].items():
                if not isinstance(rid, str) or not isinstance(item, dict) or not item.get('operation_id') or FileDeliveryManifest.from_dict(item.get('manifest')) is None:
                    raise ValueError('Invalid remote file delivery checkpoint')
        for item in value.get('calls', {}).values():
            if not isinstance(item, dict) or item.get('key') not in scopes:
                raise ValueError('Remote file call has no owning scope')

    def reconcile_restored_history(self):
        present = set().union(*(set(refs) for refs in self.history_refs.values()))
        for state in self.scopes.values():
            state['live'] = {rid: item for rid, item in state['live'].items() if rid in present}
            state['pending'] = [oid for oid in state['pending'] if oid in state['unresolved']]
            state['revision'] += 1
            self._schedule_sync(state)

    async def close(self):
        self.retire_scope()
        self.detach_histories()
        if self.tasks:
            await asyncio.gather(*tuple(self.tasks), return_exceptions=True)
