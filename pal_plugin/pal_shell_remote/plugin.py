from __future__ import annotations

import os
from pathlib import Path
import subprocess
import signal
import sys
import tempfile
import time
import tomllib

from pal.core.module_registry import ModuleHandle, MODULE_TIER_DETACHABLE
from pal.foundation.sidecar import SidecarEndpoint, SidecarRpcClient, python_subprocess_env
from .port import HubPort


class RemotePlugin:
    plugin_id = 'remote'
    version = '0.4.0'

    def __init__(self, runtime_root):
        self.runtime_root = Path(runtime_root)
        self.process = self.directory = self.port = self.owner = None

    def start(self, scope):
        self.owner = scope.context.port_registry.get('execution:native_shell_targets')
        if self.owner is None:
            raise RuntimeError('Remote requires the native shell execution backend')
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
            return self._publish(scope)
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
            return self._publish(scope)
        except BaseException:
            self.close()
            raise

    def _publish(self, scope):
        try:
            self.owner.attach_remote(self.port)
            handle = ModuleHandle(module_id='remote', tier=MODULE_TIER_DETACHABLE, detachable=True,
                                  ports={'remote': self.port}, shutdown_sync=self.close)
            scope.context.register_module(handle)
            return handle
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


def build_plugin(context):
    return RemotePlugin(context.runtime_root)
