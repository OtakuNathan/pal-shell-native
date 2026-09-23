from __future__ import annotations

import os
from pathlib import Path
import subprocess
import signal
import sys
import tempfile
import time
import tomllib

from pal.foundation.sidecar import SidecarEndpoint, SidecarRpcClient, python_subprocess_env
from .port import HubPort


class RemoteHubClient:
    def __init__(self, runtime_root, owner):
        self.runtime_root = Path(runtime_root)
        self.process = self.directory = self.port = None
        self.owner = owner

    def start(self):
        config = self.runtime_root / 'config' / 'remote.toml'
        try:
            targets = tomllib.loads(config.read_text()).get('targets', [])
        except FileNotFoundError:
            targets = []
        if not isinstance(targets, list):
            raise ValueError('remote.toml targets must be an array of tables')
        if not targets:
            # Default-enabled local-only installations need no RPC package or sidecar.
            self.port = HubPort(None)
            return self._attach()
        self.directory = tempfile.TemporaryDirectory(prefix='pal-hub-')
        endpoint = SidecarEndpoint(self.runtime_root, 'remote', runtime_dir_override=Path(self.directory.name))
        self.port = HubPort(endpoint)
        try:
            env = python_subprocess_env()
            # palpkg places this module under runtime/plugins/community/remote,
            # outside site-packages. The sidecar must load this installed generation.
            plugin_root = str(Path(__file__).resolve().parent.parent)
            env['PYTHONPATH'] = os.pathsep.join([plugin_root, *filter(None, env.get('PYTHONPATH', '').split(os.pathsep))])
            self.process = subprocess.Popen([sys.executable, '-m', 'pal_shell_remote.hub', '--config', str(config),
                '--directory', self.directory.name], env=env, start_new_session=True,
                stdin=subprocess.DEVNULL)
            for _ in range(100):
                if self.process.poll() is not None:
                    raise RuntimeError('Remote hub exited during startup')
                if endpoint.socket_path.exists():
                    health = SidecarRpcClient(endpoint, request_timeout_seconds=2, unix_only=True).request_sync('health', {})['result']
                    if health['pid'] != self.process.pid or health['protocol'] != 1:
                        raise RuntimeError('Remote hub ownership mismatch')
                    break
                time.sleep(.05)
            else:
                raise RuntimeError('Remote hub startup timed out')
            return self._attach()
        except BaseException:
            self.close()
            raise

    def _attach(self):
        try:
            if self.port.client and self.owner._shell is not None:
                for target in {t.target for t in self.owner.shell.operations.values()}:
                    reply = self.port.client.request_sync('restore_execution', {
                        'target': target, 'records': self.owner.shell.execution_records(target)})
                    if 'error' in reply:
                        raise RuntimeError('Cannot restore retained remote execution admission')
            self.owner.attach_remote(self.port)
            return self.port
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.port:
            self.port.closed = True
            if self.owner:
                self.owner.detach_remote(self.port)
        if self.process:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    # Killing the local hub does not signal the independent worker.
                    os.killpg(self.process.pid, signal.SIGKILL)
                    self.process.wait(timeout=5)
            self.process = None
        if self.directory:
            self.directory.cleanup()
            self.directory = None
