"""Asyncio adapter for the opt-in native shell backend."""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass
import itertools
import os
from pathlib import Path
import threading

from pal_shell_contracts import RemoteFailure
from .local_engine import LocalEngine

TERMINAL = frozenset({"exited", "cancelled", "timed_out", "failed"})
READ_EFFECTS = frozenset({"none", "local_read", "external_read"})


class ShellRejected(RuntimeError):
    pass


@dataclass(frozen=True)
class Completion:
    session_id: int
    origin_turn: str
    result: dict


class ShellRuntime:
    def __init__(self, *, completed_capacity: int = 32, on_ready=None, engine=None):
        self.loop = asyncio.get_running_loop()
        self.loop_thread = threading.get_ident()
        self.engine = engine or LocalEngine(capacity=completed_capacity)
        self.epoch = self.engine.epoch
        self._sequence = itertools.count(1)
        self._pending: dict[int, tuple[asyncio.Future, str, str]] = {}
        self._owners: dict[int, str] = {}
        self.completion_budgets = {}
        self._foreground: dict[asyncio.Task, str] = {}
        self._completions: dict[int, Completion] = {}
        self._seen: OrderedDict[int, None] = OrderedDict()
        self._consumed: set[int] = set()
        self._closed = False
        self._closing: asyncio.Task | None = None
        self._on_ready = on_ready
        self.delivery_threads: set[int] = set()
        self.engine.connect(self._deliver, self._transport_failed)

    def _transport_failed(self, error):
        for future, _, _ in self._pending.values():
            if not future.done():
                future.set_exception(RemoteFailure("local_engine_lost",
                    f"Local engine disconnected: {error}", effect="unknown"))
        if self._on_ready:
            self._on_ready()

    def _deliver(self, event: dict) -> None:
        self.delivery_threads.add(threading.get_ident())
        assert threading.get_ident() == self.loop_thread
        event = {**event, "runtime_epoch": self.epoch}
        request = event["request_id"]
        session = event["session_id"]
        if request:
            pending = self._pending.get(request)
            if pending is None:
                return
            future, operation, turn = pending
            if operation == "run" and session:
                self._owners[session] = turn
            if not future.done():
                future.set_result(event)
            return
        identity = (session, event.get("event_sequence", 0))
        if identity in self._seen or self._closed:
            return
        self._seen[identity] = None
        if len(self._seen) > 256:
            previous, _ = self._seen.popitem(last=False)
            self._consumed.discard(previous[0])
        origin = self._owners.get(session, "")
        if session in self._consumed:
            return
        self._completions[session] = Completion(session, origin, event)
        if self._on_ready:
            self._on_ready()

    async def _request(self, operation: str, *args, turn: str = "", cleanup: bool = False, approval: str | None = None) -> dict:
        if (self._closed or self._closing is not None) and not cleanup:
            raise ShellRejected("shell runtime is closed")
        request = next(self._sequence)
        future = self.loop.create_future()
        self._pending[request] = future, operation, turn
        try:
            try:
                self.engine.submit(operation, request, args, approval=approval)
            except (ValueError, RuntimeError, asyncio.QueueFull) as exc:
                raise ShellRejected(str(exc)) from exc
            try:
                result = await asyncio.shield(future)
            except asyncio.CancelledError:
                # Cancel the subscription/foreground operation, not an acknowledged background session.
                await self._request("cancel_request", request, cleanup=True)
                result = await asyncio.shield(future)
                if operation == "acquire_write" and result["status"] == "write_acquired":
                    await self._request("release_write", result["session_id"], cleanup=True)
                if operation == "run" and result["session_id"]:
                    session = result["session_id"]
                    await self._discard_cancelled_session(session)
                elif operation == "run" and result.get("output_id"):
                    await self._request("release_output", result["output_id"], cleanup=True)
                raise
            if result["status"] == "rejected":
                raise ShellRejected(result["error"])
            return result
        finally:
            self._pending.pop(request, None)

    async def run(self, cmd: str, *, cwd: str = "", tty: bool = False, stdin: str | None = None,
                  wait_ms: int | None = None, timeout_ms: int | None = None,
                  shell: str = "/bin/bash", turn_id: str = "", retain_output: bool = False, load_output: bool = True,
                  _approval: str | None = None) -> dict:
        if not load_output and not retain_output:
            raise ValueError("file handoff requires retained output")
        self._check_stdin(stdin, tty)
        task = asyncio.current_task()
        self._foreground[task] = turn_id
        result = None
        try:
            result = await self._request(
                "run", shell, cmd, os.path.expanduser(cwd), tty,
                (1000 if tty else 300000) if wait_ms is None else wait_ms,
                0 if timeout_ms is None else timeout_ms, stdin, turn=turn_id, approval=_approval,
            )
            if load_output:
                result = await self.materialize(result)
            if result["session_id"]:
                await self._request("acknowledge", result["session_id"], False)
            elif not retain_output:
                await self.release_output(result)
            return result
        except asyncio.CancelledError:
            # A cancelled handoff has not returned a session to the tool caller,
            # even if its native acknowledgement just won the scheduling race.
            if result is not None and result["session_id"]:
                sid = result["session_id"]
                await self._request("terminate", sid, cleanup=True)
                await self._discard_cancelled_session(sid)
            elif result is not None and result.get("output_id"):
                await self._request("release_output", result["output_id"], cleanup=True)
            raise
        finally:
            self._foreground.pop(task, None)

    @staticmethod
    def _check_stdin(stdin, tty=False):
        from .inputs import validate_stdin
        try:
            validate_stdin(stdin)
        except ValueError as exc:
            raise ShellRejected(f"invalid_stdin: {exc}") from exc
        if stdin is not None:
            if tty:
                raise ShellRejected("stdin_pty_conflict: stdin requires tty=false")

    async def _discard_cancelled_session(self, session_id: int) -> None:
        while True:
            result = await self._request("read", session_id, 5000, cleanup=True)
            if result["status"] in TERMINAL:
                break
        await self._request("release_output", session_id, cleanup=True)
        self._mark_consumed(session_id)

    async def materialize(self, event: dict, *, load_output: bool = True) -> dict:
        """Read the exact file prefix observed by native code, outside the asyncio thread."""
        if not load_output:
            return dict(event)
        def load():
            result = dict(event)
            for stream in ("stdout", "stderr"):
                path = event.get(stream + "_path", "")
                size = event.get(stream + "_total", 0)
                if stream + "_bytes" in event:
                    raw = event[stream + "_bytes"]
                elif path:
                    with Path(path).open("rb") as file:
                        raw = file.read(size)
                    if len(raw) != size:
                        raise OSError("shell output file shorter than its snapshot")
                else:
                    raw = b""
                result[stream + "_bytes"] = raw
                result[stream] = raw.decode("utf-8", errors="replace")
            return result
        return await asyncio.to_thread(load)

    async def release_output(self, result: dict) -> None:
        await self._request("release_output", result["output_id"], cleanup=True)
        if result["session_id"]:
            self._mark_consumed(result["session_id"])

    def forget_output(self, result: dict) -> None:
        """Retire host references; this does not claim backend release succeeded."""
        if result['session_id']:
            self._mark_consumed(result['session_id'])

    async def read(self, session_id: int, *, wait_ms: int = 0) -> dict:
        result = await self.materialize(await self._request("read", session_id, wait_ms))
        if result["status"] in TERMINAL:
            await self._request("acknowledge", session_id, True)
            self._mark_consumed(session_id)
        return result

    async def session_snapshot(self, session_id: int, *, action: str = "read",
                               wait_ms: int | None = None, text: str | None = None,
                               rows: int | None = None, columns: int | None = None,
                               extend_by_ms: int | None = None) -> dict:
        """Retained file handoff for a host that acknowledges only after output delivery.

        The tool contract validates action-specific arguments before dispatch.
        Unlike the convenience read(), a terminal snapshot is not consumed here.
        """
        arguments = {
            "read": (session_id, wait_ms or 0),
            "write": (session_id, text),
            "resize": (session_id, rows, columns),
            "terminate": (session_id,),
            "release": (session_id,),
            "watch": (session_id, wait_ms or 0, extend_by_ms or 0),
            "extend": (session_id, extend_by_ms or 0),
            "unwatch": (session_id,),
        }
        if action not in arguments:
            raise ValueError(f"unknown session action: {action}")
        result = await self._request(action, *arguments[action])
        if action in {"watch", "unwatch"}:
            pending = self._completions.get(session_id)
            if pending and pending.result.get("watch_generation", 0) < result.get("watch_generation", 0):
                self._completions.pop(session_id, None)
        if action == "release":
            self._owners.pop(session_id, None)
            self._mark_consumed(session_id)
        return result

    async def write(self, session_id: int, text: str) -> dict:
        return await self.materialize(await self._request("write", session_id, text))

    async def resize(self, session_id: int, rows: int, columns: int) -> dict:
        return await self.materialize(await self._request("resize", session_id, rows, columns))

    async def terminate(self, session_id: int) -> dict:
        return await self.materialize(await self._request("terminate", session_id))

    async def release(self, session_id: int) -> None:
        await self._request("release", session_id)
        self._owners.pop(session_id, None)
        self._mark_consumed(session_id)

    def _mark_consumed(self, session_id: int) -> None:
        self._owners.pop(session_id, None)
        self._consumed.add(session_id)
        self.completion_budgets.pop(session_id, None)
        self._completions.pop(session_id, None)

    def drain_completions(self) -> list[Completion]:
        events = list(self._completions.values())
        self._completions.clear()
        return events

    async def acknowledge_completion(self, event: Completion) -> None:
        if event.result["status"] in TERMINAL:
            await self.release_output(event.result)

    async def interrupt_turn(self, turn_id: str) -> None:
        tasks = [task for task, owner in self._foreground.items() if owner == turn_id]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    @asynccontextmanager
    async def tool_admission(self, effect_kind: str):
        """Retain legacy lease-token bookkeeping without excluding other calls."""
        if effect_kind in READ_EFFECTS:
            yield
            return
        lease = (await self._request("acquire_write"))["session_id"]
        try:
            yield
        finally:
            await asyncio.shield(self._request("release_write", lease, cleanup=True))

    def close_idle(self):
        """Lifecycle-fenced synchronous teardown, without live requests or processes."""
        if self._foreground or self._pending:
            raise ShellRejected("execution_busy: shell requests are still active")
        self._closed = True
        self.engine.close()
        self._owners.clear()
        self.completion_budgets.clear()
        self._completions.clear()
        self._seen.clear()
        self._consumed.clear()

    async def close(self) -> None:
        if self._closing is None:
            self._closing = asyncio.create_task(self._close_runtime())
        await asyncio.shield(self._closing)
        # call_soon_threadsafe deliveries were queued before worker shutdown finished.
        await asyncio.sleep(0)
        self._owners.clear()
        self.completion_budgets.clear()
        self.drain_completions()

    async def _close_runtime(self) -> None:
        # Finish cancelled foreground handoffs before destroying their output files.
        tasks = list(self._foreground)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._closed = True
        await asyncio.to_thread(self.engine.close)

    async def reset(self) -> ShellRuntime:
        await self.close()
        return type(self)(on_ready=self._on_ready)
