-------------------------- MODULE RemoteOperation --------------------------
EXTENDS Naturals, FiniteSets, TLC

\* Normative, total operation reducer. Python's transition table is exported
\* from these operators, never generated in the opposite direction.
Kinds == {"execution", "control", "privileged", "shutdown"}
Phases == {"absent", "submitting", "preparing", "prepared", "signing",
           "pending", "unknown", "unsigned_unknown", "running", "delivery", "retired"}
Unsigned == {"preparing", "prepared", "unsigned_unknown"}
Known == {"running", "delivery"}
Unconfirmed == {"submitting", "signing", "pending", "unknown"}
Events == {"claim", "prepare", "sign", "approval", "pending", "running",
           "terminal", "terminal_empty", "accepted", "control_complete",
           "not_started", "lost", "query_failed", "missing", "release",
           "cancel_unsigned", "epoch_changed", "restore_submitting",
           "restore_unknown", "restore_running", "restore_delivery",
           "restore_prepared", "restore_unsigned", "foreign"}
Relations == {"exact", "session", "foreign"}
ValidPhase(k, p) ==
    p \in {"absent", "pending", "unknown", "retired"}
    \/ (k \in {"execution", "control"} /\ p = "submitting")
    \/ (k \in {"execution", "privileged"} /\ p \in Known)
    \/ (k \in {"privileged", "shutdown"} /\ p \in (Unsigned \cup {"signing"}))

Restored(e) == CASE e = "restore_running" -> "running"
                [] e = "restore_delivery" -> "delivery"
                [] e = "restore_prepared" -> "prepared"
                [] e = "restore_unsigned" -> "unsigned_unknown"
                [] OTHER -> "unknown"
IsRestore(e) == e \in {"restore_submitting", "restore_unknown", "restore_running",
                      "restore_delivery", "restore_prepared", "restore_unsigned"}

Reduce(k, hold, p, e) ==
    CASE p = "retired" -> "retired"
      [] p = "absent" ->
           CASE e = "claim" /\ k \in {"execution", "control"} -> "submitting"
             [] e = "prepare" /\ k \in {"privileged", "shutdown"} -> "preparing"
             [] IsRestore(e) -> IF ValidPhase(k, Restored(e)) THEN Restored(e) ELSE "unknown"
             [] OTHER -> "absent"
      [] IsRestore(e) \/ e = "foreign" -> p
      [] e = "sign" -> IF p \in {"prepared", "unsigned_unknown"} THEN "signing" ELSE p
      [] e = "approval" -> IF p \in Unsigned THEN "prepared" ELSE p
      [] e = "pending" -> IF p \in Unconfirmed THEN "pending" ELSE p
      [] e = "running" ->
           IF k \in {"execution", "privileged"} /\ p \in (Unconfirmed \cup {"running"})
           THEN "running" ELSE p
      [] e = "terminal" ->
           IF k \in {"execution", "privileged"} /\ p \in (Unconfirmed \cup Known)
           THEN IF hold THEN "delivery" ELSE "retired" ELSE p
      [] e = "accepted" ->
           IF k = "shutdown" /\ p \in Unconfirmed THEN "retired" ELSE p
      [] e = "control_complete" -> IF k = "control" THEN "retired" ELSE p
      [] e = "not_started" -> IF p \in Known THEN p ELSE "retired"
      [] e \in {"lost", "missing", "terminal_empty"} ->
           IF p \in Known THEN p ELSE IF p \in Unsigned THEN "unsigned_unknown" ELSE "unknown"
      [] e = "query_failed" -> IF p \in Unconfirmed THEN "unknown" ELSE p
      [] e = "release" -> IF p \in Known THEN "retired" ELSE p
      [] e = "cancel_unsigned" -> IF p \in Unsigned THEN "retired" ELSE p
      [] e = "epoch_changed" -> IF p \in Unsigned THEN "unsigned_unknown" ELSE "unknown"
      [] OTHER -> p

Permit(k, p, relation, sameEpoch) ==
    sameEpoch /\ (relation = "exact" \/
        (relation = "session" /\ k \in {"execution", "privileged"} /\ p \in Known))

External(p) == CASE p \in {"preparing", "signing"} -> "submitting"
                [] p = "prepared" -> "awaiting_approval"
                [] p = "unsigned_unknown" -> "unknown"
                [] OTHER -> p

CanAdopt(owned, consumed, sameEpoch) == owned /\ ~consumed /\ sameEpoch
StaleSnapshot(oldGeneration, oldSequence, terminalRegression, lostBytes) ==
    oldGeneration \/ oldSequence \/ terminalRegression \/ lostBytes
PollDecision(owned, hasIdentity) ==
    IF owned THEN "adopt" ELSE IF hasIdentity THEN "skip" ELSE "defer"
=============================================================================
