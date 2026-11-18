"""Supervise one owned C++ subprocess and its private Typed RPC connection."""
from __future__ import annotations

import asyncio
from contextlib import suppress
import fcntl
import importlib.metadata
import itertools
import json
import os
from pathlib import Path
import shutil
import socket
import struct
import subprocess
import tempfile
from uuid import uuid4

from pal_shell_contracts.package_layout import packaged_engine, platform_key

from . import local_wire

VERSION = "0.9.0"
_RANGE = 1 << 20
_FIRST = 1 << 47
_END = 1 << 48
_PENDING_LIMIT = 128
_PRIVATE_IDS = itertools.count(1, _RANGE)


def _engine_source(package_root):
    if package_root is not None and (package_root / "backend").exists():
        source = packaged_engine(package_root)
    elif package_root is not None and (package_root / ".pal-package.json").is_file():
        raise RuntimeError("remote package is missing its bundled local engine")
    elif path := os.environ.get("PAL_SHELL_LOCAL_WORKER"):
        # Explicit source-tree test/development launch. Never used by a .palpkg.
        source = Path(path)
    else:
        source = Path(importlib.metadata.distribution("pal-shell-native").locate_file("libexec/pal-shell-local-worker"))
    if not source.is_file() or source.is_symlink():
        raise RuntimeError(f"Missing local engine for {platform_key()}")
    return source


