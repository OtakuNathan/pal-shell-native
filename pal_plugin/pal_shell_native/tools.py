"""Model-facing contracts for native shell execution."""
from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from pal.execution.tool_facade import StrictToolModel, ToolAffordance

from .adapter import TERMINAL


from .guidance import RUN_GUIDANCE, SESSION_GUIDANCE, REMOTE_RUN_GUIDANCE, RESIDENT_SESSION_GUIDANCE


class RunInput(StrictToolModel):
    cmd: str = Field(min_length=1)
    cwd: str = Field(default="", description="Working directory on the execution target; ~ expands to that account’s home.")
    tty: bool = False
    wait_ms: int | None = Field(default=None, ge=0, le=300000)
    timeout_ms: int | None = Field(default=None, ge=1, le=2147483647)


class SessionInput(StrictToolModel):
    session_id: int = Field(gt=0, le=9223372036854775807)
    action: Literal["read", "write", "resize", "terminate", "release", "watch", "extend", "unwatch"] = "read"
    wait_ms: int | None = Field(default=None, ge=0, le=300000)
    extend_by_ms: int | None = Field(default=None, ge=0, le=2147483647)
    text: str | None = None
    rows: int | None = Field(default=None, ge=1, le=65535)
    columns: int | None = Field(default=None, ge=1, le=65535)

    @model_validator(mode="after")
    def validate_action_arguments(self):
        required = {"write": {"text"}, "resize": {"rows", "columns"}, "watch": {"wait_ms"}, "extend": {"extend_by_ms"}}.get(self.action, set())
        allowed = required | ({"wait_ms"} if self.action == "read" else set()) | ({"extend_by_ms"} if self.action == "watch" else set())
        supplied = {name for name in ("wait_ms", "extend_by_ms", "text", "rows", "columns") if getattr(self, name) is not None}
        if not required <= supplied or not supplied <= allowed:
            raise ValueError(f"{self.action} requires {sorted(required)} and only accepts {sorted(allowed)}")
        if self.action == "watch" and not self.wait_ms:
            raise ValueError("watch requires a positive wait_ms")
        if self.action == "extend" and not self.extend_by_ms:
            raise ValueError("extend requires a positive extend_by_ms")
        if self.text is not None and len(self.text.encode("utf-8")) > 65536:
            raise ValueError("PTY input must fit the 64 KiB input queue")
        return self


def session_affordances(result: dict) -> list[ToolAffordance]:
    sid = result.get("session_id", 0)
    status = result.get("status")
    if not sid or status in TERMINAL or status in {"released", "notification_retry_queued"}:
        return []
    actions = ["read (current status and output)"]
    if status != "terminating":
        if not result.get("has_wake", False):
            actions.append("watch (one timed decision notification)")
        if result.get("watching", True):
            actions.append("unwatch (stop notifications without stopping execution)")
        if result.get("remaining_ms") is not None and result["remaining_ms"] > 0:
            actions.append("extend (extend the finite execution budget)")
        if result.get("tty"):
            actions.extend(("write (PTY input)", "resize (PTY dimensions)"))
        actions.append("terminate (request cancellation)")
    return [ToolAffordance(
        tool="read_tool", arguments={"name": "shell_session"},
        reason="shell_session supports: " + "; ".join(actions) + ". "
               "The contract is available here if unknown; known actions are callable via call_tool.",
    )]
