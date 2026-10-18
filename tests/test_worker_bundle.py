"""Accept a standalone worker process, with no Pal or Python installation inside it."""
import asyncio
import json
import os
from pathlib import Path
import shlex
import shutil
import sys
import tempfile
from uuid import uuid4

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pal_shell_worker.client import Connection


async def main(binary, original=None):
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        key = Ed25519PrivateKey.generate()
        config = root/'worker.toml'
        config.write_text('\n'.join(f'{name} = {json.dumps(value)}' for name, value in {
            'worker_id': 'bundle', 'client_id': 'test',
            'client_public_key': key.public_key().public_bytes_raw().hex(),
            'socket_path': str(root/'worker.sock')}.items()))
        env = os.environ.copy()
        if sys.platform.startswith('linux'):
            for name in ('LD_LIBRARY_PATH', 'LD_LIBRARY_PATH_ORIG', 'PAL_EXPECTED_LIBRARY_PATH'):
                env.pop(name, None)
            if original is not None:
                env['LD_LIBRARY_PATH'] = original
                env['PAL_EXPECTED_LIBRARY_PATH'] = original
        process = await asyncio.create_subprocess_exec(str(binary), '--config', str(config), env=env,
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL)
        clients = []
        try:
            # A fresh macOS bundle can take longer to become ready on first launch.
            # This is startup readiness, not a command execution/retry timeout.
            async with asyncio.timeout(30):
                while not (root/'worker.sock').exists():
                    if process.returncode is not None:
                        raise RuntimeError('Standalone worker exited during startup')
                    await asyncio.sleep(.05)
            async def connect(epoch=None):
                client = Connection(root/'worker.sock', client_id='test', worker_id='bundle', private_key=key, epoch=epoch)
                clients.append(client)
                return await client.connect()
            client = await connect()
            oid = uuid4().hex
            gate = root/'finish'
            args = {'operation_id': oid,
                    'cmd': f'while [ ! -f {gate} ]; do sleep .01; done; printf bundle-ok', 'wait_ms': 0}
            await client.request('submit', args)
            epoch = client.epoch
            await client.close()
            client = await connect(epoch)
            await client.request('submit', args)
            result = (await client.request('query', {'operation_id': oid, 'wait_ms': 5000}))['result']
            assert result['status'] == 'running', result
            gate.touch()
            read = uuid4().hex
            await client.request('session', {'operation_id': read, 'session_id': result['session_id'],
                                            'action': 'read', 'wait_ms': 5000})
            result = (await client.request('query', {'operation_id': read, 'wait_ms': 5000}))['result']
            assert result['status'] == 'exited', result
            output = await client.request('output', {'snapshot': result['snapshot'], 'stream': 'stdout'})
            assert output['data'] == b'bundle-ok', output
            await client.request('release', {'output_id': result['output_id']})
            if sys.platform.startswith('linux'):
                # These commands must work without a model adding env -u.
                probe = ('import os,ssl; '
                         'assert (os.environ.get("LD_LIBRARY_PATH") or "")==os.environ.get("PAL_EXPECTED_LIBRARY_PATH", ""), '
                         'repr((os.environ.get("LD_LIBRARY_PATH"),os.environ.get("PAL_EXPECTED_LIBRARY_PATH"))); '
                         'print(ssl.OPENSSL_VERSION)')
                commands = ['python3 -c ' + shlex.quote(probe)]
                if shutil.which('curl'):
                    commands.append('curl --version')
                venv = root/'system-venv'
                commands.append('python3 -m venv --without-pip ' + shlex.quote(str(venv))
                                + ' && ' + shlex.quote(str(venv/'bin/python')) + ' -c "import ssl; print(ssl.OPENSSL_VERSION)"')
                for command in commands:
                    operation = uuid4().hex
                    await client.request('submit', {'operation_id': operation, 'cmd': command, 'wait_ms': 5000})
                    result = (await client.request('query', {'operation_id': operation, 'wait_ms': 5000}))['result']
                    if result['status'] != 'exited' or result['returncode'] != 0:
                        stderr = await client.request('output', {'snapshot': result['snapshot'], 'stream': 'stderr'})
                        raise AssertionError((command, result, stderr))
                    await client.request('release', {'output_id': result['output_id']})
        finally:
            for client in clients:
                await client.close()
            if process.returncode is None:
                process.terminate()
            await asyncio.wait_for(process.wait(), 10)
        assert process.returncode == 0, process.returncode
    print('Standalone worker authentication, disconnect/reconcile, output and clean shutdown passed')


if __name__ == '__main__':
    binary = Path(sys.argv[1]).resolve()
    for original in ((None, '', '/tmp/pal-user-libraries') if sys.platform.startswith('linux') else (None,)):
        asyncio.run(main(binary, original))
