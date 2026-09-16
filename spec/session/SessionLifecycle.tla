--------------------------- MODULE SessionLifecycle ---------------------------
EXTENDS Naturals, FiniteSets, TLC
CONSTANTS Sessions, Horizon, MaxGeneration
VARIABLES now, phase, deadline, watching, generation, due, pending, pendingGen,
          extended, deliveredWait, deliveredTerminal, duplicate
vars == <<now, phase, deadline, watching, generation, due, pending, pendingGen,
          extended, deliveredWait, deliveredTerminal, duplicate>>
InitialDeadline(s) == IF s = 1 THEN 2 ELSE 0
Init == /\ now = 0
        /\ phase = [s \in Sessions |-> "running"]
        /\ deadline = [s \in Sessions |-> InitialDeadline(s)]
        /\ watching = [s \in Sessions |-> TRUE]
        /\ generation = [s \in Sessions |-> 0]
        /\ due = [s \in Sessions |-> 0]
        /\ pending = [s \in Sessions |-> "none"]
        /\ pendingGen = [s \in Sessions |-> 0]
        /\ extended = {}
        /\ deliveredWait = {}
        /\ deliveredTerminal = {}
        /\ duplicate = FALSE
Tick == /\ now < Horizon /\ now' = now + 1
        /\ UNCHANGED <<phase, deadline, watching, generation, due, pending,
                       pendingGen, extended, deliveredWait, deliveredTerminal, duplicate>>
Watch(s) == /\ phase[s] = "running"
            /\ deadline[s] = 0 \/ now < deadline[s]
            /\ generation[s] < MaxGeneration /\ now < Horizon
            /\ generation' = [generation EXCEPT ![s] = @ + 1]
            /\ due' = [due EXCEPT ![s] = now + 1]
            /\ watching' = [watching EXCEPT ![s] = TRUE]
            /\ pending' = [pending EXCEPT ![s] = "none"]
            /\ UNCHANGED <<now, phase, deadline, pendingGen, extended,
                           deliveredWait, deliveredTerminal, duplicate>>
Extend(s) == /\ phase[s] = "running" /\ deadline[s] > now
             /\ s \notin extended
             /\ deadline' = [deadline EXCEPT ![s] = @ + 1]
             /\ extended' = extended \cup {s}
             /\ UNCHANGED <<now, phase, watching, generation, due, pending,
                            pendingGen, deliveredWait, deliveredTerminal, duplicate>>
Unwatch(s) == /\ watching[s]
              /\ watching' = [watching EXCEPT ![s] = FALSE]
              /\ due' = [due EXCEPT ![s] = 0]
              /\ pending' = [pending EXCEPT ![s] = "none"]
              /\ UNCHANGED <<now, phase, deadline, generation, pendingGen,
                             extended, deliveredWait, deliveredTerminal, duplicate>>
Wake(s) == /\ phase[s] = "running" /\ watching[s] /\ due[s] > 0
           /\ now >= due[s] /\ (deadline[s] = 0 \/ now < deadline[s])
           /\ pending' = [pending EXCEPT ![s] = "wait"]
           /\ pendingGen' = [pendingGen EXCEPT ![s] = generation[s]]
           /\ due' = [due EXCEPT ![s] = 0]
           /\ UNCHANGED <<now, phase, deadline, watching, generation, extended,
                          deliveredWait, deliveredTerminal, duplicate>>
Stop(s) == /\ phase[s] = "running"
           /\ phase' = [phase EXCEPT ![s] = "terminating"]
           /\ due' = [due EXCEPT ![s] = 0]
           /\ pending' = [pending EXCEPT ![s] = "none"]
           /\ UNCHANGED <<now, deadline, watching, generation, pendingGen,
                          extended, deliveredWait, deliveredTerminal, duplicate>>
Expire(s) == /\ deadline[s] > 0 /\ now >= deadline[s] /\ Stop(s)
Finish(s) == /\ phase[s] # "done"
             /\ phase' = [phase EXCEPT ![s] = "done"]
             /\ pending' = [pending EXCEPT ![s] = IF watching[s] THEN "terminal" ELSE "none"]
             /\ due' = [due EXCEPT ![s] = 0]
             /\ UNCHANGED <<now, deadline, watching, generation, pendingGen,
                            extended, deliveredWait, deliveredTerminal, duplicate>>
Reap(s) == phase[s] = "terminating" /\ Finish(s)
Deliver(s) == /\ pending[s] # "none" /\ watching[s]
              /\ pending' = [pending EXCEPT ![s] = "none"]
              /\ deliveredWait' = IF pending[s] = "wait"
                    THEN deliveredWait \cup {<<s, pendingGen[s]>>} ELSE deliveredWait
              /\ deliveredTerminal' = IF pending[s] = "terminal"
                    THEN deliveredTerminal \cup {s} ELSE deliveredTerminal
              /\ duplicate' = duplicate \/ IF pending[s] = "wait"
                    THEN <<s, pendingGen[s]>> \in deliveredWait ELSE s \in deliveredTerminal
              /\ UNCHANGED <<now, phase, deadline, watching, generation, due, pendingGen, extended>>
Next == Tick \/ \E s \in Sessions: Watch(s) \/ Extend(s) \/ Unwatch(s) \/ Wake(s)
                                  \/ Stop(s) \/ Expire(s) \/ Finish(s) \/ Deliver(s)
Spec == Init /\ [][Next]_vars /\ WF_vars(Tick)
        /\ \A s \in Sessions: WF_vars(Expire(s)) /\ WF_vars(Reap(s)) /\ WF_vars(Deliver(s))
TypeOK == /\ now \in 0..Horizon /\ phase \in [Sessions -> {"running", "terminating", "done"}]
          /\ watching \in [Sessions -> BOOLEAN] /\ generation \in [Sessions -> 0..MaxGeneration]
          /\ pending \in [Sessions -> {"none", "wait", "terminal"}]
NoDuplicates == ~duplicate
BudgetOnce == \A s \in Sessions: deadline[s] = InitialDeadline(s) + IF s \in extended THEN 1 ELSE 0
QuietUnwatched == \A s \in Sessions: ~watching[s] => pending[s] = "none" /\ due[s] = 0
NoStaleWait == \A s \in Sessions: pending[s] = "wait" => phase[s] = "running" /\ pendingGen[s] = generation[s]
FiniteTerminates == \A s \in Sessions: InitialDeadline(s) > 0 => <>(phase[s] = "done")
=============================================================================
