from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
import signal
import tomllib

from .worker import Worker, WorkerConfig


def load_config(path):
    with Path(path).open('rb') as f:
        data = tomllib.load(f)
    for key in ('shutdown_argv', 'protected_machine_ids', 'approvers', 'management_actions'):
        if key in data:
            data[key] = tuple(data[key])
    data['socket_path'] = Path(data['socket_path']).expanduser()
    return WorkerConfig(**data)


async def serve(config):
    worker = Worker(config)
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    import os
    for sig in (() if os.name == "nt" else (signal.SIGINT, signal.SIGTERM)):
        loop.add_signal_handler(sig, stopped.set)
    try:
        await worker.start()
        await stopped.wait()
    finally:
        await worker.close()


def main():
    parser = argparse.ArgumentParser(prog='pal-shell-worker')
    parser.add_argument('--config', type=Path)
    parser.add_argument('--management-helper', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--askpass-config', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--generate-client-key', type=Path, help='Create a private client signing key and print only its public key')
    parser.add_argument('--write-service', type=Path, help='Write a user-service definition to this directory; never activate it')
    parser.add_argument('--executable', type=Path, help='Installed pal-shell-worker executable for the service definition')
    parser.add_argument('--setup-sudo', action='store_true', help='Prepare Linux signed management or enroll Mac Keychain credentials; no activation')
    args, extra = parser.parse_known_args()
    if args.management_helper:
        import sys
        if not sys.platform.startswith('linux'):
            parser.error('Signed sudoers management is Linux-only')
        if len(sys.argv) != 2:
            parser.error('Management entry accepts no additional arguments')
        from .management_helper import main as manage
        sys.argv = [sys.argv[0]]
        raise SystemExit(manage())
    if args.askpass_config:
        import sys
        from .askpass import main as askpass_main
        sys.argv = [sys.argv[0], '--config', str(args.askpass_config), *extra]
        raise SystemExit(askpass_main())
    if extra:
        parser.error('Unexpected arguments')
    if args.setup_sudo:
        if args.config is None or args.generate_client_key or args.write_service:
            parser.error('--setup-sudo requires --config and cannot be combined with key/service generation')
        from .sudo_setup import main as setup_sudo
        raise SystemExit(setup_sudo(load_config(args.config)))
    if args.generate_client_key:
        import os
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        key = Ed25519PrivateKey.generate()
        path = args.generate_client_key.expanduser()
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as file:
            file.write(key.private_bytes_raw().hex() + '\n')
        print(key.public_key().public_bytes_raw().hex())
        return
    if args.config is None:
        parser.error('--config is required to run a worker or write its service definition')
    if args.write_service:
        if args.executable is None:
            parser.error('--write-service requires --executable')
        from .service import definition
        name, data = definition(args.executable, args.config)
        args.write_service.mkdir(parents=True, exist_ok=True)
        path = args.write_service / name
        with path.open('xb') as file:
            file.write(data)
        print(path)
        return
    asyncio.run(serve(load_config(args.config)))


if __name__ == '__main__':
    main()
