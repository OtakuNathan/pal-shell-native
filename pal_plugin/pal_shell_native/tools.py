"""Model-facing contracts for native shell execution."""
from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from pal.execution.tool_facade import StrictToolModel, ToolAffordance


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


def session_affordances(result: dict, *, output_ref: str = "") -> list[ToolAffordance]:
    """Only failed output delivery warrants a recovery action.

    Live state, PTY support, and a new session ID are facts, not reasons to
    replay the session contract. Static controls remain in tool guidance.
    """
    if not result.get("output_error"):
        return []
    sid = result.get("session_id", 0)
    if sid:
        args = {"session_id": sid, "action": "read"}
    elif output_ref:
        args = {"output_ref": output_ref, "action": "read"}
    else:
        return []
    return [ToolAffordance(
        tool="call_tool", arguments={"name": "shell_session", "args": args},
        reason="After resolving output storage/read failure, export the retained result without rerunning the command.",
    )]
