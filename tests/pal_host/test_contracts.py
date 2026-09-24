import unittest

from pal_shell_native.adapter import Completion
from pal_shell_native.observations import event_metadata, observation_is_current, output_since
from pal_shell_native.tools import session_affordances, SESSION_GUIDANCE, RESIDENT_SESSION_GUIDANCE


class NativeContractTests(unittest.TestCase):
    def test_session_states_do_not_replay_static_controls(self):
        for sid in (7, 8):
            for status in ('running', 'terminating', 'input_accepted', 'exited', 'cancelled', 'timed_out', 'failed', 'released'):
                for watching in (True, False):
                    with self.subTest(sid=sid, status=status, watching=watching):
                        self.assertEqual(session_affordances({
                            'session_id': sid, 'status': status, 'watching': watching,
                            'tty': True, 'has_wake': True, 'remaining_ms': 50,
                        }), [])

    def test_failed_output_has_bound_recovery_even_after_exit(self):
        for sid in (7, 8):
            hints = session_affordances({'session_id': sid, 'status': 'exited', 'output_error': 'disk full'})
            self.assertEqual(len(hints), 1)
            self.assertEqual(hints[0].tool, 'call_tool')
            self.assertEqual(hints[0].arguments, {'name': 'shell_session', 'args': {'session_id': sid, 'action': 'read'}})
        hints = session_affordances({'session_id': 0, 'output_error': 'disk full'}, output_ref='saved-call')
        self.assertEqual(hints[0].arguments['args'], {'output_ref': 'saved-call', 'action': 'read'})
        self.assertEqual(session_affordances({'output_error': 'disk full'}), [])
        self.assertEqual(session_affordances({'status': 'exited'}, output_ref='saved-call'), [])

    def test_guidance_keeps_host_specific_actions_out_of_shared_contract(self):
        self.assertIn(SESSION_GUIDANCE.use_when, RESIDENT_SESSION_GUIDANCE.use_when)
        self.assertIn('watch(wait_ms', SESSION_GUIDANCE.use_when)
        self.assertNotIn('retry_notification', SESSION_GUIDANCE.use_when)

    def test_event_identity_kind_and_obsolete_observations(self):
        result = {'status': 'running', 'event_kind': 'wait_expired', 'runtime_epoch': 'epoch', 'event_sequence': 2, 'watch_generation': 3}
        completion = Completion(7, 'origin', result)
        metadata = event_metadata(completion)
        self.assertEqual(metadata['source'], 'execution.shell.wait_expired')
        self.assertEqual(metadata['event_id'], 'shell:epoch:7:2')
        self.assertTrue(observation_is_current(result, {'watch_generation': 3}))
        for state in ({'watching': False}, {'watch_generation': 4}, {'latest_status': 'exited'}, {'latest_status': 'terminating'}):
            self.assertFalse(observation_is_current(result, state))
        self.assertTrue(observation_is_current({**result, 'status': 'exited'}, {'latest_status': 'terminating'}))
        loaded = {'stdout_bytes': b'oldnew', 'stderr_bytes': b'err', 'stdout': 'oldnew'}
        delta = output_since(loaded, {'stdout': 3})
        self.assertEqual(delta['stdout'], 'new')
        self.assertEqual(loaded['stdout'], 'oldnew')
