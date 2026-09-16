"""Prepare a Linux signed-sudoers installation; never elevate or activate."""
import json
import os
from pathlib import Path
import shlex
import sys
import tempfile
import hashlib
import importlib.resources
import shutil


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
        print('To change an existing installation, rerun this wizard and install the new policy, then merge worker-sudo.toml.')
        target = int(input('Pal target number [1]: ').strip() or '1')
        if target<=0: raise ValueError('Target must be positive')
        shutdown = input('Allow approved shutdown on this machine? Type YES to enable [disabled]: ').strip()=='YES'
        default_bundle = str(Path(sys.executable).parent) if getattr(sys, 'frozen', False) else ''
        bundle_text = input(f'Complete extracted worker bundle directory [{default_bundle or "required; contains pal-shell-worker and _internal"}]: ').strip() or default_bundle
        bundle = Path(bundle_text).expanduser().resolve()
        if not bundle_text or not (bundle/'pal-shell-worker').is_file() or not (bundle/'_internal').is_dir():
            raise ValueError('A complete standalone worker bundle is required, not a virtualenv')
        template = importlib.resources.files('pal_shell_worker').joinpath('resources/install_root.py.txt').read_text()
        # The same stdlib implementation checks both prepared and installed trees.
        installer = {'__name__':'prepared_installer'}
        exec(compile(template, 'install-root.py', 'exec'), installer)
        inventory = installer['inventory'](bundle)
        bundle_id = hashlib.sha256(json.dumps(inventory, sort_keys=True).encode()).hexdigest()[:16]
        default_executable = f'/usr/local/libexec/pal-shell-worker-{bundle_id}/pal-shell-worker'
        executable = Path(input(f'Protected worker executable [{default_executable}]: ').strip() or default_executable)
        if not executable.is_absolute() or any(c in str(executable) for c in '\n\r\0'): raise ValueError('Absolute executable required')
        if executable.name != 'pal-shell-worker' or executable.parent.parent != Path('/usr/local/libexec') or not executable.parent.name.startswith('pal-shell-worker-'):
            raise ValueError('Choose /usr/local/libexec/pal-shell-worker-VERSION/pal-shell-worker')
        output = Path(input('Setup output directory [~/.local/share/pal-shell-sudo-setup]: ').strip() or '~/.local/share/pal-shell-sudo-setup').expanduser().resolve()
        if output.is_relative_to(bundle):
            raise ValueError('Setup output must be outside the worker bundle')
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
        shutil.copytree(bundle, directory/'bundle', symlinks=True)
        if installer['inventory'](directory/'bundle') != inventory:
            raise ValueError('Worker bundle changed during preparation')
        (directory/'install-root.py').write_text(template)
        manifest = {'destination':str(executable.parent), 'bundle':inventory,
                    'files':{name:installer['file_hash'](directory/name) for name in
                             ('management.toml','pal-shell-manage','pal-shell-management.sudoers')}}
        (directory/'INSTALL_MANIFEST.json').write_text(json.dumps(manifest, sort_keys=True, indent=2)+'\n')
        instructions = directory/'NEXT_STEPS.txt'
        q=shlex.quote
        instructions.write_text(f'''Signed management for worker {config.worker_id}, target {target}, account {account}.
No password was requested or saved. No service or installed configuration was changed.

Recommended administrator installation, after reviewing install-root.py,
INSTALL_MANIFEST.json and the policy/sudoers files:
  sudo /usr/bin/python3 -I {q(str(directory/'install-root.py'))}

The prepared directory includes the complete worker bundle. The manifest detects
changes since preparation; it is not a publisher signature. Use trusted release
assets. The installer copies and verifies files in a root-owned staging directory,
runs visudo -cf and visudo -c, and backs up replaced configuration. It restores
configuration if publication/validation fails. Identical bundles can be reused;
a different existing bundle requires a new versioned destination. Pending root
operations must be reconciled first. Root journals are never deleted.
No apt command, password storage, worker configuration change or service restart
is performed by the installer. After it succeeds, continue at step 3 below.

Manual equivalent (optional):

1. Install the COMPLETE matching worker 0.4.0 bundle at {executable}.
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
   Shutdown policy: {'approval' if shutdown else 'disabled'}. To change it later, rerun --setup-sudo;
   update BOTH the administrator policy and worker-sudo.toml, then coordinate worker restart.
   Verify list_remote reports management.shutdown.supported={'true' if shutdown else 'false'}.
   remote_power(target={target}, action="shutdown") requests a real shutdown after approval;
   do not use it as an installation probe.
5. Rollback: remove only /etc/sudoers.d/pal-shell-management, run sudo visudo -c,
   and restore the previous worker configuration/version after coordinating tasks.
   Retain the root operation journal for reconciliation; never delete it to replay an operation.
Existing keyring credentials are untouched. Linux management no longer uses them.
''')
        print('Management templates prepared; administrator installation is still required.')
        return 0
    except (ValueError,OSError,EOFError) as error:
        print(f'Setup did not complete; installed configuration is unchanged: {error}',file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('Setup cancelled; installed configuration is unchanged.',file=sys.stderr)
        return 130
    finally:
        if instructions:
            print(f'Next step on THIS remote machine: {instructions}\nReview:\n  cat {shlex.quote(str(instructions))}\nInstall from your own terminal:\n  sudo /usr/bin/python3 -I {shlex.quote(str(instructions.parent/"install-root.py"))}')
