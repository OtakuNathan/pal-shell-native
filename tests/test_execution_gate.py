"""Concurrent execution preserves each operation's outcome and delivery state."""
import unittest
from pal_shell_remote.execution_gate import ExecutionGate
from pal_shell_worker.protocol import RemoteError


class ExecutionGateTests(unittest.TestCase):
    def test_worker_pending_receipt_advances_submission_without_inventing_a_session(self):
        gate = ExecutionGate(1)
        gate.claim('op', 'epoch', hold_output=True)
        gate.result('op', {'state': 'pending', 'result': None, 'error': None}, 'epoch')
        self.assertEqual(gate.status()['reasons'], ['pending'])
        self.assertEqual(gate.operations['op']['session_id'], 0)
        self.assertEqual(gate.operations['op']['output_id'], '')

    def test_restored_submission_is_unknown_but_live_claim_is_not_overwritten(self):
        gate = ExecutionGate(1)
        record = dict(operation_id='op', epoch='epoch', state='submitting', session_id=0, output_id='')
        gate.restore([record])
        self.assertEqual(gate.status()['reasons'], ['unknown'])
        self.assertEqual(record['state'], 'submitting')
        live = ExecutionGate(1)
        live.claim('op', 'epoch')
        live.restore([record])
        self.assertEqual(live.status()['reasons'], ['submitting'])

    def test_execution_identity_recovers_empty_keys_without_touching_other_claims(self):
        gate = ExecutionGate(1)
        gate.claim('lost', 'epoch', hold_output=True)
        gate.failed('lost', 'unknown')
        gate.claim('independent', 'epoch', hold_output=True)
        gate.snapshot({'session_id': 8, 'status': 'exited', 'output_id': 'retained',
                       'execution_id': 'lost'}, 'epoch')
        self.assertEqual(gate.operations['lost']['state'], 'delivery')
        self.assertEqual(gate.operations['lost']['session_id'], 8)
        self.assertEqual(gate.operations['lost']['output_id'], 'retained')
        self.assertEqual(gate.operations['independent']['state'], 'submitting')
        gate.release('retained', 'epoch')
        self.assertEqual(set(gate.operations), {'independent'})

    def test_event_identity_cannot_confirm_another_epoch_or_an_unknown_control(self):
        gate = ExecutionGate(1)
        gate.claim('execution', 'epoch', hold_output=True)
        gate.failed('execution', 'unknown')
        gate.restore([dict(operation_id='input', epoch='epoch', state='unknown',
                           session_id=8, output_id='', control=True)])
        event = {'session_id': 8, 'status': 'exited', 'output_id': 'retained', 'execution_id': 'execution'}
        gate.snapshot(event, 'another-epoch')
        self.assertEqual(gate.operations['execution']['state'], 'unknown')
        gate.snapshot(event, 'epoch')
        self.assertEqual(gate.operations['execution']['state'], 'delivery')
        self.assertEqual(gate.operations['input']['state'], 'unknown')

    def test_wrong_epoch_result_or_failure_cannot_erase_original_evidence(self):
        gate = ExecutionGate(1)
        gate.claim('op', 'original')
        gate.failed('op', 'unknown')
        gate.result('op', {'state': 'complete', 'error': {'effect': 'not_started'}}, 'replacement')
        gate.failed('op', 'not_started', 'replacement')
        self.assertEqual(gate.operations['op']['epoch'], 'original')
        self.assertEqual(gate.operations['op']['state'], 'unknown')

    def test_complete_response_without_outcome_does_not_leave_submitting(self):
        gate = ExecutionGate(1)
        gate.claim('op', 'epoch')
        gate.result('op', {'state': 'complete', 'result': None, 'error': None}, 'epoch')
        self.assertEqual(gate.status()['reasons'], ['unknown'])

    def test_lost_first_response_binds_the_connected_epoch_for_later_events(self):
        gate = ExecutionGate(1)
        gate.claim('op', None, hold_output=True)
        gate.failed('op', 'unknown', 'connected-epoch')
        self.assertEqual(gate.operations['op']['epoch'], 'connected-epoch')
        gate.snapshot({'execution_id': 'op', 'session_id': 1, 'status': 'exited', 'output_id': 'output'},
                      'connected-epoch')
        self.assertEqual(gate.operations['op']['state'], 'delivery')

    def test_late_pending_response_or_query_failure_does_not_erase_delivery_evidence(self):
        gate = ExecutionGate(1)
        gate.claim('op', 'epoch', hold_output=True)
        gate.snapshot({'session_id': 1, 'status': 'exited', 'output_id': 'output', 'execution_id': 'op'}, 'epoch')
        gate.result('op', {'state': 'pending', 'result': None}, 'epoch')
        gate.query_failed('op', 'epoch')
        self.assertEqual(gate.operations['op']['state'], 'delivery')
        gate.release('output', 'epoch')
        self.assertFalse(gate.operations)

    def test_late_running_receipt_cannot_regress_a_completed_execution(self):
        gate = ExecutionGate(1)
        gate.claim('op', 'epoch', hold_output=True)
        gate.snapshot({'session_id': 1, 'status': 'exited', 'output_id': 'output',
                       'execution_id': 'op'}, 'epoch')
        gate.result('op', {'state': 'complete', 'result': {
            'session_id': 1, 'status': 'running', 'output_id': 'output'}}, 'epoch')
        self.assertEqual(gate.operations['op']['state'], 'delivery')

    def test_failed_receipt_cannot_demote_a_confirmed_delivery(self):
        gate = ExecutionGate(1)
        gate.claim('op', 'epoch', hold_output=True)
        gate.snapshot({'session_id': 1, 'status': 'exited', 'output_id': 'output',
                       'execution_id': 'op'}, 'epoch')
        gate.failed('op', 'unknown', 'epoch')
        gate.release('output', 'epoch')
        self.assertFalse(gate.operations)

    def test_empty_or_old_approval_receipt_cannot_erase_confirmed_output(self):
        gate = ExecutionGate(1)
        gate.claim('op', 'epoch', hold_output=True)
        gate.snapshot({'session_id': 1, 'status': 'exited', 'output_id': 'output',
                       'execution_id': 'op'}, 'epoch')
        gate.result('op', {'state': 'complete', 'result': None}, 'epoch')
        gate.result('op', {'state': 'approval_required', 'result': None}, 'epoch')
        self.assertEqual(gate.operations['op']['state'], 'delivery')
        gate.release('output', 'epoch')
        self.assertFalse(gate.operations)

    def test_accepted_shutdown_has_no_output_to_retain(self):
        gate = ExecutionGate(1)
        gate.claim('shutdown', 'epoch', hold_output=True, kind='shutdown')
        gate.result('shutdown', {'state': 'approval_required'}, 'epoch')
        gate.sign('shutdown', 'epoch')
        gate.result('shutdown', {'state': 'complete', 'result': {
            'status': 'accepted', 'operation_id': 'shutdown', 'runtime_epoch': 'epoch'}}, 'epoch')
        self.assertFalse(gate.operations)

    def test_unknown_survives_epoch_change_and_output_release(self):
        gate = ExecutionGate(1)
        gate.claim('op', 'old')
        gate.failed('op', 'unknown')
        gate.snapshot({'session_id': 1, 'status': 'exited', 'output_id': 'output'}, 'new')
        gate.release('output', 'old')
        gate.claim('new', 'new')
        self.assertEqual(gate.operations['op']['state'], 'unknown')
        self.assertEqual(gate.operations['op']['epoch'], 'old')
        self.assertEqual(gate.operations['new']['state'], 'submitting')
        self.assertFalse(gate.status()['blocked'])

    def test_output_delivery_is_retained_without_blocking_new_execution(self):
        gate = ExecutionGate(1)
        gate.claim('op', 'epoch', hold_output=True)
        gate.result('op', {'state': 'complete', 'result': {
            'session_id': 1, 'status': 'exited', 'output_id': 'output'}}, 'epoch')
        self.assertEqual(gate.status()['reasons'], ['delivery'])
        gate.release('output', 'wrong-epoch')
        self.assertTrue(gate.status()['has_work'])
        gate.claim('next', 'epoch')
        self.assertFalse(gate.status()['blocked'])
        gate.release('output', 'epoch')
        self.assertNotIn('op', gate.operations)
        self.assertIn('next', gate.operations)

    def test_only_unsigned_preparation_can_be_cancelled(self):
        gate = ExecutionGate(1)
        gate.restore([dict(operation_id='op', epoch='epoch', state='unknown', unsigned=False)])
        gate.cancel_prepared('op')
        self.assertTrue(gate.status()['has_work'])
        gate.operations['op']['unsigned'] = True
        gate.cancel_prepared('op')
        self.assertFalse(gate.status()['has_work'])

    def test_completion_event_cannot_confirm_an_inflight_control(self):
        gate = ExecutionGate(1)
        gate.claim('input', 'epoch')
        gate.operations['input'].update(session_id=1, control=True)
        gate.snapshot({'session_id': 1, 'status': 'exited', 'output_id': 'output'}, 'epoch')
        self.assertEqual(gate.status()['reasons'], ['submitting'])
        gate.failed('input', 'unknown')
        self.assertEqual(gate.status()['reasons'], ['unknown'])

    def test_query_confirming_input_releases_control_claim_even_if_process_running(self):
        gate = ExecutionGate(1)
        gate.restore([dict(operation_id='input', epoch='epoch', state='unknown',
                           session_id=1, output_id='output', control=True)])
        gate.result('input', {'state': 'complete', 'result': {
            'session_id': 1, 'status': 'running', 'output_id': 'output'}}, 'epoch')
        self.assertFalse(gate.status()['has_work'])

    def test_concurrent_claims_and_snapshots_keep_each_session_separate(self):
        gate = ExecutionGate(1)
        gate.claim('first', 'epoch', hold_output=True)
        gate.claim('second', 'epoch', hold_output=True)
        gate.snapshot({'session_id': 1, 'status': 'running', 'output_id': 'first-output'}, 'epoch', 'first')
        gate.snapshot({'session_id': 2, 'status': 'running', 'output_id': 'second-output'}, 'epoch', 'second')
        gate.snapshot({'session_id': 1, 'status': 'exited', 'output_id': 'first-output'}, 'epoch')
        self.assertEqual(gate.operations['first']['state'], 'delivery')
        self.assertEqual(gate.operations['second']['state'], 'running')
        gate.release('first-output', 'epoch')
        self.assertEqual(set(gate.operations), {'second'})
        with self.assertRaises(RemoteError) as error:
            gate.claim('second', 'another-epoch')
        self.assertEqual(error.exception.code, 'runtime_changed')


class ShortcutConfigTests(unittest.TestCase):
    def target(self, number=1, shortcut=''):
        from pal_shell_remote.slot import Target
        return Target(number, 'test', 'worker', 'pal', '/identity', '/worker.sock', shortcut=shortcut)

    def test_shortcut_matches_public_alias_limits(self):
        for name in ('cloud', 'macOS', 'build-box_2', 'x' * 54, ''):
            self.assertEqual(self.target(shortcut=name).shortcut, name)
        for name in ('bad/name', 'has space', 'x' * 55):
            with self.assertRaises(ValueError):
                self.target(shortcut=name)

    def test_duplicate_shortcut_fails_before_creating_transport(self):
        from pal_shell_remote.hub import RemoteHub
        with self.assertRaisesRegex(ValueError, 'unique'):
            RemoteHub([self.target(1, 'cloud'), self.target(2, 'cloud')])
