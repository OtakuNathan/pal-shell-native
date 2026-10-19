"""Real sudoers/worker/helper acceptance ONLY in the disposable CI container."""
import asyncio
import base64
import json
import os
from pathlib import Path
import pwd
import shlex
import subprocess
import sys
from uuid import uuid4

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pal_shell_worker.client import Connection
from pal_shell_worker.protocol import canonical


async def main():
    assert os.environ.get('PAL_MANAGEMENT_CONTAINER') == '1' and Path('/.dockerenv').exists()
    assert os.geteuid() == 0
    user = pwd.getpwnam('paltest')
    key = Ed25519PrivateKey.generate()
    runtime_dir = Path('/home/paltest/.pal-shell-run')
    runtime_dir.mkdir(mode=0o700)
    os.chown(runtime_dir, user.pw_uid, user.pw_gid)
    sock = runtime_dir / 'worker.sock'
    helper = Path('/usr/local/libexec/pal-shell-manage')
    helper.parent.mkdir(exist_ok=True)
    # Make helper startup slower than an async run snapshot, deterministically.
    helper.write_text('#!/bin/sh\n/bin/sleep .1\nexec /usr/local/bin/python -m pal_shell_worker --management-helper\n')
    helper.chmod(0o755)
    state = Path('/var/lib/pal-shell-management'); state.mkdir(mode=0o700)
    common = {'worker_id':'container','client_id':'pal','client_public_key':key.public_key().public_bytes_raw().hex()}
    def toml(data):
        return '\n'.join(k+' = '+json.dumps(v) for k,v in data.items())+'\n'
    policy = Path('/usr/local/etc/pal-shell-management.toml')
    policy.write_text(toml({**common,'worker_uid':user.pw_uid,'target':1,'worker_socket':str(sock),
                           'allowed_actions':['apt_update','apt_install','shell_command'],'state_directory':str(state)}))
    sudoers = Path('/etc/sudoers.d/pal-shell-management')
    sudoers.write_text('paltest ALL=(root) NOPASSWD: /usr/local/libexec/pal-shell-manage ""\n')
    sudoers.chmod(0o440)
    subprocess.run(['/usr/sbin/visudo','-c'],check=True)
    config = Path('/home/paltest/worker.toml')
    config.write_text(toml({**common,'socket_path':str(sock),'management_helper':str(helper),
                            'management_actions':['apt_update','apt_install','shell_command'],'shutdown_policy':'disabled'}))
    # Exercise the generated installer with a complete test bundle. Its executable
    # delegates to the container's root-owned Python installation; no host files
    # or credentials are used. The real bundled binary is covered by bundle CI.
    bundle = Path('/home/paltest/fixture-bundle')
    (bundle/'_internal').mkdir(parents=True)
    (bundle/'pal-shell-worker').write_text('#!/bin/sh\n/bin/sleep .1\nexec /usr/local/bin/python -m pal_shell_worker "$@"\n')
    (bundle/'pal-shell-worker').chmod(0o755)
    prepare = '''
from pathlib import Path
from unittest.mock import patch
from pal_shell_worker.__main__ import load_config
from pal_shell_worker.management_setup import main
answers = ['1', '', 'YES', '/home/paltest/fixture-bundle',
           '/usr/local/libexec/pal-shell-worker-fixture/pal-shell-worker', '/home/paltest/setup']
with patch('sys.stdin.isatty', return_value=True), patch('builtins.input', side_effect=answers):
    assert main(load_config('/home/paltest/worker.toml')) == 0
'''
    subprocess.run(['/usr/sbin/runuser','-u','paltest','--',sys.executable,'-c',prepare],check=True)
    setup = next(Path('/home/paltest/setup').glob('setup-*'))
    for _ in range(2):
        subprocess.run([sys.executable,'-I',str(setup/'install-root.py')],check=True)
    assert config.read_text() == toml({**common,'socket_path':str(sock),'management_helper':str(helper),
                                     'management_actions':['apt_update','apt_install','shell_command'],'shutdown_policy':'disabled'})
    assert len(list((state/'install-backups').iterdir())) == 2
    # No repository network access or package mutation is needed for acceptance:
    # update an empty source set, then install the already-installed bash package.
    for path in Path('/etc/apt/sources.list.d').glob('*'): path.unlink()
    Path('/etc/apt/sources.list').write_text('')
    process = await asyncio.create_subprocess_exec('/usr/sbin/runuser','-u','paltest','--',
        sys.executable,'-m','pal_shell_worker','--config',str(config))
    client = None
    try:
        async with asyncio.timeout(20):
            while not sock.exists():
                assert process.returncode is None
                await asyncio.sleep(.05)
        client = await Connection(sock,client_id='pal',private_key=key,worker_id='container').connect()
        info = await client.request('metadata',{'refresh':True})
        assert info['privilege']['installation']['ok'], info
        assert not info['power']['shutdown'], info
        prefix = ['/usr/sbin/runuser','-u','paltest','--','/usr/bin/sudo','-n','--',str(helper)]
        status_payload = base64.b64encode(canonical({'mode': 'status'}))
        probe = await asyncio.to_thread(subprocess.run, prefix, input=status_payload, capture_output=True)
        assert probe.returncode == 0, probe.stderr  # This fixed sudoers entry is usable.
        ordinary = uuid4().hex
        command = 'printf %s ' + shlex.quote(status_payload.decode()) + ' | ' + shlex.join(prefix[4:])
        await client.request('submit', {'operation_id': ordinary, 'cmd': command, 'wait_ms': 300000})
        blocked = (await client.request('query', {'operation_id': ordinary, 'wait_ms': 300000}))['result']
        assert blocked['returncode'] != 0, 'Ordinary shell used NOPASSWD sudo without approval'
        await client.request('release', {'output_id': blocked['output_id']})
        for args, payload in [(['extra'],b''), ([],base64.b64encode(b'{"args":{}}'))]:
            result = await asyncio.to_thread(subprocess.run,prefix+args,input=payload,capture_output=True)
            assert result.returncode != 0, 'Unauthenticated/argument-bearing root invocation accepted'
        marker = Path('/var/tmp/pal-approved-command')
        stdin_marker = Path('/var/tmp/pal-approved-stdin')
        stdin_text = '  中文\x00\r\n\"\' $HOME $(false) EOF  '
        general = '/usr/bin/id -u > ' + shlex.quote(str(marker))
        for command, wait, stdin in [('apt update', 0, None), ('apt install bash', 1, None),
                                     ('apt update', 300000, None), (general, 0, None),
                                     ('cat >> ' + str(stdin_marker), 0, stdin_text)]:
            oid = uuid4().hex
            prepared = await client.request('prepare_privileged',{'operation_id':oid,'target':1,
                'action':'sudo','cmd':command,'wait_ms':wait, **({'stdin': stdin} if stdin is not None else {})})
            signature = key.sign(canonical(prepared['approval'])).hex()
            if command == general:
                assert not marker.exists(), 'Root command ran before approval was committed'
                bad = await asyncio.to_thread(subprocess.run, prefix,
                    input=base64.b64encode(canonical({'approval': prepared['approval'],
                        'args': {**prepared['normalized_args'], 'cmd': 'touch /var/tmp/tampered'},
                        'signature': signature})), capture_output=True)
                assert bad.returncode != 0, 'Root verifier accepted an altered approved script'
            if stdin is not None:
                bad = await asyncio.to_thread(subprocess.run, prefix,
                    input=base64.b64encode(canonical({'approval': prepared['approval'],
                        'args': {**prepared['normalized_args'], 'stdin': stdin + 'tampered'},
                        'signature': signature})), capture_output=True)
                assert bad.returncode != 0 and not stdin_marker.exists()
            await client.request('commit_privileged',{'operation_id':oid,'signature':signature})
            result = await client.request('query',{'operation_id':oid,'wait_ms':300000},timeout_ms=310000)
            execution = result['result']
            async with asyncio.timeout(30):
                while execution['status'] == 'running':
                    read = uuid4().hex
                    await client.request('session', {'operation_id':read,'action':'read',
                        'session_id':execution['session_id'],'wait_ms':5000})
                    execution = (await client.request('query', {'operation_id':read,'wait_ms':5000}))['result']
            assert execution['returncode'] == 0, execution
            if command == general:
                assert marker.read_text().strip() == '0', 'Approved general command did not run as root'
                assert not Path('/var/tmp/tampered').exists()
            reconciled = await client.request('query', {'operation_id':oid})
            assert reconciled['management_journal']['state'] == 'complete', reconciled
            # Root verifier sees a genuine, already-consumed signed grant again.
            envelope = {'approval':prepared['approval'],'args':prepared['normalized_args'],'signature':signature}
            before = {p.name:p.read_bytes() for p in state.glob('*.json')}
            replay = await asyncio.to_thread(subprocess.run,prefix,input=base64.b64encode(canonical(envelope)),capture_output=True)
            assert replay.returncode == 0, replay.stderr
            assert before == {p.name:p.read_bytes() for p in state.glob('*.json')}
            if stdin is not None:
                assert stdin_marker.read_bytes() == stdin.encode(), 'Input changed or replay executed twice'
                assert stdin_marker.stat().st_uid == 0
            await client.request('release',{'output_id':execution['output_id']})
        print('Real sudoers denial, ordinary elevation guard, signed APT/general root commands, durable replay and worker query passed')
    finally:
        if client: await client.close()
        if process.returncode is None: process.terminate()
        await asyncio.wait_for(process.wait(),10)


if __name__ == '__main__':
    asyncio.run(main())
