"""Linux installer handoff, without reading credentials or changing the host."""
import io
from pathlib import Path
import sys
import tempfile
import tomllib
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from pal_shell_worker import management_setup


@unittest.skipUnless(sys.platform.startswith('linux'), 'Linux setup')
class SetupTests(unittest.TestCase):
    def test_no_password_default_no_power_and_exact_instructions(self):
        with tempfile.TemporaryDirectory(prefix='setup space ') as directory:
            config = SimpleNamespace(worker_id='desktop', client_id='pal', client_public_key='a'*64,
                                     socket_path=Path('/home/test/worker.sock'), protected_machine_ids=())
            output = io.StringIO()
            with patch('os.getuid', return_value=1000), patch('os.geteuid', return_value=1000), \
                 patch('pwd.getpwuid', return_value=SimpleNamespace(pw_name='test')), \
                 patch('sys.stdin.isatty', return_value=True), patch('sys.stdout', output), \
                 patch('builtins.input', side_effect=['1', '', '', directory]) as prompt:
                self.assertEqual(management_setup.main(config), 0)
            path = next(Path(directory).glob('setup-*/NEXT_STEPS.txt'))
            self.assertIn(str(path), output.getvalue())
            self.assertIn("cat '", output.getvalue())
            policy = tomllib.loads((path.parent/'management.toml').read_text())
            self.assertEqual(policy['allowed_actions'], ['apt_update', 'apt_install'])
            self.assertIn('NOPASSWD: /usr/local/libexec/pal-shell-manage ""',
                          (path.parent/'pal-shell-management.sudoers').read_text())
            self.assertEqual(prompt.call_count, 4)
            self.assertIn('visudo -c', path.read_text())
            self.assertNotIn('secret-service', path.read_text())

    def test_noninteractive_setup_is_refused(self):
        with patch('os.getuid', return_value=1000), patch('os.geteuid', return_value=1000), \
             patch('sys.stdin.isatty', return_value=False), patch('sys.stderr', io.StringIO()):
            self.assertEqual(management_setup.main(None), 1)


if __name__ == '__main__':
    unittest.main()
