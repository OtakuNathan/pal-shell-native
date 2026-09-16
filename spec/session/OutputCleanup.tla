------------------------- MODULE OutputCleanup -------------------------
EXTENDS Naturals
CONSTANT MaxAttempts
VARIABLES delivery, covered, attempts, cleanup, remoteReleased, request, wake
vars == <<delivery, covered, attempts, cleanup, remoteReleased, request, wake>>
Init == /\ delivery = "pending" /\ covered = FALSE /\ attempts = 0
        /\ cleanup = "idle" /\ remoteReleased = FALSE
        /\ request = "idle" /\ wake = FALSE
CommitSuccess == /\ delivery = "pending" /\ delivery' = "success"
                 /\ covered' = TRUE /\ cleanup' = "pending"
                 /\ UNCHANGED <<attempts, remoteReleased, request, wake>>
CommitFailure == /\ delivery = "pending" /\ delivery' = "failed"
                 /\ cleanup' = "pending"
                 /\ UNCHANGED <<covered, attempts, remoteReleased, request, wake>>
Send == /\ delivery # "pending" /\ request = "idle" /\ request' = "busy"
        /\ UNCHANGED <<delivery, covered, attempts, cleanup, remoteReleased, wake>>
Return == /\ request = "busy" /\ request' = "done"
          /\ UNCHANGED <<delivery, covered, attempts, cleanup, remoteReleased, wake>>
Attempt == /\ cleanup = "pending" /\ attempts < MaxAttempts
           /\ attempts' = attempts + 1
           /\ \/ /\ remoteReleased' = TRUE /\ cleanup' = "released"
              \/ /\ remoteReleased' \in BOOLEAN
                 /\ cleanup' = IF attempts' = MaxAttempts THEN "exhausted" ELSE "pending"
              \/ /\ UNCHANGED remoteReleased /\ cleanup' = "exhausted"
           /\ UNCHANGED <<delivery, covered, request, wake>>
Close == /\ cleanup = "pending" /\ cleanup' = "closed"
         /\ UNCHANGED <<delivery, covered, attempts, remoteReleased, request, wake>>
Next == CommitSuccess \/ CommitFailure \/ Send \/ Return \/ Attempt \/ Close
Spec == Init /\ [][Next]_vars /\ WF_vars(Attempt) /\ WF_vars(Send) /\ WF_vars(Return)
TypeOK == /\ attempts \in 0..MaxAttempts /\ covered \in BOOLEAN
          /\ remoteReleased \in BOOLEAN /\ wake = FALSE
OnlyCommittedCleanup == cleanup # "idle" => delivery # "pending"
HonestCoverage == covered <=> delivery = "success"
ConfirmedRelease == cleanup = "released" => remoteReleased
SendIndependent == (delivery # "pending" /\ request = "idle") => ENABLED Send
NoWakeOrRedelivery == [][Attempt => UNCHANGED <<wake, delivery, covered, request>>]_vars
CleanupTerminates == (cleanup = "pending") ~> (cleanup # "pending")
=============================================================================
