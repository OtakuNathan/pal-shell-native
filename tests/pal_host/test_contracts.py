import unittest

from pal_shell_native.adapter import Completion
from pal_shell_native.observations import event_metadata, observation_is_current, output_since
from pal_shell_native.tools import session_affordances, SESSION_GUIDANCE, RESIDENT_SESSION_GUIDANCE


class NativeContractTests(unittest.TestCase):
    def test_quiet_session_can_watch_and_extend_without_duplicate_unwatch(self):
        hints = session_affordances({'session_id': 7, 'status': 'running', 'watching': False, 'remaining_ms': 50})
        self.assertEqual(len(hints), 1)
        self.assertEqual(hints[0].tool, 'read_tool')
        self.assertEqual(hints[0].arguments, {'name': 'shell_session'})
        self.assertIn('watch (', hints[0].reason)
        self.assertNotIn('unwatch (', hints[0].reason)
        self.assertIn('extend (', hints[0].reason)
        self.assertNotIn('PTY', hints[0].reason)
        terminating = session_affordances({'session_id': 7, 'status': 'terminating'})
        self.assertEqual(len(terminating), 1)
        self.assertIn('read (', terminating[0].reason)
        self.assertNotIn('terminate (', terminating[0].reason)
        self.assertNotIn('watch (', terminating[0].reason)
        for status in ('exited', 'cancelled', 'timed_out', 'failed', 'released'):
            self.assertEqual(session_affordances({'session_id': 7, 'status': status}), [])

    def test_live_pty_capabilities_match_attention_and_deadline(self):
        hints = session_affordances({'session_id': 7, 'status': 'running',
                                    'has_wake': True, 'watching': True, 'tty': True})
        self.assertEqual(len(hints), 1)
        reason = hints[0].reason
        self.assertIn('unwatch (', reason)
        self.assertNotIn('; watch (', reason)
        self.assertNotIn('extend (', reason)
        self.assertIn('write (PTY input)', reason)
        self.assertIn('resize (PTY dimensions)', reason)
        self.assertIn('terminate (', reason)
        self.assertEqual(session_affordances({'status': 'running'}), [])

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
