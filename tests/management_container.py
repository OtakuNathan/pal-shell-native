"""Real sudoers/worker/helper acceptance ONLY in the disposable CI container."""
import asyncio
import base64
import json
import os
from pathlib import Path
import pwd
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
    sock = Path('/home/paltest/worker.sock')
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
                           'allowed_actions':['apt_update','apt_install'],'state_directory':str(state)}))
    sudoers = Path('/etc/sudoers.d/pal-shell-management')
    sudoers.write_text('paltest ALL=(root) NOPASSWD: /usr/local/libexec/pal-shell-manage ""\n')
    sudoers.chmod(0o440)
    subprocess.run(['/usr/sbin/visudo','-c'],check=True)
    config = Path('/home/paltest/worker.toml')
    config.write_text(toml({**common,'socket_path':str(sock),'management_helper':str(helper),
                            'management_actions':['apt_update','apt_install'],'shutdown_policy':'disabled'}))
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
        for args, payload in [(['extra'],b''), ([],base64.b64encode(b'{"args":{}}'))]:
            result = await asyncio.to_thread(subprocess.run,prefix+args,input=payload,capture_output=True)
            assert result.returncode != 0, 'Unauthenticated/argument-bearing root invocation accepted'
        for command, wait in [('apt update', 0), ('apt install bash', 1), ('apt update', 300000)]:
            oid = uuid4().hex
            prepared = await client.request('prepare_privileged',{'operation_id':oid,'target':1,
                'action':'sudo','cmd':command,'wait_ms':wait})
            signature = key.sign(canonical(prepared['approval'])).hex()
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
            reconciled = await client.request('query', {'operation_id':oid})
            assert reconciled['management_journal']['state'] == 'complete', reconciled
            # Root verifier sees a genuine, already-consumed signed grant again.
            envelope = {'approval':prepared['approval'],'args':prepared['normalized_args'],'signature':signature}
            before = {p.name:p.read_bytes() for p in state.glob('*.json')}
            replay = await asyncio.to_thread(subprocess.run,prefix,input=base64.b64encode(canonical(envelope)),capture_output=True)
            assert replay.returncode == 0, replay.stderr
            assert before == {p.name:p.read_bytes() for p in state.glob('*.json')}
            await client.request('release',{'output_id':execution['output_id']})
        print('Real sudoers denial, signed apt update/install, durable replay and worker query passed')
    finally:
        if client: await client.close()
        if process.returncode is None: process.terminate()
        await asyncio.wait_for(process.wait(),10)


if __name__ == '__main__':
    asyncio.run(main())
