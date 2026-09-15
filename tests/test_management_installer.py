"""Administrator installer contracts without elevating the test process."""
import importlib.resources
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


class InstallerTests(unittest.TestCase):
    def setUp(self):
        resource = importlib.resources.files('pal_shell_worker').joinpath('resources/install_root.py.txt')
        self.code = {'__name__':'installer_test'}
        exec(compile(resource.read_text(), str(resource), 'exec'), self.code)
        self.temp = tempfile.TemporaryDirectory(prefix='installer space ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_bundle_rejects_absolute_escaping_and_broken_symlinks(self):
        root = self.root/'bundle'; root.mkdir()
        (root/'file').write_text('data')
        link = root/'link'
        for target in ('../outside', str(root/'file'), 'missing'):
            link.symlink_to(target)
            with self.assertRaises(ValueError): self.code['inventory'](root)
            link.unlink()
        link.symlink_to('file')
        self.assertEqual(self.code['inventory'](root)['link'], {'link':'file'})

    def test_pending_journal_blocks_without_removing_records(self):
        record = self.root/'execution.json'
        record.write_text(json.dumps({'state':'pending'}))
        with patch.dict(self.code, protected=lambda p: None):
            with self.assertRaisesRegex(ValueError, 'Reconcile'):
                self.code['check_pending'](self.root)
            self.assertTrue(record.exists())
            record.write_text(json.dumps({'state':'complete'}))
            self.code['check_pending'](self.root)
            self.assertTrue(record.exists())

    def test_tampered_bundle_or_policy_is_refused_before_publication(self):
        setup=self.root/'setup'; (setup/'bundle').mkdir(parents=True)
        binary=setup/'bundle/pal-shell-worker'; binary.write_text('approved binary')
        policy=setup/'policy'; policy.write_text('approved policy')
        manifest={'bundle':self.code['inventory'](setup/'bundle'),
                  'files':{'policy':self.code['file_hash'](policy)}}
        for name in ('binary','policy'):
            with self.subTest(name=name):
                binary.write_text('approved binary'); policy.write_text('approved policy')
                (binary if name=='binary' else policy).write_text('modified')
                staged=self.root/name; staged.mkdir()
                with self.assertRaisesRegex(ValueError,'changed'):
                    self.code['stage_bundle'](setup,staged,manifest,['policy'])

    def test_publication_failure_restores_previous_files_and_modes(self):
        staged=self.root/'staged'; staged.mkdir()
        backup=self.root/'backup'; backup.mkdir()
        old=self.root/'existing'; old.write_text('old'); old.chmod(0o440)
        new=self.root/'new'
        for name in ('policy','sudoers'): (staged/name).write_text('updated')
        def fail(): raise RuntimeError('visudo rejected')
        with patch.dict(self.code, protected=lambda p: None):
            with self.assertRaisesRegex(RuntimeError, 'visudo'):
                self.code['publish'](staged, {'policy':(old,0o644),'sudoers':(new,0o440)},backup,fail)
        self.assertEqual(old.read_text(),'old')
        self.assertEqual(old.stat().st_mode & 0o777,0o440)
        self.assertFalse(new.exists())
        self.assertEqual((backup/'policy').read_text(),'old')
        self.assertEqual(list(self.root.glob('.pal-install-*')),[])

    def test_repeat_publication_preserves_configuration_backups(self):
        staged=self.root/'staged'; staged.mkdir()
        (staged/'policy').write_text('new')
        target=self.root/'policy'; target.write_text('original')
        with patch.dict(self.code, protected=lambda p: None):
            for i in range(2):
                backup=self.root/str(i); backup.mkdir()
                self.code['publish'](staged,{'policy':(target,0o644)},backup,lambda:None)
        self.assertEqual((self.root/'0/policy').read_text(),'original')
        self.assertEqual((self.root/'1/policy').read_text(),'new')

    def test_nonroot_is_refused_before_reading_installer_input(self):
        with patch('os.geteuid',return_value=1000):
            with self.assertRaisesRegex(ValueError,'sudo command'):
                self.code['install'](self.root/'missing')


if __name__ == '__main__':
    unittest.main()
