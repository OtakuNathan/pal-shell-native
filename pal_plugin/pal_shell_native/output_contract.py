"""Shared output projection and pending delivery state; no owner dependency."""
from __future__ import annotations

from dataclasses import dataclass

from pal.execution.contracts import CapabilityResult
from pal.execution.tool_facade import EffectOutcome, EffectReceipt
from pal.shared.result_rendering import render_structured_for_llm
from pal.shared import RuntimeStatus

from .adapter import TERMINAL


def output_result(result):
    # Control receipts, epochs, cursors and journal IDs never enter model text.
    fields = ("session_id", "target", "status", "returncode", "signal", "error",
              "stdout", "stderr", "tty", "watching", "has_deadline", "remaining_ms",
              "has_wake", "wake_remaining_ms", "truncated", "output_error")
    payload = {key: result[key] for key in fields if key in result}
    if result.get("status") not in TERMINAL:
        payload["returncode"] = None
    text = render_structured_for_llm(payload)
    if result.get("_snapshot_text"):
        text += "\n" + result["_snapshot_text"]
    return CapabilityResult(status=RuntimeStatus.OK, structured=payload, text=text, llm_text=text,
                            effect_receipt=EffectReceipt(outcome=EffectOutcome.APPLIED),
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