def _reserve_ids(directory):
    if directory is None:
        # Isolated role/test owners do not share IDs with the resident plugin.
        first = next(_PRIVATE_IDS)
        return first, first + _RANGE
    with (directory / "ids.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = directory / "next-id"
        first = json.loads(path.read_text())["next"] if path.exists() else _FIRST
        if not _FIRST <= first <= _END - _RANGE or first % _RANGE:
            raise RuntimeError("Local engine session ID range is invalid or exhausted")
        from pal.packages.process import atomic_json

        atomic_json(path, {"next": first + _RANGE})
        return first, first + _RANGE


def _header(data):
    size, = struct.unpack("!I", data)
    if not 8 <= size <= local_wire.FRAME_LIMIT:
        raise ValueError("invalid local engine frame")
    return size


def _sync_read(sock, size):
    chunks = bytearray()
    while len(chunks) < size:
        chunk = sock.recv(size - len(chunks))
        if not chunk:
            raise EOFError("local engine closed before ready")
        chunks.extend(chunk)
    return bytes(chunks)


_SESSION_FIELDS = {
    "read": ("wait",), "watch": ("wait", "timeout"), "extend": ("timeout",),
    "write": ("text",), "resize": ("rows", "columns"), "acknowledge": ("terminal",),
    "unwatch": (), "terminate": (), "release": (), "release_output": (),
    "cancel_request": (), "release_write": (),
}


def _command(number, operation, args, approval, epoch):
    value = {"request": number, "operation": operation}
    if operation == "run":
        shell, text, cwd, tty, wait, timeout, stdin = args
        value.update(shell=shell, text=text, cwd=cwd or os.getcwd(),
                     tty=tty, wait=wait, timeout=timeout, has_stdin=stdin is not None,
                     stdin=stdin if stdin is not None else b"", authorized=approval is not None,
                     approval=approval or b"", epoch=epoch)
    elif operation != "acquire_write":
        session, *rest = args
        value.update(session=session, **dict(zip(_SESSION_FIELDS[operation], rest, strict=True)))
    return value


class LocalEngine:
    def __init__(self, *, package_root=None, runtime_root=None, role=False, capacity=32):
        state = Path(runtime_root) / "data" / "local-shell" if runtime_root and not role else None
        if state is not None:
            state.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.first, self.end = _reserve_ids(state)
        source = _engine_source(package_root)
        self.directory = tempfile.TemporaryDirectory(prefix="pal-local-engine-", dir=state)
        self.output_dir = Path(self.directory.name)
        self.epoch = uuid4().hex
        self.closed = False
        self.process = None
        self.socket = None
        self.tasks = ()
        self.inflight = set()
        self.awaiting_admission = set()
        try:
            executable = self.output_dir / "pal-shell-local-worker"
            shutil.copyfile(source, executable)
            executable.chmod(0o700)
            self.socket, child = socket.socketpair()
            with child:
                self.process = subprocess.Popen(
                    [str(executable), str(child.fileno()), self.epoch, str(capacity),
                     str(self.first), str(self.end), self.directory.name, "role" if role else "resident"],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, pass_fds=(child.fileno(),),
                    start_new_session=True,
                )
            self.socket.settimeout(5)
            self.socket.sendall(local_wire.request("Hello", 1))
            packet = _sync_read(self.socket, _header(_sync_read(self.socket, 4)))
            if packet[:8] != struct.pack("!Q", 1):
                raise ValueError("unexpected local engine handshake identity")
            hello = local_wire.response("Hello", packet[8:])
            if hello != {"pid": self.process.pid, "epoch": self.epoch.encode(), "version": VERSION.encode()}:
                raise RuntimeError("local engine handshake did not match the installed package")
            self.socket.setblocking(False)
        except BaseException:
            self.close()
            raise

    def connect(self, event, failed):
        self.event, self.failed = event, failed
        self.outbox = asyncio.Queue(_PENDING_LIMIT * 2)
        self.tasks = (asyncio.create_task(self._read()), asyncio.create_task(self._write()))

    def submit(self, operation, number, args, *, approval=None):
        if self.closed or self.process.poll() is not None:
            raise RuntimeError("local engine is unavailable")
        if max(len(self.inflight), len(self.awaiting_admission)) >= _PENDING_LIMIT and operation != "cancel_request":
            raise RuntimeError("local engine request capacity exceeded")
        command = _command(number, operation, args, approval, self.epoch)
        self.outbox.put_nowait(local_wire.request("Submit", number + 1, command))
        self.inflight.add(number)
        self.awaiting_admission.add(number)

    async def _read(self):
        loop = asyncio.get_running_loop()

        async def read(size):
            buffer = bytearray()
            while len(buffer) < size:
                chunk = await loop.sock_recv(self.socket, size - len(buffer))
                if not chunk:
                    raise EOFError("local engine connection closed")
                buffer.extend(chunk)
            return bytes(buffer)

        try:
            while not self.closed:
                packet = await read(_header(await read(4)))
                identifier, = struct.unpack_from("!Q", packet)
                if identifier:
                    number = identifier - 1
                    error = local_wire.response("Submit", packet[8:])
                    if not local_wire.accepts_frame(self.closed, identifier, 0, 0,
                                                    self.first, self.end, number in self.awaiting_admission):
                        raise ValueError("unexpected local engine admission")
                    self.awaiting_admission.remove(number)
                    if error:
                        self.inflight.remove(number)
                        self.event({"request_id": number, "session_id": 0, "status": "rejected",
                                    "error": error.decode(errors="replace")})
                    continue
                raw = local_wire.decode("Event", packet[8:])
                number = raw.pop("request")
                if not local_wire.accepts_frame(self.closed, number, raw["session"], raw["output"],
                                                self.first, self.end, number in self.inflight):
                    raise ValueError("event does not belong to the active local engine")
                if number:
                    self.inflight.remove(number)
                raw["session_id"] = raw.pop("session")
                raw["output_id"] = raw.pop("output")
                event = {**raw, "request_id": number}
                for key in ("event_kind", "status", "error"):
                    event[key] = event[key].decode("utf-8", errors="replace")
                for key in ("stdout_path", "stderr_path"):
                    event[key] = os.fsdecode(event[key])
                    if event[key] and not Path(event[key]).resolve().is_relative_to(self.output_dir.resolve()):
                        raise ValueError("local engine output escapes its private directory")
                for field, present in (("remaining_ms", "has_deadline"), ("wake_remaining_ms", "has_wake"),
                                       ("returncode", "has_returncode")):
                    if not event[present]:
                        event[field] = None
                self.event(event)
        except (OSError, EOFError, ValueError, struct.error, KeyError) as error:
            self._lost(error)

    async def _write(self):
        loop = asyncio.get_running_loop()
        try:
            while not self.closed:
                await loop.sock_sendall(self.socket, await self.outbox.get())
        except OSError as error:
            self._lost(error)

    def _lost(self, error):
        if not self.closed:
            self._disconnect()
            self.inflight.clear()
            self.awaiting_admission.clear()
            self.failed(error)

    def _disconnect(self):
        self.closed = True
        if self.socket is not None:
            with suppress(OSError):
                self.socket.shutdown(socket.SHUT_RDWR)
            self.socket.close()
        for task in self.tasks:
            if not task.get_loop().is_closed():
                task.get_loop().call_soon_threadsafe(task.cancel)

    def close(self):
        self._disconnect()
        if self.process is not None:
            # EOF closes admission and reaps the engine's command groups.
            self.process.wait(timeout=10)
        self.directory.cleanup()
