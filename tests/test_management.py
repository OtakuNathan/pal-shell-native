"""Signed helper policy and durable consumption, without host elevation."""
import copy
import json
from pathlib import Path
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pal_shell_worker.management import parse_apt, normalize
from pal_shell_worker import management_helper as helper
from pal_shell_worker.protocol import canonical,digest,RemoteError


class ManagementTests(unittest.TestCase):
    def setUp(self):
        self.key=Ed25519PrivateKey.generate()
        self.config={'client_public_key':self.key.public_key().public_bytes_raw().hex(),
            'client_id':'pal','worker_id':'desktop','target':1,'allowed_actions':['apt_update','apt_install']}
        self.args={'action':'sudo','target':1,'cmd':'apt install cmake ninja-build','cwd':'','wait_ms':1000}
        self.args['management']=normalize(self.args)
        grant={'protocol':'pal-shell-approval.v1','client_id':'pal','worker_id':'desktop','target':1,
            'runtime_epoch':'runtime','operation_id':'operation','nonce':'n'*32,
            'expires_at':time.time()+60,'fingerprint':digest(['privileged',self.args])}
        self.envelope={'approval':grant,'args':self.args,'signature':self.key.sign(canonical(grant)).hex()}

    def test_exact_apt_forms(self):
        self.assertEqual(parse_apt('apt-get update')['action'],'apt_update')
        self.assertEqual(parse_apt('apt install cmake g++')['packages'],['cmake','g++'])
        for cmd in ('sudo apt update','apt upgrade','apt install -y cmake','apt install ./a.deb',
                    'apt install cmake; id','apt update && id','apt install $(id)','apt install a=1',
                    'apt -o APT::Update::Pre-Invoke::=id update','apt update\n','apt remove cmake'):
            with self.subTest(cmd=cmd), self.assertRaises(RemoteError): parse_apt(cmd)

    def test_signature_binds_command_and_target(self):
        self.assertEqual(helper.verify(self.config,self.envelope)['action'],'apt_install')
        for field,value in [('target',2),('worker_id','other'),('runtime_epoch','other'),('expires_at',0)]:
            e=copy.deepcopy(self.envelope);e['approval'][field]=value
            with self.subTest(field=field),self.assertRaises(Exception):helper.verify(self.config,e)
        e=copy.deepcopy(self.envelope);e['args']['cmd']='apt install evil'
        with self.assertRaises(ValueError):helper.verify(self.config,e)
        e=copy.deepcopy(self.envelope);e['args']['management']['packages']=['evil']
        with self.assertRaises(ValueError):helper.verify(self.config,e)

    def test_signed_but_disabled_or_expired_rejected(self):
        with self.assertRaises(ValueError):helper.verify({**self.config,'allowed_actions':[]},self.envelope)
        with self.assertRaises(ValueError):helper.verify(self.config,self.envelope,now=time.time()+100)
        with self.assertRaises(ValueError):helper.verify(self.config,self.envelope,now=time.time()-1000)

    def test_replay_and_concurrent_submit_execute_once(self):
        with tempfile.TemporaryDirectory() as d:
            config={**self.config,'state_directory':d}
            with patch.object(helper,'protected'),patch.object(helper,'consume') as consume,patch.object(helper,'run_child',return_value=0) as run:
                with ThreadPoolExecutor(4) as pool:
                    codes=list(pool.map(lambda _:helper.execute(config,self.envelope),range(4)))
                self.assertEqual(codes.count(0)>=1,True)
                self.assertEqual(run.call_count,1);self.assertEqual(consume.call_count,1)
                self.assertEqual(helper.execute(config,self.envelope),0)
                self.assertEqual(run.call_count,1)

    def test_crash_after_reservation_never_reexecutes(self):
        with tempfile.TemporaryDirectory() as d:
            config={**self.config,'state_directory':d}
            with patch.object(helper,'protected'),patch.object(helper,'consume'),patch.object(helper,'run_child',side_effect=OSError('crash')) as run:
                with self.assertRaises(OSError):helper.execute(config,self.envelope)
                self.assertEqual(helper.execute(config,self.envelope),125)
                self.assertEqual(run.call_count,1)
                self.assertEqual(json.loads(next(Path(d).glob('*.json')).read_text())['state'],'pending')

    def test_unavailable_runtime_never_starts_command(self):
        with tempfile.TemporaryDirectory() as d:
            config={**self.config,'state_directory':d}
            with patch.object(helper,'protected'),patch.object(helper,'consume',side_effect=ValueError('old Runtime')),patch.object(helper,'run_child') as run:
                with self.assertRaises(ValueError):helper.execute(config,self.envelope)
                self.assertEqual(helper.execute(config,self.envelope),126)
                run.assert_not_called()

    def test_fixed_exec_argv(self):
        self.assertEqual(helper.command({'action':'apt_install','packages':['cmake']}),
            ['/usr/bin/apt-get','--assume-yes','--no-remove','install','--','^cmake$'])
        self.assertEqual(helper.command({'action':'shutdown','packages':[]}),['/usr/bin/systemctl','poweroff'])

    @unittest.skipUnless(Path('/usr/bin/apt-get').exists(), 'APT acceptance')
    def test_apt_does_not_expand_package_patterns_or_suffix_operations(self):
        import subprocess
        for name in ('bash.', 'bash+', 'bash-'):
            with self.subTest(name=name):
                argv = helper.command({'action':'apt_install','packages':[name]})
                result = subprocess.run([argv[0], '--simulate', *argv[1:]],
                    capture_output=True, text=True, timeout=30, env={'PATH':'/usr/bin:/bin','LC_ALL':'C'})
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertFalse(any(line.startswith(('Inst ', 'Remv ')) for line in result.stdout.splitlines()), result.stdout)

    def test_literal_package_names_preserve_cpp_and_dots(self):
        import re
        from pal_shell_worker.management import literal_package_selector
        for name in ('g++', 'libstdc++6', 'python3.11', 'bash.', 'bash-', 'bash+'):
            pattern = literal_package_selector(name)
            self.assertIsNotNone(re.fullmatch(pattern, name))
            self.assertIsNone(re.fullmatch(pattern, name + '-extra'))
            if '.' in name:
                self.assertIsNone(re.fullmatch(pattern, name.replace('.', 'x')))

    def test_protected_paths_reject_symlinks_and_user_files(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'config';path.write_text('')
            linked=Path(d)/'linked';linked.symlink_to(path)
            with self.assertRaises(ValueError):helper.protected(linked)


if __name__ == '__main__':
    unittest.main()
