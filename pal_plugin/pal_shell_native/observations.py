"""Shared observation semantics for native resident and Bunshin hosts."""
from __future__ import annotations

from typing import Any, Mapping

from .adapter import Completion, TERMINAL

EVENT = "execution.shell.completed"
WAIT_EVENT = "execution.shell.wait_expired"


def event_metadata(completion: Completion) -> dict[str, Any]:
    result = completion.result
    kind = result.get("event_kind", "terminal")
    return {
        "source": WAIT_EVENT if kind == "wait_expired" else EVENT,
        "event_kind": kind,
        "event_id": f"shell:{result.get('runtime_epoch', 'local')}:{completion.session_id}:{result.get('event_sequence', 0)}",
        "origin_turn": completion.origin_turn,
        "session_id": completion.session_id,
    }


def observation_is_current(result: Mapping[str, Any], session: Mapping[str, Any]) -> bool:
    return (session.get("watching", True)
            and result.get("watch_generation", 0) >= session.get("watch_generation", 0)
            and not (session.get("latest_status") in TERMINAL | {"terminating"}
                     and result["status"] not in TERMINAL))


def output_since(loaded: Mapping[str, Any], offsets: Mapping[str, int]) -> dict[str, Any]:
    """Project new bytes without changing the retained full-output snapshot."""
    result = dict(loaded)
    if loaded.get("_output_snapshots"):
        return result
    for stream in ("stdout", "stderr"):
        result[stream] = loaded.get(stream + "_bytes", b"")[offsets.get(stream, 0):].decode("utf-8", errors="replace")
    return result
