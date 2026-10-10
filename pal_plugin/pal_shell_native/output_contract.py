"""Shared output projection and pending delivery state; no owner dependency."""
from __future__ import annotations

from dataclasses import dataclass

from pal.execution.contracts import CapabilityResult
from pal.execution.tool_facade import EffectOutcome, EffectReceipt
from pal.shared.result_rendering import render_structured_for_llm
from pal.shared import RuntimeStatus

from .adapter import TERMINAL
from .guidance import NONZERO_EXIT_GUIDANCE, RUNNING_SESSION_GUIDANCE


def result_guidance(result):
    status = result.get("status")
    if status in {"running", "terminating"}:
        return RUNNING_SESSION_GUIDANCE
    if status == "failed" and "returncode" in result and result["returncode"] is None:
        return "The command did not start. Read error and correct the reported launch problem before retrying."
    if status == "failed":
        return ("Shell execution failed; read error and retained stdout/stderr for the cause. "
                "The command may have changed state before the failure; inspect it before repeating work.")
    if status == "timed_out":
        return ("The command exceeded its execution deadline. This is not a successful completion. "
                "Read retained stdout/stderr and inspect any changes before deciding whether to retry.")
    if status == "cancelled":
        return ("The command was cancelled. Cancellation does not undo earlier effects; "
                "inspect retained output and any changes before repeating work.")
    if status == "exited" and result.get("signal"):
        return ("The command terminated on the reported signal. Read retained stdout/stderr and "
                "inspect any changes before deciding whether to retry.")
    if status == "exited" and result.get("returncode") not in (None, 0):
        return NONZERO_EXIT_GUIDANCE
    return ""


def output_result(result):
    # Control receipts, epochs, cursors and journal IDs never enter model text.
    fields = ("session_id", "target", "status", "returncode", "signal", "error",
              "stdout", "stderr", "tty", "watching", "has_deadline", "remaining_ms",
              "has_wake", "wake_remaining_ms", "truncated", "output_error", "observation_error")
    payload = {key: result[key] for key in fields if key in result}
    if result.get("status") not in TERMINAL:
        payload["returncode"] = None
    text = render_structured_for_llm(payload)
    if result.get("_snapshot_text"):
        text += "\n" + result["_snapshot_text"]
    # The native snapshot has returncode=None on terminal failure only when no
    # process was spawned (runtime.cpp: snapshot/has_returncode). A failure after
    # spawn still has a returncode and cannot erase already-applied effects.
    not_started = result.get("status") == "failed" and "returncode" in result and result["returncode"] is None
    return CapabilityResult(status=RuntimeStatus.OK, structured=payload, text=text, llm_text=text,
                            effect_receipt=EffectReceipt(outcome=EffectOutcome.NOT_STARTED if not_started else EffectOutcome.APPLIED),
                            recovery_hint=result_guidance(result),
                            snapshot_refs=tuple(result.get("_output_snapshots", ())))


@dataclass
class PendingOutput:
    result: dict
    turn_id: str
    raw: CapabilityResult | None = None
    prepared: bool = False
    recovery_of: str = ""
    delivered: bool = False
    failure: str = ""
    covers_output: bool = True
