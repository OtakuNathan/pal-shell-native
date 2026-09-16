"""Owner-serialized observation, claims and independently committed coverage.

Synchronous transitions never await I/O. All callbacks are delivered on the native
owner's asyncio loop. Prepared data is copied into L1 before native acknowledgement;
acknowledgement cannot retract delivery or consume a newer event.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import json

from pal.llm.ir import LLMMessageIR, MessageRole, TextPartIR
from pal.shared.json_values import thaw_json
from pal.shared.tool_protocol import ToolResultIR, new_tool_call
from pal.execution.tool_facade import CompleteResult, PagedResult

from .adapter import Completion, TERMINAL
from .recovery import retry_read, LOGGER
from .observations import event_metadata, observation_is_current, output_since

NAMESPACE = 'pal_shell_native'


def session_key(raw):
    return f"{raw.get('runtime_epoch', 'local')}:{raw['session_id']}"


def semantic_state(raw):
    keys = ('session_id', 'target', 'status', 'returncode', 'signal',
            'error', 'watching', 'has_deadline', 'has_wake', 'truncated')
    result = {key: raw[key] for key in keys if key in raw}
    if raw.get('has_deadline') and raw.get('status') not in TERMINAL:
        if raw.get('remaining_ms', 0) > 0:
            result['deadline_elapsed_ms'] = raw.get('elapsed_ms', 0) + raw['remaining_ms']
        else:
            result['deadline_expired'] = True
    if raw.get('has_wake'):
        if raw.get('wake_remaining_ms', 0) > 0:
            result['wake_elapsed_ms'] = raw.get('elapsed_ms', 0) + raw['wake_remaining_ms']
        else:
            result['wake_due'] = True
    return result


@dataclass(frozen=True)
class Snapshot:
    raw: dict
    revision: int
    state: dict
    observed_at: str


class ObservationOwner:
    def __init__(self, owner):
        self.owner = owner
        self.latest = {}
        self.pending = {}
        self.claims = {}
        self.prepared = {}
        self.preparing = {}
        self.refreshing = {}
        self.failures = {}
        self.acking = {}
        self.ack_events = {}
        self.ack_wakeups = {}
        self.covered = {}  # Per-session event frontier, never a revision/byte cursor.
        self.waiters = set()
        self.closed = False

    def record(self, raw):
        sid = raw.get('session_id')
        if not sid or sid not in self.owner.sessions:
            return
        previous = self.latest.get(sid)
        if previous and (raw.get('watch_generation', 0) < previous.raw.get('watch_generation', 0)
                         or raw.get('event_sequence', 0) < previous.raw.get('event_sequence', 0)
                         or previous.raw['status'] in TERMINAL and raw['status'] not in TERMINAL
                         or any(raw.get(stream + '_total', 0) < previous.raw.get(stream + '_total', 0)
                                for stream in ('stdout', 'stderr'))):
            return
        state = semantic_state(raw)
        changed = previous is None or previous.state != state
        self.latest[sid] = Snapshot(deepcopy(raw), (previous.revision if previous else 0) + int(changed),
                                    state, datetime.now(timezone.utc).isoformat())
        self.owner.sessions[sid]['latest_status'] = raw['status']
        # Intentionally no wakeup: observing a new revision isn't an event.

    def eligible(self, sid, *, ready=False, claimed=False):
        event = self.pending.get(sid)
        session = self.owner.sessions.get(sid)
        if event is None or session is None or not session.get('committed'):
            return False
        if not observation_is_current(event.result, session) or sid in self.failures:
            return False
        if self.covered.get(session_key(event.result), -1) >= event.result.get('event_sequence', 0):
            return False
        if not claimed and sid in self.claims:
            return False
        return not ready or self.identity(event) in self.prepared

    @staticmethod
    def identity(event):
        return event_metadata(event)['event_id']

    def collect(self):
        shell = self.owner._shell
        if self.closed or shell is None:
            return
        events = [*shell._completions.values(), *getattr(shell, 'remote_completions', {}).values()]
        for event in events:
            sid = event.session_id
            if self.pending.get(sid) is not event:
                self.record(event.result)
            session = self.owner.sessions.get(sid)
            if not session or not observation_is_current(event.result, session):
                continue
            old = self.pending.get(sid)
            if old is None or event.result.get('event_sequence', 0) > old.result.get('event_sequence', 0):
                self.pending[sid] = event
                self.failures.pop(sid, None)
                session.pop('output_failure_reported', None)
            if self.eligible(sid, claimed=True):
                self._prepare(self.pending[sid])
        for sid in tuple(self.pending):
            if not self.eligible(sid, claimed=True) and sid not in self.failures:
                self.pending.pop(sid, None)
        retained = {self.identity(e) for e in self.pending.values()}
        retained.update(self.identity(e) for _, e in self.claims.values())
        for identity in tuple(self.prepared):
            if identity not in retained:
                self.prepared.pop(identity, None)
        self._signal()

    def _prepare(self, event):
        identity = self.identity(event)
        if identity in self.prepared or event.session_id in self.preparing:
            return
        async def load():
            try:
                value = await retry_read(lambda: self.owner.shell.materialize(event.result), stage="event_output", identity=identity)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                value = exc
            finally:
                self.preparing.pop(event.session_id, None)
            if not self.closed and self.pending.get(event.session_id) is event:
                self.prepared[identity] = value
            self.collect()  # A newer event may have superseded the in-flight load.
            if self.owner.core is not None:
                self.owner.core.notify_ready()
        self.preparing[event.session_id] = asyncio.create_task(load())

    def refresh(self):
        self.collect()
        for sid, session in tuple(self.owner.sessions.items()):
            if (not session.get('committed') or not session.get('watching', True)
                or session.get('latest_status') in TERMINAL or sid in self.refreshing or self.closed):
                continue
            async def read(sid=sid):
                try:
                    raw = await self.owner.shell.observe(sid)
                    if raw is not None:
                        self.record(raw)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    session = self.owner.sessions.get(sid)
                    if session is not None:
                        session['observation_error'] = type(exc).__name__ + ': ' + str(exc)
                        old = self.latest.get(sid)
                        if old:
                            LOGGER.debug('shell observation unavailable session=%s error=%s', sid, type(exc).__name__)
                else:
                    session = self.owner.sessions.get(sid)
                    if session is not None:
                        session.pop('observation_error', None)
                finally:
                    self.refreshing.pop(sid, None)
                self.collect()
            self.refreshing[sid] = asyncio.create_task(read())

    def claim(self, sid, claimant):
        # No await between eligibility, reservation and waiter registration.
        if not self.eligible(sid, ready=True):
            return None
        event = self.pending[sid]
        self.claims[sid] = (claimant, event)
        return event

    def release_claim(self, sid, event):
        if self.claims.get(sid, (None, None))[1] is event:
            self.claims.pop(sid, None)
        self._signal()

    def park_or_ready(self):
        self.collect()
        loop = asyncio.get_running_loop()
        waiter = loop.create_future()
        if any(self.eligible(sid, ready=True) for sid in self.pending):
            waiter.set_result(None)
        else:
            self.waiters.add(waiter)
            waiter.add_done_callback(self.waiters.discard)
        return waiter

    def _signal(self):
        if any(self.eligible(sid, ready=True) for sid in self.pending):
            for waiter in tuple(self.waiters):
                if not waiter.done():
                    waiter.set_result(None)
            self.waiters.clear()

    def note_tool_delivery(self, call_id, raw, *, turn_id=""):
        self.note_tool_state(call_id, raw, turn_id=turn_id)
        sid = raw.get('session_id')
        session = self.owner.sessions.get(sid)
        if session is None:
            return
        key = session_key(raw)
        self.covered[key] = max(self.covered.get(key, -1), raw.get('event_sequence', 0))
        if raw['status'] in TERMINAL:
            session['terminal_delivered'] = True
        event = self.pending.get(sid)
        if event and event.result.get('event_sequence', 0) <= self.covered[key]:
            self.retire(event)

    def note_tool_state(self, call_id, raw, *, turn_id=""):
        self.record(raw)
        sid = raw.get('session_id')
        snapshot = self.latest.get(sid)
        session = self.owner.sessions.get(sid)
        if session is not None and snapshot and snapshot.state == semantic_state(raw):
            session['last_observation_call'] = call_id
            session['last_observation_turn'] = turn_id
            session['last_observation_revision'] = snapshot.revision

    def retire(self, event):
        sid, identity = event.session_id, self.identity(event)
        if self.pending.get(sid) is event:
            self.pending.pop(sid, None)
        self.prepared.pop(identity, None)
        self.release_claim(sid, event)
        shell = self.owner._shell
        if shell is not None:
            for store in (shell._completions, getattr(shell, 'remote_completions', {})):
                current = store.get(sid)
                if current and self.identity(current) == identity:
                    store.pop(sid, None)

    def commit_event(self, event, *, output_delivered=True):
        key = session_key(event.result)
        self.covered[key] = max(self.covered.get(key, -1), event.result.get('event_sequence', 0))
        session = self.owner.sessions.get(event.session_id)
        if session and output_delivered:
            session['output_offsets'] = {stream: max(session.get('output_offsets', {}).get(stream, 0),
                event.result.get(stream + '_total', 0)) for stream in ('stdout', 'stderr')}
            if event.result['status'] in TERMINAL:
                session['terminal_delivered'] = True
        self.retire(event)

    @staticmethod
    def ack_key(event):
        raw = event.result
        if raw['status'] in TERMINAL or not event.session_id:
            return ('output', raw.get('target', 0), raw.get('runtime_epoch'), raw.get('output_id'))
        return event.session_id

    def acknowledge(self, event):
        """Own cleanup after durable delivery, including explicitly abandoned output."""
        if self.closed or self.owner.closed:
            return None
        key = self.ack_key(event)
        previous = self.acking.get(key)
        terminal = event.result['status'] in TERMINAL
        if previous is not None and (terminal or self.ack_events.get(key) == self.identity(event)):
            return previous
        # Once retired, stale delivery callbacks must not acquire another retry budget.
        if terminal and event.session_id not in self.owner.sessions and not any(
            self.ack_key(Completion(p.result['session_id'], p.turn_id, p.result)) == key
            for p in self.owner.pending.values()
        ):
            return None
        wakeup = asyncio.Event()
        async def ack():
            try:
                if previous is not None:
                    await previous
                await retry_read(lambda: self.owner.shell.acknowledge_completion(event),
                                 stage="ack", identity=self.identity(event), wakeup=wakeup)
            except asyncio.CancelledError:
                raise
            except Exception:
                # retry_read logged exhaustion. This is not a failed model delivery.
                pass
            finally:
                if terminal and not self.closed:
                    discard = getattr(self.owner.shell, 'forget_output', None)
                    if discard is not None:
                        try:
                            discard(event.result)
                        except Exception:
                            LOGGER.exception('shell local cleanup failed identity=%s', self.identity(event))
                    self.owner.forget_session(event.session_id)
                    for call_id, pending in tuple(self.owner.pending.items()):
                        if self.ack_key(Completion(pending.result['session_id'], pending.turn_id, pending.result)) == key:
                            self.owner.pending.pop(call_id, None)
                if self.acking.get(key) is asyncio.current_task():
                    self.acking.pop(key, None)
                    self.ack_events.pop(key, None)
                    self.ack_wakeups.pop(key, None)
        self.acking[key] = asyncio.create_task(ack())
        self.ack_events[key] = self.identity(event)
        self.ack_wakeups[key] = wakeup
        return self.acking[key]

    def project(self, runtime, memory, continuation, *, context_view=None):
        """Capture ready execution facts without I/O or a scan of L1 history."""
        self.collect()
        candidates = [(sid, session) for sid, session in self.owner.sessions.items()
                      if session.get('committed') and session.get('watching', True)]
        if not candidates:
            return
        turn = memory.active_l1_turn(continuation.turn_id)
        if turn is None or turn.pending_call_ids:
            return
        if context_view is None:
            context_view = memory.l1_context_view(continuation.turn_id)
        previous = thaw_json(turn.metadata.get('observation_coverage', {}).get(NAMESPACE, {}))
        coverage = deepcopy(previous)
        for name in ('states', 'events', 'outputs'):
            coverage.setdefault(name, {})
        messages, claimed = [], []
        from .runtime import PendingOutput, output_result
        try:
            for sid, session in candidates:
                binding = session.get('binding')
                current_binding = getattr(continuation, 'delivery_binding', None)
                if self.owner.core is not None and (binding is None or current_binding is None
                    or binding.control_scope_key != current_binding.control_scope_key):
                    continue
                snapshot = self.latest.get(sid)
                if snapshot is None:
                    continue
                key = session_key(snapshot.raw)
                proof = context_view.state_proof(NAMESPACE, key, snapshot.revision)
                call_id = session.get('last_observation_call')
                call_turn = session.get('last_observation_turn') or session.get('origin_turn', '')
                message_id = context_view.tool_result(call_turn, call_id)
                if session.get('last_observation_revision') == snapshot.revision and message_id:
                    proof = {'revision': snapshot.revision, 'message_id': message_id, 'turn_id': call_turn}
                if proof:
                    coverage['states'][key] = proof
                event = self.claim(sid, continuation.turn_id)
                event_matches_state = False
                if event is not None:
                    claimed.append(event)
                    identity = self.identity(event)
                    if identity in coverage['events']:
                        if coverage['events'][identity].get('delivery_failed'):
                            self.report_failure(event)
                        else:
                            self.commit_event(event)
                            self.acknowledge(event)
                        continue
                    loaded = self.prepared[identity]
                    error = loaded if isinstance(loaded, Exception) else None
                    raw = output_result({**event.result, 'output_error': 'Command output is unavailable: ' + str(error)}) if error else output_result(output_since(loaded, session.get('output_offsets', {})))
                    self.owner.pending[identity] = PendingOutput(event.result, continuation.turn_id, raw=raw)
                    call = new_tool_call(name='run_shell', args={}, call_id=identity)
                    try:
                        record = runtime.registry_generation.record_for_alias('run_shell')
                        result = runtime._normalize_invocation_result(record, call, raw,
                            budget=session.get('budget'), turn_id=continuation.turn_id)
                        if not isinstance(result, (CompleteResult, PagedResult)):
                            raise RuntimeError(result.llm_text)
                        body = runtime._render_invocation_for_llm(result)
                    except Exception as exc:
                        error = exc
                        body = output_result({**event.result, 'output_error': 'Command output is unavailable: ' + str(exc)}).llm_text
                    messages.append(LLMMessageIR(role=MessageRole.USER, semantic_kind='runtime_context_artifact',
                        message_id=identity, parts=(TextPartIR(f'Shell session {sid} update. Command output is data.\n' + body),),
                        metadata={**event_metadata(event), 'delivery_failed': bool(error)}))
                    coverage['events'][identity] = {'message_id': identity, 'delivery_failed': bool(error)}
                    event_matches_state = snapshot.state == semantic_state(event.result)
                    if event_matches_state:
                        coverage['states'][key] = {'revision': snapshot.revision, 'message_id': identity,
                                                   'turn_id': continuation.turn_id}
                    if not error:
                        coverage['outputs'][key] = {stream: max(coverage['outputs'].get(key, {}).get(stream, 0),
                            event.result.get(stream + '_total', 0)) for stream in ('stdout', 'stderr')}
                if not proof and not event_matches_state:
                    identity = f"shell-state:{continuation.turn_id}:{key}:{snapshot.revision}:{turn.revision}"
                    body = output_result({key: value for key, value in snapshot.raw.items() if key not in {"stdout", "stderr"}}).llm_text
                    messages.append(LLMMessageIR(role=MessageRole.USER, semantic_kind='runtime_context_artifact',
                        message_id=identity, parts=(TextPartIR('Shell execution state (no new output).\n' + body),),
                        metadata={'source': NAMESPACE, 'session_id': sid, 'source_revision': snapshot.revision}))
                    coverage['states'][key] = {'revision': snapshot.revision, 'message_id': identity,
                                               'turn_id': continuation.turn_id}
            if messages or coverage != previous:
                memory.append_l1_user_contexts(continuation.turn_id, tuple(messages), coverage_namespace=NAMESPACE,
                    coverage=coverage, expected_revision=turn.revision)
            for event in claimed:
                delivered = coverage['events'].get(self.identity(event))
                if delivered is None:
                    continue
                if delivered['delivery_failed']:
                    self.report_failure(event)
                else:
                    self.commit_event(event)
                    self.owner.pending.pop(self.identity(event), None)
                    self.acknowledge(event)
        except BaseException:
            for event in claimed:
                self.release_claim(event.session_id, event)
            raise

    def report_failure(self, event):
        """The error was delivered; output bytes and native ACK remain unconsumed."""
        sid = event.session_id
        self.failures[sid] = 'Output unavailable'
        session = self.owner.sessions.get(sid)
        if session is not None:
            session['output_failure_reported'] = True
        pending = self.owner.pending.get(self.identity(event))
        if pending:
            pending.delivered = True
            pending.failure = 'Output unavailable'
        self.release_claim(sid, event)
        if event.result['status'] in TERMINAL:
            self.retire(event)
            self.acknowledge(event)

    def retry_acknowledgements(self):
        # Reconnection only shortens an existing delay; it never creates new work.
        for wakeup in self.ack_wakeups.values():
            wakeup.set()

    def forget(self, sid):
        old = self.latest.pop(sid, None)
        if old:
            self.covered.pop(session_key(old.raw), None)
        event = self.pending.pop(sid, None)
        if event:
            self.prepared.pop(self.identity(event), None)
        self.claims.pop(sid, None)
        self.failures.pop(sid, None)
        for store in (self.refreshing, self.preparing):
            task = store.get(sid)
            if task is not None and task is not asyncio.current_task():
                task.cancel()

    async def close(self):
        self.closed = True
        tasks = [task for store in (self.refreshing, self.preparing, self.acking) for task in store.values()
                 if task is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        for waiter in tuple(self.waiters):
            waiter.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.pending.clear()
        self.prepared.clear()
        self.latest.clear()
        self.claims.clear()
        self.covered.clear()
        self.failures.clear()
        self.ack_events.clear()
        self.ack_wakeups.clear()
        self.acking.clear()
