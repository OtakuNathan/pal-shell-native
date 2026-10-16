"""Model-derived transition conformance plus executable counterexample replays."""
import hashlib
from pathlib import Path
import unittest

from pal_shell_contracts._remote_transitions import (
    EVENTS, SPEC_SHA256, TRANSITIONS, VALID_PHASES,
)
from pal_shell_contracts.remote_lifecycle import (
    next_phase, operation_phase, store_phase,
)
from pal_shell_remote.execution_gate import ExecutionGate
from pal_shell_worker.protocol import RemoteError

ROOT = Path(__file__).resolve().parent.parent


class FormalConformanceTests(unittest.TestCase):
    def test_runtime_table_is_tied_to_the_normative_spec(self):
        self.assertEqual(hashlib.sha256(
            (ROOT / "spec/remote/RemoteOperation.tla").read_bytes()).hexdigest(), SPEC_SHA256)

    def test_every_typed_transition_is_executed_by_the_gate(self):
        checked = 0
        for kind, phase in sorted(VALID_PHASES):
            for hold in (False, True):
                for event in sorted(EVENTS):
                    with self.subTest(kind=kind, phase=phase, hold=hold, event=event):
                        gate = ExecutionGate(1)
                        if phase == "retired":
                            gate.retired["op"] = "epoch"
                        else:
                            item = dict(operation_id="op", kind=kind, epoch="epoch",
                                        hold_output=hold, state="absent")
                            store_phase(item, phase)
                            gate.operations["op"] = item
                        expected = TRANSITIONS.get((kind, hold, phase, event), phase)
                        actual = gate._advance("op", event)
                        self.assertEqual(actual, expected)
                        self.assertEqual(next_phase(kind, hold, phase, event), expected)
                        if actual not in {"retired", "absent"}:
                            self.assertEqual(operation_phase(gate.operations["op"]), expected)
                        checked += 1
        self.assertGreater(checked, 1000)

    def test_stale_restore_after_retirement_cannot_recreate_a_claim(self):
        gate = ExecutionGate(1)
        gate.claim("op", "epoch", hold_output=True)
        saved = dict(gate.operations["op"])
        gate.failed("op", "not_started", "epoch")
        gate.restore([saved])
        self.assertFalse(gate.operations)
        self.assertIn("op", gate.retired)
        with self.assertRaises(RemoteError):
            gate.claim("op", "epoch")
        with self.assertRaises(RemoteError):
            gate.claim("op", "new-epoch")

    def test_delayed_approval_after_lost_commit_cannot_cancel_the_effect(self):
        gate = ExecutionGate(1)
        gate.claim("op", "epoch", hold_output=True, kind="privileged")
        gate.result("op", {"state": "approval_required"}, "epoch")
        gate.sign("op", "epoch")
        gate.failed("op", "unknown", "epoch")
        gate.result("op", {"state": "approval_required"}, "epoch")
        gate.reconcile_prepared("op", "epoch")
        gate.cancel_prepared("op")
        self.assertEqual(operation_phase(gate.operations["op"]), "unknown")
        self.assertFalse(gate.operations["op"]["unsigned"])

    def test_old_epoch_receipt_cannot_reconfirm_a_restarted_worker(self):
        gate = ExecutionGate(1)
        gate.connected("old")
        gate.claim("op", "old", hold_output=True)
        gate.connected("new")
        gate.result("op", {"state": "complete", "result": {
            "session_id": 1, "output_id": "out", "status": "running", "runtime_epoch": "old"}}, "old")
        self.assertEqual(gate.operations["op"]["state"], "unknown")
        gate.release("out", "old")
        self.assertIn("op", gate.operations)

    def test_conflicting_output_identity_cannot_replace_retained_output(self):
        gate = ExecutionGate(1)
        gate.claim("op", "epoch", hold_output=True)
        gate.snapshot({"execution_id": "op", "session_id": 1, "output_id": "out",
                       "status": "running"}, "epoch")
        gate.snapshot({"execution_id": "op", "session_id": 2, "output_id": "other",
                       "status": "exited"}, "epoch")
        self.assertEqual(gate.operations["op"]["output_id"], "out")
        self.assertEqual(gate.operations["op"]["session_id"], 1)


if __name__ == "__main__":
    unittest.main()
