"""Root-owned, non-setuid verifier. Only this fixed entry is allowed by sudoers.

All input is untrusted, including the ordinary worker. No passwords are used.
"""
import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
import tomllib

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from .management import normalize, literal_package_selector
from .protocol import canonical, digest, encode, decode

CONFIG = Path('/usr/local/etc/pal-shell-management.toml')
MAX_INPUT = 65536


def protected(path, *, directory=False):
    path = Path(path)
    if not path.is_absolute():
        raise ValueError('Protected paths must be absolute')
    for p in (path, *path.parents):
        s = p.lstat()
        if p.is_symlink() or s.st_uid != 0 or s.st_mode & 0o022:
            raise ValueError('Unprotected helper configuration or state')
    if directory and not path.is_dir():
        raise ValueError('State directory required')


def verify(config, envelope, now=None):
    grant, args = envelope['approval'], envelope['args']
    now = time.time() if now is None else now
    Ed25519PublicKey.from_public_bytes(bytes.fromhex(config['client_public_key'])).verify(
        bytes.fromhex(envelope['signature']), canonical(grant))
    if (grant['protocol'] != 'pal-shell-approval.v1' or grant['worker_id'] != config['worker_id']
            or grant['client_id'] != config['client_id'] or grant['target'] != config['target']
            or args['target'] != config['target'] or not grant['runtime_epoch']
            or not now < grant['expires_at'] <= now + 610
            or grant['fingerprint'] != digest(['privileged', args])):
        raise ValueError('Invalid management grant')
    if not isinstance(grant['nonce'], str) or len(grant['nonce']) < 16:
        raise ValueError('Invalid authorization nonce')
    action = normalize(args)
    if action['action'] not in config['allowed_actions']:
        raise ValueError('Management action is disabled')
    if args.get('management') != action:
        raise ValueError('Approved and normalized actions differ')
    if action['action'] == 'shutdown':
        machine = Path('/etc/machine-id').read_text().strip()
        # Use the same machine identity as worker metadata.
        identity = 'linux:' + machine
        if args.get('protected_machine_id') in (None, '', identity) or identity in config.get('protected_machine_ids', []):
            raise ValueError('Protected or unidentified host')
    return action


def consume(config, envelope):
    import _pal_shell_rpc as wire
    grant = envelope['approval']
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(5)
        sock.connect(config['worker_socket'])
        frame = wire.pack(encode({'method':'consume_management','params':{
            'approval':grant,'signature':envelope['signature']}}))
        response = decode(wire.exchange(sock.fileno(),frame,5000))
    if not response.get('ok') or not response.get('result',{}).get('authorized'):
        raise ValueError('Worker grant is unavailable or Runtime changed')


def save(path, record):
    temporary = path.with_suffix('.tmp')
    fd = os.open(temporary, os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd,'wb') as stream:
            stream.write(canonical(record)); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary,path)
        fd = os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
        try: os.fsync(fd)
        finally: os.close(fd)
    finally:
        temporary.unlink(missing_ok=True)


def command(action):
    if action['action'] == 'apt_update':
        return ['/usr/bin/apt-get','update']
    if action['action'] == 'apt_install':
        return ['/usr/bin/apt-get','--assume-yes','--no-remove','install','--',
                *(literal_package_selector(name) for name in action['packages'])]
    if action['action'] == 'shutdown':
        return ['/usr/bin/systemctl','poweroff']
    raise ValueError('Unsupported action')


def run_child(argv, timeout_ms):
    stopping = False
    def stop(*_):
        nonlocal stopping
        stopping = True
    old = {sig:signal.signal(sig,stop) for sig in (signal.SIGTERM,signal.SIGINT,signal.SIGHUP)}
    parent = os.getppid()
    process = None
    try:
        process = subprocess.Popen(argv,stdin=subprocess.DEVNULL,cwd='/',start_new_session=True,
            env={'PATH':'/usr/sbin:/usr/bin:/sbin:/bin','HOME':'/root','USER':'root','LOGNAME':'root',
                 'LANG':'C.UTF-8','DEBIAN_FRONTEND':'noninteractive'})
        started = time.monotonic(); stopping_at = None
        while process.poll() is None:
            if stopping or os.getppid()!=parent or (timeout_ms and (time.monotonic()-started)*1000>=timeout_ms):
                if stopping_at is None: stopping_at = time.monotonic()
                try: os.killpg(process.pid,signal.SIGKILL if time.monotonic()-stopping_at>2 else signal.SIGTERM)
                except ProcessLookupError: pass
            time.sleep(.05)
        return process.returncode
    finally:
        if process:
            try: os.killpg(process.pid,signal.SIGKILL)
            except ProcessLookupError: pass
        for sig, handler in old.items(): signal.signal(sig,handler)


def execute(config, envelope):
    action = verify(config,envelope)
    grant = envelope['approval']
    root = Path(config['state_directory']); protected(root,directory=True)
    key = digest([grant['client_id'],grant['runtime_epoch'],grant['operation_id']])
    path = root/(key+'.json')
    lock_fd = os.open(root/'journal.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
    with os.fdopen(lock_fd,'r+') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        if path.exists():
            previous = json.loads(path.read_text())
            if previous['fingerprint'] != grant['fingerprint']:
                raise ValueError('Operation identity conflict')
            # Replaying a grant cannot create another process, even after a crash.
            return previous.get('returncode',125)
        records = list(root.glob('*.json'))
        for item in records:
            old = json.loads(item.read_text())
            if old['state'] in ('complete','not_started') and old['expires_at'] < time.time()-86400:
                item.unlink()
        if len(list(root.glob('*.json'))) >= 4096:
            raise ValueError('Management journal full; reconcile retained records')
        record = {'state':'pending','fingerprint':grant['fingerprint'],'expires_at':grant['expires_at'],
                  'operation_id':grant['operation_id'],'runtime_epoch':grant['runtime_epoch']}
        save(path,record)
    try:
        consume(config,envelope)
    except Exception:
        save(path,{**record,'state':'not_started','returncode':126})
        raise
    # Once process creation is attempted, a crash leaves pending/UNKNOWN.
    code = run_child(command(action), envelope['args'].get('timeout_ms'))
    save(path,{**record,'state':'complete','returncode':code})
    return code if code>=0 else 128-code


def main():
    try:
        if os.geteuid()!=0 or len(sys.argv)!=1:
            raise ValueError('Use the fixed sudoers management entry')
        protected(CONFIG)
        config = tomllib.loads(CONFIG.read_text())
        if os.environ.get('SUDO_UID') != str(config['worker_uid']):
            raise ValueError('Unexpected invoking account')
        raw = sys.stdin.buffer.read(MAX_INPUT+1)
        if len(raw)>MAX_INPUT:
            raise ValueError('Management envelope limit exceeded')
        data = json.loads(base64.b64decode(raw,validate=True))
        mode = data.get('mode','execute')
        if mode == 'status':
            protected(Path(config['state_directory']),directory=True)
            print(json.dumps({'ok':True,'mode':'signed_sudoers','allowed_actions':config['allowed_actions']}))
            return 0
        if mode == 'query':
            key = digest([config['client_id'],data['runtime_epoch'],data['operation_id']])
            root = Path(config['state_directory']); protected(root,directory=True)
            path = root/(key+'.json')
            print(path.read_text() if path.exists() else '{"state":"unknown"}')
            return 0
        if mode != 'execute':
            raise ValueError('Unsupported management request')
        return execute(config,data)
    except Exception:
        print('Management authorization/configuration failed; inspect signed operation status.',file=sys.stderr)
        return 126


if __name__ == '__main__':
    raise SystemExit(main())
