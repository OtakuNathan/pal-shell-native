"""Concurrent execution preserves each operation's outcome and delivery state."""
import unittest
from pal_shell_remote.execution_gate import ExecutionGate
from pal_shell_worker.protocol import RemoteError


class ExecutionGateTests(unittest.TestCase):
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
        gate.restore([dict(operation_id='input', epoch='epoch', state='submitting',
                           session_id=1, output_id='', control=True)])
        gate.snapshot({'session_id': 1, 'status': 'exited', 'output_id': 'output'}, 'epoch')
        self.assertTrue(gate.status()['has_work'])
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
