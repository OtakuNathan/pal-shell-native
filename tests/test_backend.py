"""Installed-wheel acceptance tests; deliberately independent of Pal."""
from pathlib import Path
import itertools
import os
import queue
import tempfile
import time
import unittest
from unittest.mock import patch

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

    def test_output_respects_tmpdir_and_cleans_up_private_files(self):
        with tempfile.TemporaryDirectory(prefix='pal output space ') as directory:
            with patch.dict(os.environ, {'TMPDIR': directory + '/'}):
                event = self.run_shell('printf custom-temp', budget=0)
            self.assertEqual(event['status'], 'exited', event)
            path = Path(event['stdout_path'])
            self.assertEqual(path.parent.parent.resolve(), Path(directory).resolve())
            self.assertEqual(path.read_bytes(), b'custom-temp')
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.call('release_output', event['output_id'])
            self.assertFalse(path.parent.exists())

    def test_empty_or_unset_tmpdir_keeps_posix_default(self):
        for value in (None, ''):
            with self.subTest(value=value), patch.dict(os.environ):
                if value is None:
                    os.environ.pop('TMPDIR', None)
                else:
                    os.environ['TMPDIR'] = value
                event = self.run_shell('printf default-temp', budget=0)
                self.assertEqual(event['status'], 'exited', event)
                path = Path(event['stdout_path'])
                self.assertEqual(path.parent.parent.resolve(), Path('/tmp').resolve())
                self.call('release_output', event['output_id'])
                self.assertFalse(path.parent.exists())

    def test_invalid_tmpdir_fails_before_command_without_fallback(self):
        import shlex
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / 'executed'
            with patch.dict(os.environ, {'TMPDIR': str(Path(directory) / 'missing')}):
                event = self.run_shell(f'touch {shlex.quote(str(marker))}')
            self.assertEqual(event['status'], 'failed', event)
            self.assertIn('output directory', event['error'])
            self.assertFalse(event['stdout_path'])
            self.assertFalse(marker.exists())
            self.call('release_output', event['output_id'])

    def test_background_handoff_and_read(self):
        event = self.run_shell('sleep 0.2; printf done', wait=0)
        session = event['session_id']
        self.assertGreater(session, 0)
        self.assertEqual(self.call('acknowledge', session, False)['status'], 'acknowledged')
        result = self.call('read', session, 5000)
        self.assertEqual((result['status'], result['stdout_bytes']), ('exited', b'done'))
        self.call('release_output', result['output_id'])

    def test_concurrent_sessions_and_legacy_leases_keep_separate_identity(self):
        first = self.run_shell('sleep 30', wait=0)
        second = self.run_shell('sleep 30', wait=0)
        self.assertEqual(first['status'], 'running')
        self.assertEqual(second['status'], 'running')
        self.assertNotEqual(first['session_id'], second['session_id'])
        lease1 = self.call('acquire_write')['session_id']
        lease2 = self.call('acquire_write')['session_id']
        self.assertNotEqual(lease1, lease2)
        independent = self.run_shell('printf independent')
        self.assertEqual(independent['stdout_bytes'], b'independent')
        self.call('release_output', independent['output_id'])
        self.assertEqual(self.call('release_write', lease1)['status'], 'write_released')
        self.assertEqual(self.call('release_write', lease1)['status'], 'rejected')
        self.assertEqual(self.call('release_write', second['session_id'])['status'], 'rejected')
        self.assertEqual(self.call('release_write', lease2)['status'], 'write_released')
        self.call('terminate', first['session_id'])
        result = self.call('read', first['session_id'], 5000)
        self.assertEqual(result['status'], 'cancelled')
        self.call('release_output', result['output_id'])
        self.assertEqual(self.call('read', second['session_id'], 0)['status'], 'running')
        self.call('terminate', second['session_id'])
        result = self.call('read', second['session_id'], 5000)
        self.assertEqual(result['status'], 'cancelled')
        self.call('release_output', result['output_id'])

    def test_hard_deadline(self):
        event = self.run_shell('sleep 30', timeout=200)
        self.assertEqual(event['status'], 'timed_out')
        self.call('release_output', event['output_id'])

    def test_running_sessions_reserve_capacity_for_unacknowledged_results(self):
        self.runtime.close()
        self.runtime = native.Runtime(2)
        native.connect(self.runtime, self.events.put)
        sessions = [self.run_shell('sleep 30', wait=0) for _ in range(2)]
        self.assertTrue(all(event['status'] == 'running' for event in sessions))
        with tempfile.TemporaryDirectory() as directory:
            import shlex
            marker = Path(directory) / 'must-not-run'
            rejected = self.run_shell(f'touch {shlex.quote(str(marker))}', wait=0)
            self.assertEqual(rejected['status'], 'rejected', rejected)
            self.assertIn('result_capacity', rejected['error'])
            self.assertFalse(marker.exists())
        # A handoff ACK or finishing alone must not discard undelivered output.
        for event in sessions:
            self.call('acknowledge', event['session_id'], False)
            self.call('terminate', event['session_id'])
            result = self.call('read', event['session_id'], 5000)
            self.assertEqual(result['status'], 'cancelled')
        self.assertEqual(self.run_shell('true')['status'], 'rejected')
        self.call('acknowledge', sessions[0]['session_id'], True)
        next_session = self.run_shell('sleep 30', wait=0)
        self.assertEqual(next_session['status'], 'running')
        # Only the acknowledged result was evicted to make room.
        self.assertEqual(self.call('read', sessions[0]['session_id'], 0)['status'], 'rejected')
        self.assertEqual(self.call('read', sessions[1]['session_id'], 0)['status'], 'cancelled')
        self.call('release_output', sessions[1]['output_id'])
        self.assertEqual(self.run_shell('true')['status'], 'exited')

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
