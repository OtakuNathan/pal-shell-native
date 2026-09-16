"""Installed-wheel acceptance tests; deliberately independent of Pal."""
from pathlib import Path
import itertools
import queue
import tempfile
import time
import unittest

import _pal_shell_runtime as native


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.events = queue.Queue()
        self.runtime = native.Runtime(32)
        self.ids = itertools.count(1)
        native.connect(self.runtime, self.events.put)

    def tearDown(self):
        self.runtime.close()
        self.assertEqual(self.runtime.callback_errors(), 0)

    def call(self, method, *args):
        request = next(self.ids)
        getattr(self.runtime, method)(request, *args)
        deadline = time.monotonic() + 10
        while True:
            event = self.events.get(timeout=max(0, deadline - time.monotonic()))
            if event['request_id'] == request:
                return event

    def run_shell(self, command, *, wait=5000, timeout=0, budget=4096):
        return self.call('run', '/bin/sh', command, '', False, wait, timeout, budget)

    def test_short_output_and_exit_code(self):
        self.assertEqual(native.API_VERSION, 2)
        event = self.run_shell('printf hello; printf error >&2; exit 7')
        self.assertEqual((event['session_id'], event['status'], event['returncode']), (0, 'exited', 7))
        self.assertEqual(event['stdout_bytes'], b'hello')
        self.assertEqual(event['stderr_bytes'], b'error')
        self.call('release_output', event['output_id'])

    def test_budget_spills_complete_output_and_releases_files(self):
        event = self.run_shell('printf 123456789', budget=4)
        self.assertNotIn('stdout_bytes', event)
        path = Path(event['stdout_path'])
        self.assertEqual(path.read_bytes(), b'123456789')
        self.assertFalse(event['truncated'])
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.call('release_output', event['output_id'])['status'], 'output_released')
        self.assertFalse(path.exists())

    def test_background_handoff_and_read(self):
        event = self.run_shell('sleep 0.2; printf done', wait=0)
        session = event['session_id']
        self.assertGreater(session, 0)
        self.assertEqual(self.call('acknowledge', session, False)['status'], 'acknowledged')
        result = self.call('read', session, 5000)
        self.assertEqual((result['status'], result['stdout_bytes']), ('exited', b'done'))
        self.call('release_output', result['output_id'])

    def test_hard_deadline(self):
        event = self.run_shell('sleep 30', timeout=200)
        self.assertEqual(event['status'], 'timed_out')
        self.call('release_output', event['output_id'])

    def test_close_reaps_child(self):
        # PID is written only after exec, so the assertion cannot accidentally
        # inspect a shell that never started. No external process is touched.
        import os
        with tempfile.TemporaryDirectory() as directory:
            import shlex
            pidfile = Path(directory) / 'pid'
            event = self.run_shell(f'echo $$ > {shlex.quote(str(pidfile))}; exec sleep 30', wait=0)
            self.assertGreater(event['session_id'], 0)
            deadline = time.monotonic() + 5
            while not pidfile.exists() or not pidfile.read_text().strip():
                if time.monotonic() > deadline:
                    self.fail('child did not start')
                time.sleep(0.01)
            pid = int(pidfile.read_text())
            self.runtime.close()
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)


if __name__ == '__main__':
    import faulthandler
    faulthandler.dump_traceback_later(45, exit=True)
    unittest.main(verbosity=2)
