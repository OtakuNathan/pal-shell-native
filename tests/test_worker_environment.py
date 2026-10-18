import os
import sys
import unittest
from unittest.mock import patch

from pal_shell_worker.environment import restore_subprocess_environment


class WorkerEnvironmentTests(unittest.TestCase):
    def test_frozen_linux_restores_original_including_empty_value(self):
        for original in ('/user/libraries', ''):
            with self.subTest(original=original), patch.dict(os.environ, {
                'LD_LIBRARY_PATH': '/bundle/libraries',
                'LD_LIBRARY_PATH_ORIG': original,
                'PATH': '/user/bin',
            }, clear=True), patch.object(sys, 'frozen', True, create=True), patch.object(sys, 'platform', 'linux'):
                restore_subprocess_environment()
                restore_subprocess_environment()
                self.assertEqual(os.environ['LD_LIBRARY_PATH'], original)
                self.assertEqual(os.environ['PATH'], '/user/bin')

    def test_frozen_linux_removes_bundle_path_without_original(self):
        with patch.dict(os.environ, {'LD_LIBRARY_PATH': '/bundle/libraries'}, clear=True), \
                patch.object(sys, 'frozen', True, create=True), patch.object(sys, 'platform', 'linux'):
            restore_subprocess_environment()
            self.assertNotIn('LD_LIBRARY_PATH', os.environ)

    def test_unfrozen_and_other_platforms_preserve_environment(self):
        for frozen, platform in ((False, 'linux'), (True, 'darwin'), (True, 'win32')):
            with self.subTest(frozen=frozen, platform=platform), patch.dict(os.environ, {
                'LD_LIBRARY_PATH': '/user/libraries', 'LD_LIBRARY_PATH_ORIG': '/other',
            }, clear=True), patch.object(sys, 'frozen', frozen, create=True), patch.object(sys, 'platform', platform):
                before = dict(os.environ)
                restore_subprocess_environment()
                self.assertEqual(dict(os.environ), before)


if __name__ == '__main__':
    unittest.main()
