"""Interactive remote-user credential enrollment; never receives a password."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile


def store_commands(platform, service, account):
    if platform == 'darwin':
        # Apple security prompts on the controlling terminal when -w is last.
        # Do not append a password argument or pipe it through Python.
        return ('keychain',
                ['/usr/bin/security', 'add-generic-password', '-U', '-s', service,
                 '-a', account, '-T', '/usr/bin/security', '-w'],
                ['/usr/bin/security', 'find-generic-password', '-s', service, '-a', account, '-w'])
    if platform.startswith('linux'):
        return ('secret-service',
                ['/usr/bin/secret-tool', 'store', '--label=Pal remote sudo', 'service', service, 'account', account],
                ['/usr/bin/secret-tool', 'lookup', 'service', service, 'account', account])
    raise ValueError('This platform does not support remote sudo setup')


def absolute_path(prompt, default):
    value = input(f'{prompt} [{default}]: ').strip() or default
    path = Path(value).expanduser()
    if not path.is_absolute() or any(c in str(path) for c in ('\n', '\r', '\0')):
        raise ValueError('An absolute path without control characters is required')
    return str(path)


def write_plan(directory, config, *, account, store, service, executable, helper, launcher, auth_path):
    """Write only public references/templates; never overwrite the worker config."""
    def toml(values):
        return '\n'.join(f'{key} = {json.dumps(value, ensure_ascii=False)}' for key, value in values.items()) + '\n'
    auth = dict(store=store, service=service, account=account,
                client_public_key=config.client_public_key, client_id=config.client_id,
                worker_id=config.worker_id, worker_socket=str(config.socket_path),
                privilege_helper=helper, shell=config.shell)
    (directory/'auth.toml').write_text(toml(auth))
    (directory/'askpass').write_text('#!/bin/sh\nexec ' + shlex.join(
        [executable, '--askpass-config', auth_path]) + ' "$@"\n')
    (directory/'worker-sudo.toml').write_text(toml(dict(privilege_helper=helper, askpass_helper=launcher)))
    instructions = f'''Sudo setup for worker {config.worker_id}, OS account {account}.

This directory contains no password. The worker configuration was not changed.
An administrator must install a complete protected pal-shell-worker distribution
at {executable}, and pal-shell-privileged at {helper} (root-owned 0755, never setuid).
The interpreter, imported modules and all parent directories must also be protected.
Install auth.toml as {auth_path} (root-owned 0644) and askpass as {launcher}
(root-owned 0755). Their parent paths must be root-owned, not symlinks, and not
group/world writable. Do not simply chown a launcher pointing into a user-writable
Python environment. The launcher configuration pins the approved worker identity.

Merge the two fields from worker-sudo.toml into the existing worker configuration;
preserve all other settings. Coordinate active sessions before restarting the
worker: a worker restart loses native sessions. This wizard restarts nothing.

Run the worker under this same OS account with access to its unlocked credential
store. Linux requires a user DBus/Secret Service session; macOS may require Keychain
authorization in the logged-in user's session. This is not a cross-user vault.

After installation, use Pal's sudo=True path to approve a harmless id command.
Credential storage/readability alone does not prove sudo policy or the complete
approval/askpass path. Every sudo command still needs its own Pal approval.
Repeat this wizard as this account to replace a changed password. Existing store
entries are updated only after the explicit STORE confirmation. Never paste the
password into Pal, an ordinary shell command, or a remote shell PTY transcript.
'''
    (directory/'NEXT_STEPS.txt').write_text(instructions)


def main(config):
    if not (sys.platform == 'darwin' or sys.platform.startswith('linux')):
        print('Remote sudo setup is unsupported on this platform.', file=sys.stderr)
        return 1
    if os.geteuid() == 0 or os.getuid() != os.geteuid():
        print('Run this wizard as the ordinary worker account, not through sudo/root.', file=sys.stderr)
        return 1
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        print('Open a remote terminal yourself; password setup requires an interactive terminal.', file=sys.stderr)
        return 1
    import pwd
    account = pwd.getpwuid(os.getuid()).pw_name
    service = f'pal-shell:{config.worker_id}:sudo'
    store, enroll, lookup = store_commands(sys.platform, service, account)
    if not Path(enroll[0]).is_file():
        print(f'Install {enroll[0]} on this host before credential enrollment.', file=sys.stderr)
        return 1
    if store == 'secret-service' and not os.environ.get('DBUS_SESSION_BUS_ADDRESS'):
        print('An unlocked user Secret Service/DBus session is required; no plaintext fallback.', file=sys.stderr)
        return 1
    print(f'Remote sudo setup: worker={config.worker_id}, OS account={account}, store={store}')
    print('Run in your own terminal, not a Pal-managed shell. The OS tool will ask for the password without echo.')
    print('This authorizes storing/replacing the credential, not running sudo commands or changing power policy.')
    try:
        executable = absolute_path('Protected worker executable', '/usr/local/libexec/pal-shell-worker/pal-shell-worker')
        helper = absolute_path('Protected privilege helper', config.privilege_helper or '/usr/local/libexec/pal-shell-privileged')
        launcher = absolute_path('Protected askpass launcher', config.askpass_helper or '/usr/local/libexec/pal-shell-askpass')
        auth_path = absolute_path('Protected authentication config', '/usr/local/etc/pal-shell-auth.toml')
        output = Path(absolute_path('Setup output directory', str(Path.home()/'.local/share/pal-shell-sudo-setup')))
        output.mkdir(parents=True, exist_ok=True)
        directory = Path(tempfile.mkdtemp(prefix='setup-', dir=output))
        write_plan(directory, config, account=account, store=store, service=service,
                   executable=executable, helper=helper, launcher=launcher, auth_path=auth_path)
        print(f'Configuration templates: {directory}')
        print(f'Credential reference: service={service}, account={account}')
        if input('Type STORE to store/replace the remote sudo password, or Enter to leave templates only: ').strip() != 'STORE':
            print('Templates prepared; credential unchanged. Follow NEXT_STEPS.txt to complete setup.')
            return 0
        # stdin/stderr stay on the user's terminal. Passwords never enter Python,
        # argv, environment variables, captured output, or generated files.
        result = subprocess.run(enroll, stdout=subprocess.DEVNULL, timeout=300, check=False)
        if result.returncode:
            print('Credential enrollment failed; check the unlocked OS store. No plaintext fallback.', file=sys.stderr)
            return 1
        result = subprocess.run(lookup, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=15, check=False)
        if result.returncode:
            print('Credential stored, but read access is unavailable. Check store permissions/unlock before activation.', file=sys.stderr)
            return 1
        print('Credential stored and readable by this account. Sudo is not yet verified; follow NEXT_STEPS.txt.')
        return 0
    except (OSError, ValueError, subprocess.TimeoutExpired, EOFError):
        print('Setup did not complete. Check paths, terminal and credential store; existing worker config is unchanged.', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('\nSetup interrupted; check enrollment status before retrying.', file=sys.stderr)
        return 130
