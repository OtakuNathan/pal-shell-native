from __future__ import annotations

import asyncio
from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from pal.core import PalCore
from pal.core.main_context import MainContext
from pal.execution import register_with_core
from pal.memory import MemoryService
from pal.llm.ir import LLMMessageIR, MessageRole
from pal.shared.tool_protocol import ToolResultIR
from pal_shell_native.adapter import Completion
from pal_shell_native.observation_owner import NAMESPACE, session_key
from pal_shell_native.runtime import NativeExecutionRuntime


class Shell:
    def __init__(self):
        self._completions = {}
        self._consumed = set()
        self._foreground = {}
        self._pending = {}
        self.execution_work = False
        self.gate = asyncio.Event()
        self.gate.set()
        self.loads = 0
        self.acks = 0
        self.fail_ack = False

    async def materialize(self, raw):
        self.loads += 1
        await self.gate.wait()
        return {**deepcopy(raw), 'stdout_bytes': b'x' * raw['stdout_total'], 'stderr_bytes': b''}

    async def acknowledge_completion(self, event):
        self.acks += 1
        if self.fail_ack:
            raise OSError('ack transport unavailable')

    async def close(self):
        pass


class ObservationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.runtime = NativeExecutionRuntime()
        self.core = PalCore(context=MainContext(execution_runtime=self.runtime))
        register_with_core(self.core.context)
        self.core.publish_module_capabilities('execution')
        self.owner = self.runtime.shell_owner
        self.shell = Shell()
        self.owner._shell = self.shell
        self.obs = self.owner.observations
        self.owner.sessions[7] = dict(committed=True, watching=True, watch_generation=0,
            origin_turn='t', budget=None, cmd='test', latest_status='running', output_offsets={})
        self.memory = MemoryService()
        self.memory.begin_l1_turn('t', user_text='task')
        self.runtime.begin_tool_result_turn(turn_id='t', scope_key='test')
        self.continuation = SimpleNamespace(turn_id='t', delivery_binding=None)

    async def asyncTearDown(self):
        await self.runtime.shutdown_async()
        self.core.close()

    def raw(self, **changes):
        return dict(dict(session_id=7, output_id='out', runtime_epoch='epoch', event_sequence=0,
            watch_generation=0, watching=True, has_deadline=True, has_wake=False, elapsed_ms=10,
            remaining_ms=90, stdout_total=0, stderr_total=0, status='running', returncode=None), **changes)

    def project(self):
        self.obs.project(self.runtime, self.memory, self.continuation)

    def publish(self, **changes):
        event = Completion(7, 't', self.raw(**{'event_sequence': 1, 'event_kind': 'wait_expired', **changes}))
        self.shell._completions[7] = event
        self.obs.collect()
        return event

    async def prepared(self):
        await asyncio.gather(*tuple(self.obs.preparing.values()))

    def state(self):
        return self.memory.active_l1_turn('t').metadata['observation_coverage'][NAMESPACE]

    async def test_fifty_clock_only_refreshes_are_silent_and_do_not_wake(self):
        self.obs.record(self.raw())
        self.project()
        before = self.memory.active_l1_turn('t')
        waiter = self.obs.park_or_ready()
        for count in range(50):
            self.obs.record(self.raw(elapsed_ms=10 + count, remaining_ms=90 - count))
            self.project()
        self.assertIs(self.memory.active_l1_turn('t'), before)
        self.assertEqual(self.obs.latest[7].revision, 1)
        self.assertFalse(waiter.done())
        waiter.cancel()

    async def test_new_turn_reuses_visible_state_without_a_new_message(self):
        self.obs.record(self.raw())
        self.project()
        self.memory.settle_l1_turn('t')
        self.memory.begin_l1_turn('next', user_text='continue')
        self.continuation.turn_id = 'next'
        before = self.memory.active_l1_turn('next')
        self.project()
        self.assertEqual(self.memory.active_l1_turn('next').messages, before.messages)
        self.assertEqual(self.obs.latest[7].revision, 1)

    async def test_unwatched_live_resource_does_not_block_completion(self):
        from pal_shell_native.role_sessions import BunshinShellSessions
        driver = BunshinShellSessions(self.runtime)
        self.owner.sessions[7]['watching'] = False
        self.assertTrue(driver.resources_live)
        self.assertFalse(driver.has_work)
        async def check():
            pass
        self.assertEqual(await asyncio.wait_for(driver.wait_after_response(check), .5), '')

    async def test_newer_ack_is_not_swallowed_by_inflight_older_ack(self):
        gate = asyncio.Event()
        calls = []
        async def ack(event):
            calls.append(event.result['event_sequence'])
            if len(calls) == 1:
                await gate.wait()
        self.shell.acknowledge_completion = ack
        first = Completion(7, 't', self.raw(event_sequence=1))
        second = Completion(7, 't', self.raw(event_sequence=2, status='exited'))
        first_task = self.obs.acknowledge(first)
        await asyncio.sleep(0)
        self.assertIs(self.obs.acknowledge(first), first_task)
        second_task = self.obs.acknowledge(second)
        self.assertIsNot(first_task, second_task)
        gate.set()
        await second_task
        self.assertEqual(calls, [1, 2])
        self.assertFalse(self.obs.acking)
        self.assertNotIn(7, self.owner.sessions)

    async def test_instant_read_stagnation_ignores_clock_but_preserves_new_output(self):
        from pal.shared.tool_protocol import new_tool_call, ToolExecutionResult
        call = new_tool_call(name='shell_session', args={'session_id': 7, 'action': 'read', 'wait_ms': 0})
        def result(raw):
            return ToolExecutionResult(name='shell_session', ok=True, llm_text=str(raw), structured=raw)
        first = self.runtime.stagnation_payload(call, result(self.raw()))
        later = self.runtime.stagnation_payload(call, result(self.raw(elapsed_ms=20, remaining_ms=80)))
        self.assertEqual(first, later)
        changed = self.runtime.stagnation_payload(call, result(self.raw(stdout_total=1, stdout='x')))
        self.assertNotEqual(first, changed)
        waited = new_tool_call(name='shell_session', args={'session_id': 7, 'action': 'read', 'wait_ms': 1000})
        self.assertNotEqual(self.runtime.stagnation_payload(waited, result(self.raw())),
                            self.runtime.stagnation_payload(waited, result(self.raw(elapsed_ms=20, remaining_ms=80))))

    async def test_capture_never_waits_for_unprepared_output(self):
        self.shell.gate.clear()
        event = self.publish(stdout_total=4)
        waiter = self.obs.park_or_ready()
        self.project()
        frozen = self.memory.active_l1_turn('t')
        self.assertFalse(self.state()['events'])
        self.assertFalse(self.state()['outputs'])
        self.assertFalse(waiter.done())
        self.shell.gate.set()
        await self.prepared()
        await asyncio.wait_for(waiter, 1)
        self.project()
        self.assertEqual(self.state()['outputs'][session_key(event.result)]['stdout'], 4)
        self.assertEqual(len(frozen.messages), 2)
        self.assertNotEqual(frozen, self.memory.active_l1_turn('t'))
        await asyncio.gather(*tuple(self.obs.acking.values()))

    async def test_owner_claim_excludes_resident_and_active_request_competition(self):
        self.publish(stdout_total=4)
        await self.prepared()
        event = self.obs.claim(7, 'resident')
        self.assertIsNotNone(event)
        self.assertIsNone(self.obs.claim(7, 'request'))
        self.project()
        self.assertFalse(self.state()['events'])
        self.obs.release_claim(7, event)
        self.project()
        self.assertEqual(len(self.state()['events']), 1)

    async def test_failed_l1_commit_does_not_advance_any_coverage(self):
        event = self.publish(stdout_total=4)
        await self.prepared()
        before = self.memory.active_l1_turn('t')
        with patch.object(self.memory, 'append_l1_user_contexts', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.project()
        self.assertIs(self.memory.active_l1_turn('t'), before)
        self.assertEqual(self.owner.sessions[7]['output_offsets'], {})
        self.assertFalse(self.obs.covered)
        self.assertIs(self.obs.pending[7], event)
        self.assertFalse(self.obs.claims)
        self.project()
        self.assertEqual(self.shell.loads, 1)
        self.assertEqual(self.state()['outputs'][session_key(event.result)]['stdout'], 4)

    async def test_old_event_does_not_replace_newer_observation_or_consume_new_event(self):
        old = self.publish(stdout_total=4)
        await self.prepared()
        self.obs.record(self.raw(event_sequence=1, stdout_total=8))
        revision = self.obs.latest[7].revision
        self.obs.collect()
        self.assertEqual(self.obs.latest[7].raw['stdout_total'], 8)
        self.assertEqual(self.obs.latest[7].revision, revision)
        self.project()
        self.assertEqual(self.state()['outputs'][session_key(old.result)]['stdout'], 4)
        newer = Completion(7, 't', self.raw(event_sequence=2, stdout_total=8, event_kind='wait_expired'))
        self.shell._completions[7] = newer
        self.obs.collect()
        await self.prepared()
        self.obs.commit_event(old)
        self.assertIs(self.obs.pending[7], newer)
        self.project()
        self.assertEqual(self.state()['outputs'][session_key(old.result)]['stdout'], 8)

    async def test_ack_failure_does_not_repeat_projection_or_block_completion(self):
        event = self.publish(status='exited', returncode=0, stdout_total=4)
        self.shell.fail_ack = True
        await self.prepared()
        self.project()
        await asyncio.gather(*tuple(self.obs.acking.values()))
        before = self.memory.active_l1_turn('t')
        self.project()
        self.assertIs(self.memory.active_l1_turn('t'), before)
        self.assertFalse(self.owner.completion_blocked)
        self.assertFalse(self.owner.has_work)
        self.shell.fail_ack = False
        self.assertIsNone(self.obs.acknowledge(event))
        self.obs.retry_acknowledgements()
        self.assertEqual(self.shell.acks, 5)

    async def test_unwatch_invalidates_a_preparing_event_without_a_wake(self):
        self.shell.gate.clear()
        self.publish(stdout_total=4)
        waiter = self.obs.park_or_ready()
        self.owner.sessions[7].update(watching=False, watch_generation=1)
        self.obs.collect()
        self.shell.gate.set()
        await self.prepared()
        self.assertFalse(waiter.done())
        self.assertFalse(self.obs.pending)
        self.assertFalse(self.obs.prepared)
        waiter.cancel()

    async def test_tool_receipt_covers_state_but_not_undelivered_bytes(self):
        raw = self.raw(stdout_total=4, watch_generation=1)
        self.obs.record(raw)
        call_id = 'watch-receipt'
        from pal.shared.tool_protocol import new_tool_call
        call = new_tool_call(name='shell_session', args={'session_id': 7}, call_id=call_id)
        self.memory.upsert_l1_assistant('t', LLMMessageIR(role=MessageRole.ASSISTANT, parts=(call,)))
        self.memory.append_l1_tool_result('t', ToolResultIR(call_id=call_id, name='shell_session',
            content='control receipt', structured=raw, ok=True))
        self.obs.note_tool_state(call_id, raw)
        before = len(self.memory.active_l1_turn('t').messages)
        self.project()
        self.assertEqual(len(self.memory.active_l1_turn('t').messages), before)
        self.assertFalse(self.state()['outputs'])
        self.assertFalse(self.obs.covered)

    async def test_byte_only_changes_do_not_grow_context_or_revision(self):
        self.obs.record(self.raw())
        self.project()
        before = self.memory.active_l1_turn('t')
        waiter = self.obs.park_or_ready()
        for count in range(1, 51):
            self.obs.record(self.raw(stdout_total=count))
            self.project()
        self.assertIs(self.memory.active_l1_turn('t'), before)
        self.assertEqual(self.obs.latest[7].revision, 1)
        self.assertFalse(waiter.done())
        waiter.cancel()

    async def test_one_ready_event_is_one_bundle_without_control_fields(self):
        self.publish(stdout_total=4)
        await self.prepared()
        before = len(self.memory.active_l1_turn('t').messages)
        self.project()
        messages = self.memory.active_l1_turn('t').messages[before:]
        self.assertEqual(len(messages), 1)
        self.assertIn('xxxx', messages[0].text)
        for private in ('event_sequence', 'runtime_epoch', 'watch_generation', 'stdout_total', 'output_id', 'acknowledgement'):
            self.assertNotIn(private, messages[0].text)
        self.assertEqual(len(self.state()['events']), 1)

    async def test_output_failure_reports_once_without_covering_bytes_or_ack(self):
        async def unavailable(raw):
            raise OSError('output file unavailable')
        self.shell.materialize = unavailable
        event = self.publish(status='exited', returncode=0, stdout_total=4)
        with patch('pal_shell_native.recovery.RETRY_DELAYS', (0, 0)):
            await self.prepared()
        self.project()
        before = self.memory.active_l1_turn('t')
        self.assertIn('output_error', before.messages[-1].text)
        self.assertIn('exited', before.messages[-1].text)
        self.assertFalse(self.state()['outputs'])
        self.assertEqual(self.shell.acks, 0)
        self.assertFalse(self.owner.completion_blocked)
        self.project()
        self.assertIs(self.memory.active_l1_turn('t'), before)
        self.assertFalse(self.obs.eligible(7))
        self.assertEqual(self.owner.sessions[7]['output_offsets'], {})

    async def test_hidden_state_proof_is_reprojected_without_source_revision_change(self):
        self.obs.record(self.raw())
        self.project()
        old = self.memory.active_l1_turn('t')
        proof = self.state()['states'][session_key(self.raw())]['message_id']
        from dataclasses import replace
        hidden = replace(old, revision=old.revision + 1, metadata={**dict(old.metadata),
            'prompt_context_state': {'excluded': [proof]}})
        self.memory.l1_store.replace(hidden)
        self.project()
        self.assertEqual(self.obs.latest[7].revision, 1)
        self.assertNotEqual(self.state()['states'][session_key(self.raw())]['message_id'], proof)

    async def test_new_execution_event_restores_pending_work_after_reported_output_failure(self):
        original = self.shell.materialize
        async def unavailable(raw):
            raise OSError('output unavailable')
        self.shell.materialize = unavailable
        self.publish(stdout_total=4)
        with patch('pal_shell_native.recovery.RETRY_DELAYS', (0, 0)):
            await self.prepared()
        self.project()
        self.assertFalse(self.owner.completion_blocked)
        self.shell.materialize = original
        self.publish(stdout_total=8, event_sequence=2)
        self.assertTrue(self.owner.completion_blocked)
        await self.prepared()
        self.project()
        self.assertFalse(self.obs.failures)
        self.assertEqual(self.owner.sessions[7]['output_offsets']['stdout'], 8)

    async def test_paged_output_growth_is_internal_progress_without_new_context(self):
        from pal.execution.tool_facade import CompleteResult, EffectOutcome
        from pal.shared.tool_protocol import new_tool_call
        from pal_shell_native.runtime import PendingOutput
        call = new_tool_call(name='shell_session', args={'session_id': 7})
        ref = self.runtime.result_snapshots.capture('full output', call_id=call.call_id, lifetime='t')
        invocation = CompleteResult(output={}, snapshot_refs=(ref,),
                                 llm_text='same preview', effect=EffectOutcome.NONE, affordances=[])
        result = SimpleNamespace(invocation_result=invocation, structured={}, ok=True)
        self.owner.pending[call.call_id] = PendingOutput(self.raw(stdout_total=4), 't')
        first = self.runtime.stagnation_payload(call, result)
        self.owner.pending[call.call_id] = PendingOutput(self.raw(stdout_total=8), 't')
        second = self.runtime.stagnation_payload(call, result)
        self.assertNotEqual(first, second)
