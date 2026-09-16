"""Native session acceptance, with no Pal dependency or model calls."""
import itertools
import queue
import time
import unittest
import _pal_shell_runtime as native

class SessionTests(unittest.TestCase):
    def setUp(self):
        self.runtime = native.Runtime(32)
        self.events = queue.Queue()
        self.ids = itertools.count(1)
        self.unsolicited = []
        native.connect(self.runtime, self.events.put)
    def tearDown(self):
        self.runtime.close()
    def call(self, method, *args):
        request = next(self.ids)
        getattr(self.runtime, method)(request, *args)
        while True:
            event = self.events.get(timeout=5)
            if event['request_id'] == request:
                return event
            self.unsolicited.append(event)
    def start(self, timeout=0):
        return self.call('run', '/bin/sh', 'sleep 5', '', False, 0, timeout, 100)
    def test_watch_is_one_shot_and_unwatch_preserves_process(self):
        first = self.start()
        sid = first['session_id']
        receipt = self.call('watch', sid, 10, 0)
        self.assertTrue(receipt['watching'])
        event = self.events.get(timeout=2)
        self.assertEqual(event['event_kind'], 'wait_expired')
        with self.assertRaises(queue.Empty):
            self.events.get(timeout=.04)
        stopped_observing = self.call('unwatch', sid)
        self.assertFalse(stopped_observing['watching'])
        self.assertEqual(stopped_observing['status'], 'running')
        self.call('terminate', sid)
        terminal = self.call('read', sid, 2000)
        self.assertEqual(terminal['status'], 'cancelled')
        self.assertFalse(terminal['watching'])
    def test_extend_adds_to_original_deadline_and_rejects_after_stop(self):
        sid = self.start(2000)['session_id']
        a = self.call('read', sid, 0)
        b = self.call('watch', sid, 100, 1000)
        self.assertEqual(b['remaining_ms'] + b['elapsed_ms'], a['remaining_ms'] + a['elapsed_ms'] + 1000)
        self.call('terminate', sid)
        self.assertEqual(self.call('extend', sid, 1000)['status'], 'rejected')
    def test_unlimited_budget_is_not_silently_changed(self):
        sid = self.start()['session_id']
        self.assertEqual(self.call('watch', sid, 100, 1000)['status'], 'rejected')
        state = self.call('read', sid, 0)
        self.assertIsNone(state['remaining_ms'])
        self.assertIsNone(state['wake_remaining_ms'])
    def test_hard_timeout_records_terminal(self):
        sid = self.start(30)['session_id']
        result = self.call('read', sid, 2000)
        self.assertEqual(result['status'], 'timed_out')
        self.assertEqual(self.call('extend', sid, 100)['status'], 'rejected')

if __name__ == '__main__': unittest.main()
