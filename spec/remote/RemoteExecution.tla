-------------------------- MODULE RemoteExecution --------------------------
EXTENDS RemoteOperation
CONSTANTS Scenario, MaxEpoch, UnsafeRestore, UnsafeApproval, UnsafeEventControl
VARIABLE s
vars == <<s>>
Ops == IF Scenario = "pair" THEN {"run", "control"}
       ELSE IF Scenario = "parallel" THEN {"run", "other"} ELSE {"run"}
Kind(o) == CASE o = "control" -> "control"
            [] Scenario = "privileged" -> "privileged"
            [] Scenario = "shutdown" -> "shutdown"
            [] OTHER -> "execution"
Sid(o) == IF o = "other" THEN 2 ELSE 1
Priv(o) == Kind(o) \in {"privileged", "shutdown"}
SameEpoch(t, o) == t.epoch[o] # 0 /\ t.epoch[o] = t.connected
Init == s = [
    connected |-> 0,
    phase |-> [o \in Ops |-> "absent"],
    epoch |-> [o \in Ops |-> 0],
    sent |-> [o \in Ops |-> FALSE],
    started |-> [o \in Ops |-> FALSE],
    done |-> [o \in Ops |-> FALSE],
    rejected |-> [o \in Ops |-> FALSE],
    runs |-> [o \in Ops |-> 0],
    proof |-> [o \in Ops |-> 0],
    authorized |-> [o \in Ops |-> ~Priv(o)],
    preparedOnce |-> [o \in Ops |-> FALSE],
    acked |-> [o \in Ops |-> FALSE],
    settled |-> [o \in Ops |-> FALSE],
    retired |-> [o \in Ops |-> FALSE],
    saved |-> [o \in Ops |-> "absent"]]

Apply(t, o, e) ==
    LET p == IF UnsafeRestore /\ t.phase[o] = "retired" /\ IsRestore(e)
             THEN Restored(e)
             ELSE IF UnsafeApproval /\ e = "approval" THEN "prepared"
             ELSE Reduce(Kind(o), TRUE, t.phase[o], e)
    IN [t EXCEPT !.phase[o] = p, !.retired[o] = @ \/ p = "retired"]
RestoreEvent(p) == CASE p = "running" -> "restore_running"
                    [] p = "delivery" -> "restore_delivery"
                    [] p \in Unsigned -> "restore_unsigned"
                    [] OTHER -> "restore_unknown"
Adopt(t, o, e) == IF SameEpoch(t, o) THEN Apply(t, o, e) ELSE t

Claim(o) == /\ s.phase[o] = "absent"
            /\ s' = Apply(s, o, IF Priv(o) THEN "prepare" ELSE "claim")
Connect == /\ s.connected < MaxEpoch
           /\ s' = [s EXCEPT
                !.connected = @ + 1,
                !.phase = [o \in Ops |-> IF s.epoch[o] = 0 \/ s.phase[o] \in {"absent", "retired"}
                    THEN s.phase[o] ELSE Reduce(Kind(o), TRUE, s.phase[o], "epoch_changed")],
                !.sent = [o \in Ops |-> FALSE],
                !.started = [o \in Ops |-> FALSE],
                !.done = [o \in Ops |-> FALSE],
                !.rejected = [o \in Ops |-> FALSE]]
Bind(o) == /\ s.connected > 0 /\ s.epoch[o] = 0
           /\ s.phase[o] \notin {"absent", "retired"}
           /\ s' = [s EXCEPT !.epoch[o] = s.connected]
PrepareReply(o) == /\ Priv(o) /\ SameEpoch(s, o) /\ s.phase[o] \in Unsigned
                  /\ s' = [Apply(s, o, "approval") EXCEPT !.preparedOnce[o] = TRUE]
Sign(o) == /\ s.phase[o] \in {"prepared", "unsigned_unknown"}
           /\ SameEpoch(s, o)
           /\ s' = [Apply(s, o, "sign") EXCEPT !.authorized[o] = TRUE]
Send(o) == /\ SameEpoch(s, o) /\ ~s.sent[o]
           /\ s.phase[o] \in Unconfirmed
           /\ s.authorized[o]
           /\ s' = [s EXCEPT !.sent[o] = TRUE]
Start(o) == /\ SameEpoch(s, o) /\ s.sent[o] /\ ~s.started[o] /\ ~s.rejected[o]
            /\ s' = [s EXCEPT !.started[o] = TRUE, !.runs[o] = @ + 1]
Finish(o) == /\ s.started[o] /\ ~s.done[o]
             /\ s' = [s EXCEPT !.done[o] = TRUE]
Pending(o) == /\ s.sent[o] /\ ~s.rejected[o]
              /\ s' = Adopt(s, o, "pending")
Running(o) == /\ s.started[o] /\ Kind(o) \in {"execution", "privileged"}
              /\ s' = [Adopt(s, o, "running") EXCEPT
                  !.proof[o] = IF SameEpoch(s, o) /\ @ = 0 THEN 1 ELSE @]
