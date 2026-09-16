------------------------- MODULE HostObservation -------------------------
EXTENDS Naturals, FiniteSets, TLC
CONSTANTS Sessions, Horizon, MaxGeneration, MaxRevision
VARIABLES now, phase, deadline, watching, generation, due, pending, pendingGen,
          extended, deliveredWait, deliveredTerminal, nativeDuplicate
nativeVars == <<now, phase, deadline, watching, generation, due, pending, pendingGen,
          extended, deliveredWait, deliveredTerminal, nativeDuplicate>>

VARIABLES revision, prepared, required, coverage, eventCoverage, pendingEvent, claim,
          capturedRevision, capturedBytes, requestInput, frozenInput,
          requestBusy, parked, signaled, acknowledgements, duplicate
hostVars == <<revision, prepared, required, coverage, eventCoverage, pendingEvent, claim,
              capturedRevision, capturedBytes, requestInput, frozenInput,
              requestBusy, parked, signaled, acknowledgements, duplicate>>
VARIABLES loadFailures, failedDeliveries, ackFailures, capturedFailed, failedOutputAdvanced
recoveryVars == <<loadFailures, failedDeliveries, ackFailures, capturedFailed, failedOutputAdvanced>>
vars == <<nativeVars, hostVars, recoveryVars>>
Native == INSTANCE SessionLifecycle WITH duplicate <- nativeDuplicate

Event(s) == ToString(<<s, pending[s], generation[s]>>)
CurrentEvent(s) == /\ watching[s] /\ pending[s] # "none"
               /\ pendingEvent[s] = Event(s)
               /\ Event(s) \notin eventCoverage
               /\ claim = "none"
Eligible(s) == CurrentEvent(s) /\ (prepared >= required[s] \/ Event(s) \in loadFailures)
AnyEligible == \E s \in Sessions: Eligible(s)
Init == /\ Native!Init
        /\ revision = 0 /\ prepared = 0 /\ coverage = 0
        /\ required = [s \in Sessions |-> 0]
        /\ eventCoverage = {} /\ acknowledgements = {}
        /\ pendingEvent = [s \in Sessions |-> "none"]
        /\ claim = "none" /\ capturedRevision = 0 /\ capturedBytes = 0
        /\ requestInput = 0 /\ frozenInput = 0
        /\ requestBusy = FALSE /\ parked = FALSE /\ signaled = FALSE
        /\ duplicate = FALSE
        /\ loadFailures = {} /\ failedDeliveries = {} /\ ackFailures = {}
        /\ capturedFailed = FALSE /\ failedOutputAdvanced = FALSE
NativeStep == /\ (Native!Tick \/ (\E s \in Sessions: Native!Watch(s) \/ Native!Extend(s)
                        \/ Native!Unwatch(s) \/ Native!Wake(s) \/ Native!Stop(s)
                        \/ Native!Expire(s) \/ Native!Finish(s)))
              /\ UNCHANGED <<hostVars, recoveryVars>>
Observe == /\ revision < MaxRevision /\ revision' = revision + 1
          /\ UNCHANGED recoveryVars
           /\ UNCHANGED <<nativeVars, required, prepared, coverage, eventCoverage,
                pendingEvent, claim, capturedRevision, capturedBytes, requestInput,
                frozenInput, requestBusy, parked, signaled, acknowledgements, duplicate>>
Prepare == /\ prepared < revision /\ prepared' = revision
          /\ UNCHANGED recoveryVars
           /\ signaled' = (signaled \/ (parked /\ \E s \in Sessions: CurrentEvent(s) /\ revision >= required[s]))
           /\ UNCHANGED <<nativeVars, required, revision, coverage, eventCoverage,
                pendingEvent, claim, capturedRevision, capturedBytes, requestInput,
                frozenInput, requestBusy, parked, acknowledgements, duplicate>>
Publish(s) == /\ watching[s] /\ pending[s] # "none"
          /\ UNCHANGED recoveryVars
              /\ pendingEvent[s] # Event(s)
              /\ pendingEvent' = [pendingEvent EXCEPT ![s] = Event(s)]
              /\ required' = [required EXCEPT ![s] = revision]
              /\ signaled' = (signaled \/ (parked /\ prepared >= revision /\ Event(s) \notin eventCoverage))
              /\ UNCHANGED <<nativeVars, revision, prepared, coverage, eventCoverage,
                   claim, capturedRevision, capturedBytes, requestInput, frozenInput,
                   requestBusy, parked, acknowledgements, duplicate>>
Capture(s) == /\ Eligible(s) /\ ~requestBusy
          /\ capturedFailed' = (Event(s) \in loadFailures)
          /\ UNCHANGED <<loadFailures, failedDeliveries, ackFailures, failedOutputAdvanced>>
              /\ claim' = Event(s)
              /\ capturedRevision' = revision /\ capturedBytes' = required[s]
              /\ parked' = FALSE /\ signaled' = FALSE
              /\ UNCHANGED <<nativeVars, required, revision, prepared, coverage, eventCoverage,
                   pendingEvent, requestInput, frozenInput, requestBusy, acknowledgements, duplicate>>
