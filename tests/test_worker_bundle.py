"""Accept a standalone worker process, with no Pal or Python installation inside it."""
import asyncio
import json
from pathlib import Path
import sys
import tempfile
from uuid import uuid4

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pal_shell_worker.client import Connection


async def main(binary):
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        key = Ed25519PrivateKey.generate()
        config = root/'worker.toml'
        config.write_text('\n'.join(f'{name} = {json.dumps(value)}' for name, value in {
            'worker_id': 'bundle', 'client_id': 'test',
            'client_public_key': key.public_key().public_bytes_raw().hex(),
            'socket_path': str(root/'worker.sock')}.items()))
        process = await asyncio.create_subprocess_exec(str(binary), '--config', str(config),
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
        finally:
            for client in clients:
                await client.close()
            if process.returncode is None:
                process.terminate()
            await asyncio.wait_for(process.wait(), 10)
        assert process.returncode == 0, process.returncode
    print('Standalone worker authentication, disconnect/reconcile, output and clean shutdown passed')


if __name__ == '__main__':
    asyncio.run(main(Path(sys.argv[1]).resolve()))
