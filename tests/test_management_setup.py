"""Linux installer handoff, without reading credentials or changing the host."""
import io
import json
import runpy
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
    def test_shutdown_opt_in_updates_both_policies_without_activating(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory)/'bundle'
            (bundle/'_internal').mkdir(parents=True)
            (bundle/'pal-shell-worker').write_text('fixture')
            config = SimpleNamespace(worker_id='desktop', client_id='pal', client_public_key='a'*64,
                                     socket_path=Path('/home/test/worker.sock'), protected_machine_ids=())
            with patch('os.getuid', return_value=1000), patch('os.geteuid', return_value=1000), \
                 patch('pwd.getpwuid', return_value=SimpleNamespace(pw_name='test')), \
                 patch('sys.stdin.isatty', return_value=True), patch('sys.stdout', io.StringIO()), \
                 patch('builtins.input', side_effect=['1', 'YES', str(bundle), '', directory]):
                self.assertEqual(management_setup.main(config), 0)
            path = next(Path(directory).glob('setup-*/NEXT_STEPS.txt'))
            policy = tomllib.loads((path.parent/'management.toml').read_text())
            worker = tomllib.loads((path.parent/'worker-sudo.toml').read_text())
            self.assertEqual(policy['allowed_actions'], ['apt_update', 'apt_install', 'shutdown'])
            self.assertEqual(worker['management_actions'], policy['allowed_actions'])
            self.assertEqual(worker['shutdown_policy'], 'approval')
            self.assertIn('do not use it as an installation probe', path.read_text())
            self.assertIn('No service or installed configuration was changed', path.read_text())

    def test_no_password_default_no_power_and_exact_instructions(self):
        with tempfile.TemporaryDirectory(prefix='setup space ') as directory:
            config = SimpleNamespace(worker_id='desktop', client_id='pal', client_public_key='a'*64,
                                     socket_path=Path('/home/test/worker.sock'), protected_machine_ids=())
            output = io.StringIO()
            bundle = Path(directory)/'source bundle'
            (bundle/'_internal').mkdir(parents=True)
            (bundle/'pal-shell-worker').write_text('#!/bin/sh\nexit 0\n')
            (bundle/'pal-shell-worker').chmod(0o755)
            with patch('os.getuid', return_value=1000), patch('os.geteuid', return_value=1000), \
                 patch('pwd.getpwuid', return_value=SimpleNamespace(pw_name='test')), \
                 patch('sys.stdin.isatty', return_value=True), patch('sys.stdout', output), \
                 patch('builtins.input', side_effect=['1', '', str(bundle), '', directory]) as prompt:
                self.assertEqual(management_setup.main(config), 0)
            path = next(Path(directory).glob('setup-*/NEXT_STEPS.txt'))
            self.assertIn(str(path), output.getvalue())
            self.assertIn("cat '", output.getvalue())
            policy = tomllib.loads((path.parent/'management.toml').read_text())
            self.assertEqual(policy['allowed_actions'], ['apt_update', 'apt_install'])
            self.assertIn('NOPASSWD: /usr/local/libexec/pal-shell-manage ""',
                          (path.parent/'pal-shell-management.sudoers').read_text())
            self.assertEqual(prompt.call_count, 5)
            self.assertIn('visudo -c', path.read_text())
            self.assertNotIn('secret-service', path.read_text())
            self.assertIn('sudo /usr/bin/python3 -I', output.getvalue())
            script = path.parent/'install-root.py'
            helpers = runpy.run_path(str(script))
            manifest = json.loads((path.parent/'INSTALL_MANIFEST.json').read_text())
            self.assertEqual(helpers['inventory'](path.parent/'bundle'), manifest['bundle'])
            (path.parent/'bundle/pal-shell-worker').write_text('tampered')
            self.assertNotEqual(helpers['inventory'](path.parent/'bundle'), manifest['bundle'])

    def test_noninteractive_setup_is_refused(self):
        with patch('os.getuid', return_value=1000), patch('os.geteuid', return_value=1000), \
             patch('sys.stdin.isatty', return_value=False), patch('sys.stderr', io.StringIO()):
            self.assertEqual(management_setup.main(None), 1)

    def test_output_inside_bundle_is_refused_before_copying(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle=Path(directory)/'bundle'; (bundle/'_internal').mkdir(parents=True)
            (bundle/'pal-shell-worker').write_text('fixture')
            output=bundle/'setup'
            with patch('os.getuid',return_value=1000), patch('os.geteuid',return_value=1000), \
                 patch('sys.stdin.isatty',return_value=True), patch('sys.stderr',io.StringIO()), \
                 patch('builtins.input',side_effect=['1','',str(bundle),'',str(output)]):
                self.assertEqual(management_setup.main(None),1)
            self.assertFalse(output.exists())


if __name__ == '__main__':
    unittest.main()