Commit == /\ claim # "none"
          /\ failedDeliveries' = IF capturedFailed THEN failedDeliveries \cup {claim} ELSE failedDeliveries
          /\ UNCHANGED <<loadFailures, ackFailures, capturedFailed>>
          /\ \E s \in Sessions: claim = Event(s) /\ Native!Deliver(s)
          /\ coverage' = IF ~capturedFailed /\ capturedBytes > coverage THEN capturedBytes ELSE coverage
          /\ failedOutputAdvanced' = (failedOutputAdvanced \/ (capturedFailed /\ coverage' # coverage))
          /\ duplicate' = (duplicate \/ claim \in eventCoverage)
          /\ eventCoverage' = eventCoverage \cup {claim}
          /\ claim' = "none"
          /\ UNCHANGED <<required, revision, prepared, pendingEvent,
               capturedRevision, capturedBytes, requestInput, frozenInput, requestBusy,
               parked, signaled, acknowledgements>>
Ack(e) == /\ e \in eventCoverage \ failedDeliveries /\ e \notin acknowledgements
          /\ UNCHANGED recoveryVars
          /\ acknowledgements' = acknowledgements \cup {e}
          /\ UNCHANGED <<nativeVars, required, revision, prepared, coverage, eventCoverage,
               pendingEvent, claim, capturedRevision, capturedBytes, requestInput,
               frozenInput, requestBusy, parked, signaled, duplicate>>
Send == /\ ~requestBusy /\ ~parked /\ claim = "none"
          /\ UNCHANGED recoveryVars
        /\ requestBusy' = TRUE /\ requestInput' = revision /\ frozenInput' = revision
        /\ UNCHANGED <<nativeVars, required, revision, prepared, coverage, eventCoverage,
             pendingEvent, claim, capturedRevision, capturedBytes, parked, signaled,
             acknowledgements, duplicate>>
Return == /\ requestBusy /\ requestBusy' = FALSE
          /\ UNCHANGED recoveryVars
          /\ UNCHANGED <<nativeVars, required, revision, prepared, coverage, eventCoverage,
               pendingEvent, claim, capturedRevision, capturedBytes, requestInput,
               frozenInput, parked, signaled, acknowledgements, duplicate>>
Park == /\ ~requestBusy /\ claim = "none" /\ ~parked
          /\ UNCHANGED recoveryVars
        /\ parked' = TRUE /\ signaled' = AnyEligible
        /\ UNCHANGED <<nativeVars, required, revision, prepared, coverage, eventCoverage,
             pendingEvent, claim, capturedRevision, capturedBytes, requestInput,
             frozenInput, requestBusy, acknowledgements, duplicate>>
AbandonClaim == /\ claim # "none"
          /\ UNCHANGED recoveryVars
                /\ ~(\E s \in Sessions: watching[s] /\ pending[s] # "none" /\ claim = Event(s))
                /\ claim' = "none"
                /\ UNCHANGED <<nativeVars, required, revision, prepared, coverage, eventCoverage,
                     pendingEvent, capturedRevision, capturedBytes, requestInput, frozenInput,
                     requestBusy, parked, signaled, acknowledgements, duplicate>>
LoadFailure(s) == /\ CurrentEvent(s) /\ Event(s) \notin loadFailures
          /\ loadFailures' = loadFailures \cup {Event(s)}
          /\ signaled' = (signaled \/ parked)
          /\ UNCHANGED <<nativeVars, revision, prepared, required, coverage, eventCoverage,
               pendingEvent, claim, capturedRevision, capturedBytes, requestInput, frozenInput,
               requestBusy, parked, acknowledgements, duplicate,
               failedDeliveries, ackFailures, capturedFailed, failedOutputAdvanced>>
AckFailure(e) == /\ e \in eventCoverage \ failedDeliveries /\ e \notin acknowledgements
          /\ ackFailures' = ackFailures \cup {e}
          /\ UNCHANGED <<nativeVars, hostVars, loadFailures, failedDeliveries, capturedFailed, failedOutputAdvanced>>
Next == AbandonClaim \/ NativeStep \/ Observe \/ Prepare \/ Commit \/ Send \/ Return \/ Park
        \/ (\E s \in Sessions: Publish(s) \/ Capture(s) \/ LoadFailure(s))
        \/ (\E e \in eventCoverage: Ack(e) \/ AckFailure(e))
Spec == Init /\ [][Next]_vars /\ WF_vars(Return)
        /\ \A s \in Sessions: WF_vars(Publish(s)) /\ WF_vars(Capture(s))
TypeOK == /\ revision \in 0..MaxRevision /\ prepared \in 0..revision
          /\ coverage \in 0..prepared /\ requestBusy \in BOOLEAN
FrozenRequest == requestBusy => requestInput = frozenInput
NoDuplicateDelivery == ~duplicate
CapturedCoverage == coverage <= prepared
AckOnlyCommitted == acknowledgements \subseteq (eventCoverage \ failedDeliveries)
NoFailedOutputCoverage == ~failedOutputAdvanced
CleanupDoesNotWake == [][(\A e \in eventCoverage: AckFailure(e) => UNCHANGED <<requestInput, signaled, parked, eventCoverage, coverage>>)]_vars
NoLostWake == parked /\ AnyEligible => signaled
NativeSafety == Native!TypeOK /\ Native!NoDuplicates /\ Native!BudgetOnce /\ Native!QuietUnwatched
WakeLiveness == (parked /\ AnyEligible) ~> (~parked \/ ~AnyEligible)
=============================================================================
