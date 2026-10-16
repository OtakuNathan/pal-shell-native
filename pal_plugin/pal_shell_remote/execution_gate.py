"""Identity/evidence owner for the TLA-defined remote operation reducer."""
from pal_shell_contracts.remote_lifecycle import (
    TERMINAL, next_phase, operation_kind, operation_phase, permitted, store_phase,
)
from pal_shell_worker.protocol import RemoteError


class ExecutionGate:
    def __init__(self, target):
        self.target = target
        self.operations = {}
        self.retired = {}
        self.current_epoch = None

    def _advance(self, operation_id, event):
        item = self.operations.get(operation_id)
        if item is None:
            return "retired" if operation_id in self.retired else "absent"
        phase = next_phase(operation_kind(item), bool(item.get("hold_output")),
                           operation_phase(item), event)
        if phase == "retired":
            self.retired[operation_id] = item.get("epoch")
            self.operations.pop(operation_id)
        else:
            store_phase(item, phase)
        return phase

    def _same_epoch(self, item, epoch):
        return item.get("epoch") == epoch and (
            self.current_epoch is None or self.current_epoch == epoch)

    def connected(self, epoch):
        self.current_epoch = epoch
        for oid, item in list(self.operations.items()):
            if item.get("epoch") is not None and item["epoch"] != epoch:
                self._advance(oid, "epoch_changed")

    def restore(self, records):
        for record in records:
            oid = record["operation_id"]
            if oid in self.retired or oid in self.operations:
                continue  # Restore never supersedes a live owner or a tombstone.
            restored = dict(record)
            kind = operation_kind(restored)
            if restored.get("unsigned"):
                event = ("restore_prepared" if restored["state"] == "awaiting_approval"
                         else "restore_unsigned")
            elif restored["state"] in {"running", "delivery"} and restored.get("output_id"):
                event = "restore_" + restored["state"]
            else:
                event = "restore_unknown"
            restored.update(kind=kind, state="absent")
            self.operations[oid] = restored
            self._advance(oid, event)
            if self.current_epoch is not None and restored.get("epoch") != self.current_epoch:
                self._advance(oid, "epoch_changed")

    def claim(self, operation_id, epoch, hold_output=False, *, kind="execution"):
        if operation_id in self.retired:
            raise RemoteError("operation_retired", "Operation is already settled; never resubmit its ID",
                              effect="unknown", operation_id=operation_id)
        if operation_id in self.operations:
            item = self.operations[operation_id]
            if item.get("epoch") != epoch:
                raise RemoteError("runtime_changed", "Operation belongs to another Runtime", effect="unknown")
            if operation_kind(item) != kind:
                raise RemoteError("operation_conflict", "Operation ID belongs to another request kind", effect="unknown")
            return False
        self.operations[operation_id] = dict(operation_id=operation_id, epoch=epoch,
            kind=kind, state="absent", session_id=0, output_id="",
            hold_output=hold_output, control=kind == "control")
        self._advance(operation_id, "prepare" if kind in {"privileged", "shutdown"} else "claim")
        return True

    def claim_control(self, operation_id, epoch, session_id):
        created = self.claim(operation_id, epoch, kind="control")
        item = self.operations[operation_id]
        if created:
            item["session_id"] = session_id
        elif item["session_id"] != session_id:
            raise RemoteError("operation_conflict", "Operation ID belongs to a different session", effect="unknown")
        return created

    def bind(self, operation_id, epoch):
        item = self.operations.get(operation_id)
        if item is None:
            return
        if item.get("epoch") not in (None, epoch) or self.current_epoch not in (None, epoch):
            raise RemoteError("runtime_changed", "Operation belongs to another Runtime", effect="unknown")
        item["epoch"] = epoch

    def sign(self, operation_id, epoch):
        item = self.operations.get(operation_id)
        if item is None or operation_kind(item) not in {"privileged", "shutdown"}:
            raise RemoteError("invalid_operation", "No owned privilege preparation for this operation")
        self.bind(operation_id, epoch)
        before = operation_phase(item)
        after = self._advance(operation_id, "sign")
        if before == "preparing":
            raise RemoteError("approval_required", "Privilege preparation has not been confirmed")
        return after == "signing" and before != "signing"

    def failed(self, operation_id, effect, epoch=None):
        item = self.operations.get(operation_id)
        if item is None:
            return
        epoch = epoch or item.get("epoch")
        if item.get("epoch") is None:
            self.bind(operation_id, epoch)
        if self._same_epoch(item, epoch):
            self._advance(operation_id, "not_started" if effect == "not_started" else "lost")

    def cancel_prepared(self, operation_id):
        self._advance(operation_id, "cancel_unsigned")

    def query_failed(self, operation_id, epoch):
        item = self.operations.get(operation_id)
        if item and self._same_epoch(item, epoch):
            self._advance(operation_id, "query_failed")

    def reconcile_prepared(self, operation_id, epoch):
        item = self.operations.get(operation_id)
        if item and self._same_epoch(item, epoch):
            self._advance(operation_id, "cancel_unsigned")

    def result(self, operation_id, response, epoch):
        item = self.operations.get(operation_id)
        if item is None:
            return
        result = response.get("result")
        if result and result.get("runtime_epoch", epoch) != epoch:
            return
        if item.get("epoch") is None:
            self.bind(operation_id, epoch)
        if not self._same_epoch(item, epoch):
            return
        if response.get("error"):
            self._advance(operation_id,
                          "not_started" if response["error"].get("effect") == "not_started" else "lost")
        elif response.get("state") == "approval_required":
            self._advance(operation_id, "approval")
        elif response.get("state") == "pending":
            self._advance(operation_id, "pending")
        elif result and result.get("status"):
            control = operation_kind(item) == "control"
            self.snapshot(result, epoch, None if control else operation_id)
            if control and response.get("state") == "complete":
                self._advance(operation_id, "control_complete")
        else:
            self._advance(operation_id, "missing")

    def snapshot(self, result, epoch, operation_id=None):
        if result.get("runtime_epoch", epoch) != epoch:
            return
        operation_id = operation_id or result.get("execution_id")
        sid, output = result.get("session_id", 0), result.get("output_id", "")
        status = result.get("status")
        event = ("accepted" if status == "accepted" else
                 "terminal" if status in TERMINAL and output else
                 "terminal_empty" if status in TERMINAL else
                 "running" if status in {"running", "terminating"} and output else "missing")
        for oid, item in list(self.operations.items()):
            relation = ("exact" if oid == operation_id else
                        "session" if sid and item.get("session_id") == sid else "foreign")
            kind, before = operation_kind(item), operation_phase(item)
            if not permitted(kind, before, relation, self._same_epoch(item, epoch)):
                continue
            if item.get("output_id") and output and item["output_id"] != output:
                continue
            if item.get("session_id") and sid and item["session_id"] != sid:
                continue
            after = next_phase(kind, bool(item.get("hold_output")), before, event)
            if ((event == "running" and after == "running") or
                (event == "terminal" and after in {"delivery", "retired"}) or
                (event == "accepted" and after == "retired")):
                item.update(session_id=item.get("session_id") or sid, output_id=output)
            self._advance(oid, event)

    def release(self, output_id, epoch):
        for oid, item in list(self.operations.items()):
            if output_id and item.get("output_id") == output_id and self._same_epoch(item, epoch):
                self._advance(oid, "release")

    def status(self):
        return {"blocked": False, "has_work": bool(self.operations),
                "reasons": sorted({v["state"] for v in self.operations.values()})}
