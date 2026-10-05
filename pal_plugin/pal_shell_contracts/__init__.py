"""Remote configuration and interfaces shared by the host plugin and hub.

This package depends only on the Python standard library.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
from typing import Protocol


@dataclass(frozen=True)
class Target:
    target: int
    name: str
    worker_id: str
    client_id: str
    client_key: str
    socket_path: str
    shortcut: str = ''
    ssh_host: str = ''
    ssh_port: int = 22
    ssh_identity: str = ''
    known_hosts: str = ''
    static: dict = field(default_factory=dict)
    start_actions: dict = field(default_factory=dict)
    worker_port: int = 0

    def __post_init__(self):
        if type(self.target) is not int or self.target <= 0:
            raise ValueError('Remote targets must be positive integers; zero is always local')
        if self.shortcut and not re.fullmatch(r'[A-Za-z0-9_-]{1,54}', self.shortcut):
            raise ValueError('Shortcut must be 1–54 letters, digits, underscores or hyphens')
        if type(self.ssh_port) is not int or not 1 <= self.ssh_port <= 65535:
            raise ValueError('Invalid SSH port')
        if self.ssh_host:
            if not re.fullmatch(r'[A-Za-z0-9_.@-]+', self.ssh_host) or self.ssh_host.startswith('-'):
                raise ValueError('Invalid SSH destination')
            if not self.ssh_identity or not self.known_hosts:
                raise ValueError('SSH requires explicit identity and known_hosts files')
        if type(self.worker_port) is not int or not 0 <= self.worker_port <= 65535:
            raise ValueError('Invalid worker loopback port')
        if self.worker_port and not self.ssh_host:
            raise ValueError('Worker TCP transport requires authenticated SSH forwarding')
        if not self.worker_port and (not Path(self.socket_path).is_absolute() or ':' in self.socket_path or '\n' in self.socket_path):
            raise ValueError('Worker socket must be an absolute forwarding-safe path')
        for argv in self.start_actions.values():
            if not isinstance(argv, list) or not argv or not Path(argv[0]).is_absolute() or not all(isinstance(x, str) for x in argv):
                raise ValueError('Start actions must reference existing executables with literal argv')


class RemoteFailure(RuntimeError):
    def __init__(self, code, message, *, effect='not_started', operation_id=''):
        super().__init__(message)
        self.code, self.effect, self.operation_id = code, effect, operation_id


class RemotePort(Protocol):
    async def request(self, target: int, method: str, params: dict, epoch: str | None = None) -> dict: ...
    async def list(self, refresh: bool = False, *, target: int | None = None) -> list[dict]: ...
