"""Prepare a Linux signed-sudoers installation; never elevate or activate."""
import json
import os
from pathlib import Path
import shlex
import sys
import tempfile


def main(config):
    if os.getuid()!=os.geteuid() or os.geteuid()==0:
        print('Run setup as the ordinary worker account; an administrator installs the generated files.',file=sys.stderr)
        return 1
    if not sys.stdin.isatty():
        print('Open your own terminal to select management actions.',file=sys.stderr)
        return 1
    instructions = None
    try:
        print('Linux signed management: no sudo password or keyring is used. Each action requires Pal approval.')
        target = int(input('Pal target number [1]: ').strip() or '1')
        if target<=0: raise ValueError('Target must be positive')
        shutdown = input('Allow approved shutdown on this machine? Type YES to enable [disabled]: ').strip()=='YES'
        executable = Path(input('Protected worker executable [/usr/local/libexec/pal-shell-worker/pal-shell-worker]: ').strip() or '/usr/local/libexec/pal-shell-worker/pal-shell-worker')
        if not executable.is_absolute() or any(c in str(executable) for c in '\n\r\0'): raise ValueError('Absolute executable required')
        output = Path(input('Setup output directory [~/.local/share/pal-shell-sudo-setup]: ').strip() or '~/.local/share/pal-shell-sudo-setup').expanduser().resolve()
        output.mkdir(parents=True,exist_ok=True,mode=0o700)
        directory = Path(tempfile.mkdtemp(prefix='setup-',dir=output))
        import pwd
        account = pwd.getpwuid(os.getuid()).pw_name
        if not account.replace('_','').replace('-','').isalnum(): raise ValueError('Unsupported sudoers account name')
        actions = ['apt_update','apt_install'] + (['shutdown'] if shutdown else [])
        policy = {'worker_uid':os.getuid(),'worker_id':config.worker_id,'client_id':config.client_id,
                  'client_public_key':config.client_public_key,'target':target,'worker_socket':str(config.socket_path),
                  'allowed_actions':actions,'protected_machine_ids':list(config.protected_machine_ids),
                  'state_directory':'/var/lib/pal-shell-management'}
        def toml(values):
            return '\n'.join(k+' = '+json.dumps(v) for k,v in values.items())+'\n'
        (directory/'management.toml').write_text(toml(policy))
        (directory/'pal-shell-manage').write_text('#!/bin/sh\nexec '+shlex.quote(str(executable))+' --management-helper\n')
        (directory/'pal-shell-management.sudoers').write_text(
            account+' ALL=(root) NOPASSWD: /usr/local/libexec/pal-shell-manage ""\n')
        (directory/'worker-sudo.toml').write_text(toml({'management_helper':'/usr/local/libexec/pal-shell-manage',
            'management_actions':actions,'shutdown_policy':'approval' if shutdown else 'disabled'}))
        instructions = directory/'NEXT_STEPS.txt'
        q=shlex.quote
        instructions.write_text(f'''Signed management for worker {config.worker_id}, target {target}, account {account}.
No password was requested or saved. No service or installed configuration was changed.

1. Install the COMPLETE matching worker 0.3.0 bundle at {executable}.
   Its program, dependencies and parent paths must be root-owned and not writable
   by group/others. Do not point a root launcher at a user-writable virtualenv.
2. In an administrator terminal, review these generated files, then install:
   sudo install -d -o root -g root -m 0755 /usr/local/etc /usr/local/libexec
   sudo install -d -o root -g root -m 0700 /var/lib/pal-shell-management
   sudo install -o root -g root -m 0644 {q(str(directory/'management.toml'))} /usr/local/etc/pal-shell-management.toml
   sudo install -o root -g root -m 0755 {q(str(directory/'pal-shell-manage'))} /usr/local/libexec/pal-shell-manage
   sudo visudo -cf {q(str(directory/'pal-shell-management.sudoers'))}
   sudo install -o root -g root -m 0440 {q(str(directory/'pal-shell-management.sudoers'))} /etc/sudoers.d/pal-shell-management
   sudo visudo -c
   Never grant NOPASSWD to apt, shutdown, a shell, or the old arbitrary-command helper.
3. Merge worker-sudo.toml into your worker configuration. Preserve SSH, identity,
   runtime paths and other target settings. Coordinate outstanding tasks/output
   before a user-managed worker restart. Update the matching Pal wheel/palpkg.
4. Use run_shell(target={target}, sudo=True, cmd="apt update") and approve once.
   Installation, helper protection and an actual approved execution are separate checks.
   Allowed actions: {', '.join(actions)}. Ordinary commands remain unprivileged.
5. Rollback: remove only /etc/sudoers.d/pal-shell-management, run sudo visudo -c,
   and restore the previous worker configuration/version after coordinating tasks.
   Retain the root operation journal for reconciliation; never delete it to replay an operation.
Existing keyring credentials are untouched. Linux management no longer uses them.
''')
        print('Management templates prepared; administrator installation is still required.')
        return 0
    except (ValueError,OSError,EOFError):
        print('Setup did not complete; installed configuration is unchanged.',file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('Setup cancelled; installed configuration is unchanged.',file=sys.stderr)
        return 130
    finally:
        if instructions:
            print(f'Next step on THIS remote machine: {instructions}\nCopy and run:\n  cat {shlex.quote(str(instructions))}')
