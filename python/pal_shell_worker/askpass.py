"""Private sudo authentication channel. Never import this into Pal.

Install a root-owned launcher with a fixed root-owned configuration path.
Stdout is exclusively sudo's askpass pipe; no secret is sent to native output.
"""
import argparse
import base64
import json
import socket
import time
import os
from pathlib import Path
import subprocess
import sys
import tomllib


def authorize(config):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from .protocol import canonical, digest, encode, decode
    import _pal_shell_rpc as wire
    raw = os.environ.get('PAL_SHELL_APPROVAL', '')
    if len(raw) > 65536:
        return False
    envelope = json.loads(base64.urlsafe_b64decode(raw))
    grant, args, signature = envelope['approval'], envelope['args'], envelope['signature']
    key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(config['client_public_key']))
    key.verify(bytes.fromhex(signature), canonical(grant))
    if (grant['protocol'] != 'pal-shell-approval.v1' or grant['worker_id'] != config['worker_id'] or
        grant['client_id'] != config['client_id'] or grant['expires_at'] < time.time() or
        args['action'] != 'sudo' or grant['fingerprint'] != digest(['privileged', args])):
        return False
    # Merely printing Password: or directly invoking this launcher is not enough.
    # The requesting parent must be a privileged sudo executing the signed command.
    parent = os.getppid()
    observed = subprocess.run(['/bin/ps', '-ww', '-p', str(parent), '-o', 'uid=', '-o', 'args='],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        timeout=2, check=False).stdout.decode().strip()
    fields = observed.split(None, 1)
    expected = ' '.join(['/usr/bin/sudo', '-A', '-k', '--', config['privilege_helper'], config['shell'], args['cmd']])
    if len(fields) != 2 or fields[0] != '0' or fields[1] != expected or os.getppid() != parent:
        return False
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(2)
        connection.connect(config['worker_socket'])
        frame = wire.pack(encode({'method': 'consume_privileged', 'params': {'approval': grant, 'signature': signature}}))
        response = decode(wire.exchange(connection.fileno(), frame, 5000))
    return bool(response.get('ok') and response.get('result', {}).get('authorized'))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True, type=Path)
    args, _ = parser.parse_known_args()  # sudo appends its human-readable prompt.
    path = args.config
    try:
        for part in (path, *path.parents):
            s = part.lstat()
            if part.is_symlink() or s.st_uid != 0 or s.st_mode & 0o022:
                return 1
        config = tomllib.loads(path.read_text())
        if not authorize(config):
            return 1
        if sys.platform == 'darwin' and config['store'] == 'keychain':
            argv = ['/usr/bin/security', 'find-generic-password', '-s', config['service'], '-a', config['account'], '-w']
        elif sys.platform.startswith('linux') and config['store'] == 'secret-service':
            argv = ['/usr/bin/secret-tool', 'lookup', 'service', config['service'], 'account', config['account']]
        else:
            return 1
        # Forward directly to the dedicated sudo pipe, never Python logging or
        # the shell session. Locked/missing stores fail instead of writing files.
        result = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=sys.stdout.buffer,
                                stderr=subprocess.DEVNULL, timeout=15, check=False)
        return result.returncode
    except Exception:
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
