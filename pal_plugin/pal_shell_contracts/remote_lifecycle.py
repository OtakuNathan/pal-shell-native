"""Adapters for the normative TLA-exported remote lifecycle.

Only these helpers interpret the generated table. Runtime owners still own
identity, I/O, and evidence storage; they cannot invent another phase transition.
"""
from __future__ import annotations

from ._remote_transitions import (
    ADOPT, EVENTS, KINDS, PERMITTED, PHASES, POLL, PUBLIC_PHASE, STALE,
    TRANSITIONS, UNSIGNED_PHASES, VALID_PHASES,
)

TERMINAL = frozenset({"exited", "cancelled", "timed_out", "failed"})


def next_phase(kind: str, hold_output: bool, phase: str, event: str) -> str:
    if kind not in KINDS or (kind, phase) not in VALID_PHASES or event not in EVENTS:
        raise ValueError(f"Invalid remote transition: {kind}, {phase}, {event}")
    target = TRANSITIONS.get((kind, bool(hold_output), phase, event), phase)
    if (kind, target) not in VALID_PHASES:
        raise ValueError(f"Invalid remote target phase: {kind}, {target}")
    return target


def permitted(kind: str, phase: str, relation: str, same_epoch: bool) -> bool:
    return (kind, phase, relation, same_epoch) in PERMITTED


def operation_kind(item: dict) -> str:
    return item.get("kind") or ("control" if item.get("control") else
                               "privileged" if "unsigned" in item else "execution")


def operation_phase(item: dict) -> str:
    state = item["state"]
    if state == "submitting" and operation_kind(item) in {"privileged", "shutdown"}:
        return "preparing" if item.get("unsigned") else "signing"
    if state == "awaiting_approval":
        return "prepared" if item.get("unsigned") else "signing"
    if state == "unknown" and item.get("unsigned") and operation_kind(item) in {"privileged", "shutdown"}:
        return "unsigned_unknown"
    return state


def store_phase(item: dict, phase: str) -> None:
    item["state"] = PUBLIC_PHASE[phase]
    if operation_kind(item) in {"privileged", "shutdown"}:
        item["unsigned"] = phase in UNSIGNED_PHASES


def can_adopt(owned: bool, consumed: bool, same_epoch: bool) -> bool:
    return ADOPT[owned, consumed, same_epoch]


def snapshot_is_older(result: dict, previous: dict) -> bool:
    return STALE[
        result.get("watch_generation", 0) < previous.get("watch_generation", 0),
        result.get("event_sequence", 0) < previous.get("event_sequence", 0),
        previous["status"] in TERMINAL and result["status"] not in TERMINAL,
        any(result.get(stream + "_total", 0) < previous.get(stream + "_total", 0)
            for stream in ("stdout", "stderr")),
    ]


def poll_decision(owned: bool, has_identity: bool) -> str:
    return POLL[owned, has_identity]
