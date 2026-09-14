"""Regression for Git's silent path filtering inside an enclosing repository."""
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'dependencies' / 'apply_patch.py'


class PatchTests(unittest.TestCase):
    def test_crlf_patch_with_empty_context_applies_and_can_be_repeated(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root/'source'
            source.mkdir()
            target = source/'header.h'
            target.write_bytes(b'\nbefore\n')
            patch = root/'windows.patch'
            patch.write_bytes(b'diff --git a/header.h b/header.h\r\n'
                              b'--- a/header.h\r\n+++ b/header.h\r\n'
                              b'@@ -1,2 +1,2 @@\r\n\r\n-before\r\n+after\r\n')
            command = [sys.executable, str(SCRIPT), 'git', str(source), str(patch)]
            env = {**os.environ, 'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0': 'core.autocrlf',
                   'GIT_CONFIG_VALUE_0': 'true'}
            for _ in range(2):
                subprocess.run(command, check=True, env=env)
                self.assertEqual(target.read_bytes(), b'\nafter\n')

    def test_nested_dependency_is_patched_once_and_bad_input_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(['git', 'init', '-q', str(root)], check=True)
            source = root / 'build' / 'deps' / 'example'
            source.mkdir(parents=True)
            target = source / 'header.h'
            target.write_bytes(b'before\n')  # Downloaded dependency archives use LF on Windows too.
            patch = root / 'fix.patch'
            patch.write_text('diff --git a/header.h b/header.h\n'
                             '--- a/header.h\n+++ b/header.h\n'
                             '@@ -1 +1 @@\n-before\n+after\n')
            command = [sys.executable, str(SCRIPT), 'git', str(source), str(patch)]
            subprocess.run(command, check=True)
            self.assertEqual(target.read_text(), 'after\n')
            subprocess.run(command, check=True)
            self.assertEqual(target.read_text(), 'after\n')
            target.write_text('incompatible\n')
            self.assertNotEqual(subprocess.run(command, capture_output=True).returncode, 0)
            self.assertEqual(target.read_text(), 'incompatible\n')


if __name__ == '__main__':
    unittest.main(verbosity=2)