Done(o) == /\ s.done[o] /\ SameEpoch(s, o)
           /\ s' = [Apply(s, o, CASE Kind(o) = "control" -> "control_complete"
                               [] Kind(o) = "shutdown" -> "accepted" [] OTHER -> "terminal")
                    EXCEPT !.proof[o] = 2,
                           !.settled[o] = Kind(o) \in {"control", "shutdown"}]
Event(source, target) ==
    /\ s.done[source] /\ Kind(source) \in {"execution", "privileged"}
    /\ LET relation == IF source = target THEN "exact"
                        ELSE IF Sid(source) = Sid(target) THEN "session" ELSE "foreign"
           permit == Permit(Kind(target), s.phase[target], relation,
                            SameEpoch(s, source) /\ s.epoch[source] = s.epoch[target])
                     \/ (UnsafeEventControl /\ relation = "session")
       IN s' = IF permit
               THEN [Apply(s, target, IF Kind(target) = "control" THEN "control_complete" ELSE "terminal")
                    EXCEPT !.proof[target] = 2]
               ELSE s
Reject(o) == /\ SameEpoch(s, o) /\ s.sent[o] /\ ~s.started[o] /\ ~s.rejected[o]
             /\ s' = [Apply(s, o, "not_started") EXCEPT !.rejected[o] = TRUE]
Lose(o) == /\ s.sent[o] /\ s.phase[o] # "retired"
           /\ s' = Apply(s, o, "lost")
QueryFailure(o) == /\ s.phase[o] \notin {"absent", "retired"}
                   /\ s' = Apply(s, o, "query_failed")
OldApproval(o) == /\ s.preparedOnce[o] /\ SameEpoch(s, o)
                  /\ s' = Apply(s, o, "approval")
Save(o) == /\ s.phase[o] \notin {"absent", "retired"}
           /\ s' = [s EXCEPT !.saved[o] = s.phase[o]]
Restore(o) == /\ s.saved[o] # "absent"
              /\ s' = Apply(s, o, RestoreEvent(s.saved[o]))
CommitOutput(o) == /\ s.phase[o] = "delivery" /\ s.proof[o] = 2
                  /\ SameEpoch(s, o)
                  /\ s' = [Apply(s, o, "release") EXCEPT !.acked[o] = TRUE]
CancelUnsigned(o) == /\ s.phase[o] \in Unsigned
                     /\ s' = Apply(s, o, "cancel_unsigned")
Next == Connect \/ (\E o \in Ops: Claim(o) \/ Bind(o) \/ PrepareReply(o) \/ Sign(o)
        \/ Send(o) \/ Start(o) \/ Finish(o) \/ Pending(o) \/ Running(o) \/ Done(o)
        \/ Reject(o) \/ Lose(o) \/ QueryFailure(o) \/ OldApproval(o) \/ Save(o)
        \/ Restore(o) \/ CommitOutput(o) \/ CancelUnsigned(o))
        \/ (\E source, target \in Ops: Event(source, target))
Spec == Init /\ [][Next]_vars
        /\ \A o \in Ops: WF_vars(Finish(o)) /\ WF_vars(Done(o)) /\ WF_vars(CommitOutput(o))

TypeOK == /\ s.phase \in [Ops -> Phases]
          /\ \A o \in Ops: ValidPhase(Kind(o), s.phase[o])
          /\ s.epoch \in [Ops -> 0..MaxEpoch]
          /\ s.connected \in 0..MaxEpoch
          /\ s.runs \in [Ops -> 0..1]
          /\ s.proof \in [Ops -> 0..2]
NoReplay == \A o \in Ops: s.runs[o] <= 1
NoResurrection == \A o \in Ops: s.retired[o] => s.phase[o] = "retired"
NoLostEffect == \A o \in Ops:
    s.runs[o] > 0 /\ s.phase[o] = "retired" =>
        s.acked[o] \/ s.settled[o]
NoSignedRegression == \A o \in Ops:
    s.authorized[o] /\ Priv(o) => s.phase[o] \notin Unsigned
CompletionMonotone == \A o \in Ops:
    s.proof[o] = 2 /\ SameEpoch(s, o) => s.phase[o] \in {"delivery", "retired"}
ControlNeedsOwnReceipt == \A o \in Ops:
    Kind(o) = "control" /\ s.phase[o] = "retired" /\ s.runs[o] > 0 => s.settled[o]
OldEpochUnknown == \A o \in Ops:
    s.epoch[o] # 0 /\ s.epoch[o] # s.connected /\ s.runs[o] > 0 /\ ~s.retired[o] =>
        s.phase[o] = "unknown"
EventualSettlement == \A o \in Ops:
    (s.done[o] /\ SameEpoch(s, o)) ~>
        (s.phase[o] = "retired" \/ ~SameEpoch(s, o))
\* Expected violations establish that the checked state space reaches useful
\* completion and concurrency paths; an over-restrictive model must fail CI.
NoSettlementWitness == \A o \in Ops: ~s.acked[o]
NoConcurrencyWitness == ~(\A o \in Ops: s.runs[o] = 1)
=============================================================================
