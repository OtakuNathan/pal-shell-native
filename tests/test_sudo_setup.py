"""Credential wizard contract tests; never access an actual user's vault."""
from contextlib import ExitStack
import io
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from pal_shell_worker import sudo_setup


class Terminal(io.StringIO):
    def isatty(self):
        return True


class SetupTests(unittest.TestCase):
    def test_macos_password_is_prompted_not_an_argument(self):
        store, enroll, lookup = sudo_setup.store_commands('darwin', 'service', 'account')
        self.assertEqual(store, 'keychain')
        self.assertEqual(enroll[-1], '-w')
        self.assertNotIn('-A', enroll)
        self.assertIn('find-generic-password', lookup)

    def test_windows_rejected_without_prompts_or_processes(self):
        with patch.object(sudo_setup.sys, 'platform', 'win32'), patch('builtins.input') as ask, patch.object(sudo_setup.subprocess, 'run') as run:
            self.assertEqual(sudo_setup.main(None), 1)
            ask.assert_not_called()
            run.assert_not_called()

    @unittest.skipIf(os.name == 'nt', 'POSIX enrollment flow')
    def test_root_rejected_before_enrollment(self):
        with patch.object(sudo_setup.os, 'geteuid', return_value=0), patch.object(sudo_setup.subprocess, 'run') as run:
            self.assertEqual(sudo_setup.main(None), 1)
            run.assert_not_called()

    @unittest.skipIf(os.name == 'nt', 'POSIX enrollment flow')
    def test_headless_rejected_before_enrollment(self):
        with patch.object(sudo_setup.sys, 'stdin', io.StringIO()), patch.object(sudo_setup.os, 'geteuid', return_value=1000), patch.object(sudo_setup.os, 'getuid', return_value=1000), patch.object(sudo_setup.subprocess, 'run') as run:
            self.assertEqual(sudo_setup.main(None), 1)
            run.assert_not_called()

    @unittest.skipIf(os.name == 'nt', 'POSIX enrollment flow')
    def test_enrollment_templates_and_readability_without_capturing_password(self):
        for confirmation, results, expected in [('STORE', [0, 0], 0), ('', [], 0), ('STORE', [1], 1), ('STORE', [0, 1], 1)]:
            with self.subTest(confirmation=confirmation, results=results), tempfile.TemporaryDirectory() as temporary, ExitStack() as stack:
                root = Path(temporary)
                config = SimpleNamespace(worker_id='worker', client_id='pal', client_public_key='ab'*32,
                    socket_path=root/'worker.sock', shell='/bin/bash', privilege_helper='', askpass_helper='')
                stack.enter_context(patch.object(sudo_setup.sys, 'platform', 'linux'))
                stack.enter_context(patch.object(sudo_setup.sys, 'stdin', Terminal()))
                stack.enter_context(patch.object(sudo_setup.sys, 'stderr', Terminal()))
                output = stack.enter_context(patch.object(sudo_setup.sys, 'stdout', Terminal()))
                stack.enter_context(patch.object(sudo_setup.os, 'geteuid', return_value=1000))
                stack.enter_context(patch.object(sudo_setup.os, 'getuid', return_value=1000))
                stack.enter_context(patch('pwd.getpwuid', return_value=SimpleNamespace(pw_name='worker-user')))
                stack.enter_context(patch.object(sudo_setup.Path, 'is_file', return_value=True))
                stack.enter_context(patch.dict(os.environ, {'DBUS_SESSION_BUS_ADDRESS': 'test-only'}))
                stack.enter_context(patch('builtins.input', side_effect=['', '', '', '', str(root), confirmation]))
                run = stack.enter_context(patch.object(sudo_setup.subprocess, 'run', side_effect=[SimpleNamespace(returncode=c) for c in results]))
                self.assertEqual(sudo_setup.main(config), expected)
                self.assertEqual(run.call_count, len(results))
                if results:
                    enrollment = run.call_args_list[0]
                    self.assertNotIn('input', enrollment.kwargs)
                    self.assertNotIn('stdin', enrollment.kwargs)  # Real controlling terminal, not a password pipe.
                    self.assertEqual(enrollment.kwargs['stdout'], sudo_setup.subprocess.DEVNULL)
                if len(results) == 2:
                    self.assertEqual(run.call_args_list[1].kwargs['stdout'], sudo_setup.subprocess.DEVNULL)
                plan = next(root.glob('setup-*'))
                import tomllib
                auth = tomllib.loads((plan/'auth.toml').read_text())
                self.assertEqual(auth['account'], 'worker-user')
                self.assertEqual(auth['worker_socket'], str(config.socket_path))
                self.assertNotIn('password', auth)
                self.assertEqual(plan.stat().st_mode & 0o777, 0o700)
                self.assertIn('Sudo is not yet verified' if results == [0, 0] else 'Configuration templates', output.getvalue())


if __name__ == '__main__':
    unittest.main()
