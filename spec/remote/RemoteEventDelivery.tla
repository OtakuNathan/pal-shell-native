------------------------ MODULE RemoteEventDelivery ------------------------
EXTENDS RemoteOperation
CONSTANTS UnsafeResurrection, UnsafeRegression, UnsafeHeadBlock
VARIABLE s
vars == <<s>>
Init == s = [owned |-> TRUE, consumed |-> FALSE, epoch |-> 1,
             adopted |-> FALSE, version |-> 0, terminalSeen |-> FALSE,
             cursor |-> 0, notified |-> FALSE]
Receive(v) ==
    /\ v \in 1..2
    /\ LET allowed == CanAdopt(s.owned, s.consumed, s.epoch = 1)
                       \/ (UnsafeResurrection /\ s.consumed)
           stale == StaleSnapshot(FALSE, v < s.version, s.version = 2 /\ v < 2, v < s.version)
       IN s' = IF allowed /\ (UnsafeRegression \/ ~stale)
               THEN [s EXCEPT !.adopted = TRUE, !.version = v,
                             !.terminalSeen = @ \/ v = 2]
               ELSE s
Retire == /\ s.adopted /\ s.version = 2 /\ ~s.consumed
          /\ s' = [s EXCEPT !.consumed = TRUE, !.owned = FALSE, !.adopted = FALSE]
Restart == /\ s.epoch = 1 /\ s' = [s EXCEPT !.epoch = 2, !.adopted = FALSE]
\* Queue item 0 is foreign, item 1 belongs to the original execution.
Poll == /\ s.cursor < 2 /\ s.epoch = 1
        /\ LET owned == s.cursor = 1 /\ s.owned /\ ~s.consumed
               choice == IF UnsafeHeadBlock /\ ~owned THEN "defer" ELSE PollDecision(owned, TRUE)
           IN s' = CASE choice = "skip" -> [s EXCEPT !.cursor = @ + 1]
                     [] choice = "adopt" ->
                          [s EXCEPT !.cursor = @ + 1, !.adopted = TRUE, !.version = 2,
                                    !.terminalSeen = TRUE, !.notified = TRUE]
                     [] OTHER -> s
Next == Retire \/ Restart \/ Poll \/ (\E v \in 1..2: Receive(v))
Spec == Init /\ [][Next]_vars /\ WF_vars(Poll)
NoResurrection == s.consumed => ~s.adopted
NoRegression == s.terminalSeen => s.version = 2
EpochFence == s.epoch = 2 => ~s.adopted
NoHeadStarvation == (s.owned /\ s.epoch = 1) ~>
    (s.notified \/ s.consumed \/ s.epoch # 1)
=============================================================================
