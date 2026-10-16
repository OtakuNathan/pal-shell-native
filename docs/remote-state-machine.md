# Remote execution state machine

The normative operation reducer is spec/remote/RemoteOperation.tla.
RemoteExecution.tla composes it with an independent worker/effect ledger,
connection epochs, delayed receipts, completion events, saved restore snapshots
and output acknowledgment. The Python reducer table is exported from TLA;
changes to runtime behavior must update the specification and pass model checking
before regenerating that table.

## Ownership

An operation ID is allocated before submitting its request. It is never reused.
Binding the worker epoch precedes sending a mutation. A response/event is usable
only for the bound worker epoch while that epoch is current. A completion event
identifies the originating execution. Sharing a session does not settle an
uncertain control operation. Only that control's own authoritative outcome does.

## States

| Internal phase | Meaning | Public diagnostic state |
| --- | --- | --- |
| absent | No claim has existed in this hub | absent |
| submitting | An ordinary execution/control request is being submitted | submitting |
| preparing | Unsigned privilege preparation is in progress | submitting |
| prepared | Unsigned preparation is known; no effect is authorized | awaiting_approval |
| signing | The first approved commit may be sent | submitting |
| pending | The worker accepted a signed/ordinary operation | pending |
| unknown | A submitted effect cannot be confirmed | unknown |
| unsigned_unknown | Preparation is uncertain, but no commit was signed | unknown |
| running | The execution and its output identity are confirmed | running |
| delivery | Completion is confirmed; output acknowledgment is outstanding | delivery |
| retired | The operation is settled; retain an ID tombstone | omitted from active work |

The signing transition is irreversible. Old approval responses cannot make a
signed operation eligible for unsigned cancellation. A failed query describes the
query, not the original effect; it cannot prove NOT_STARTED. Independently
confirmed running/completed evidence survives failed or stale receipts.

Retirement is irreversible within the resident hub. Release, authoritative
NOT_STARTED, unsigned cancellation, confirmed control completion, or accepted
shutdown may retire a claim under their respective guards. A stale restore
snapshot cannot recreate a retired claim. A retired ID cannot be submitted again.
Hub replacement restores only records still owned by the resident host.

## Model boundary and validation

The model abstracts byte content and physical child/SSH behavior. Worker acceptance,
effect completion, epoch changes, authoritative rejection, and successful output
acknowledgment are environment actions. Epoch identity and operation deduplication
are assumed to follow the existing authenticated worker protocol. A full Pal
process crash still has no durable native-handle journal.

Safety checks cover no lost effects, no replay, no resurrection, irreversible
signing, monotone completion, independent control evidence and old-epoch fencing.
Liveness is conditional: a completed operation eventually retires if its worker
epoch remains current and receipt/acknowledgment actions are fairly serviced.
Epoch loss permits retaining UNKNOWN; arbitrary permanent network failure does
not imply recovery or authorize forgetting an effect.

The bounded configurations cover two same-session operations (execution and
control), two independent executions, and single ordinary/privileged/shutdown
operations across two epochs. Protocol safety/liveness uses strict output
delivery; the exported reducer and Python conformance test cover both retention
modes. This is bounded model checking, not an unbounded proof of Python or OS I/O.
Counterexample configurations deliberately reintroduce stale-restore, stale-
approval and cross-control bugs. Model checking must reject each mutant.
Two additional witness configurations must reach a signed operation's output
settlement and two concurrently executed operations. These prevent vacuous passes
from accidentally disabling all useful worker actions.
Integration tests additionally cover real RPC scheduling, output transfer,
resident ticket retirement, and late event ownership.

## Implementation correspondence

| Model rule | Runtime owner / adapter | Validation |
| --- | --- | --- |
| Reduce / ValidPhase / External | contracts.remote_lifecycle and ExecutionGate._advance | 1,440 typed kind/phase/event/retention combinations |
| Permit / SameEpoch | ExecutionGate.snapshot / connected / bind | Exact-ID, session-only, foreign epoch and conflicting output tests |
| NoResurrection | ExecutionGate.retired | Replay the stale restore counterexample; reject settled ID reuse |
| NoSignedRegression | ExecutionGate.sign / reconcile_prepared | Replay approval received after lost signed commit |
| CanAdopt | ShellRouter._adopt / _observe_worker_epoch | Late read after release, new command metadata, or fresh target inspection |
| StaleSnapshot | ShellRouter._adopt and ObservationOwner.record | Delayed running receipt after terminal event |
| PollDecision | ShellRouter._poll_target | Foreign event at queue head; unadopted original execution |

The Python transition/guard tables are evaluated and exported by TLC from the
normative operators. Hand-written Python adapters bind identities and dispatch
evidence; they do not contain a second phase reducer. Retired IDs retain only
their epoch in the current hub, without retaining output bytes. They live until
that hub is replaced, so stale in-flight restore calls remain fenced.
The host learns epoch changes from command metadata, runtime-changed errors,
and fresh target inspections. Cached listings cannot advance that frontier;
already retired epochs and older metadata responses cannot move it backward.

Run from the repository root with a pinned local TLC jar:

    python scripts/check_remote_tla.py /path/to/tla2tools.jar --output-dir /tmp/remote-models

After intentional specification changes, add --write-table. The script checks
all safe models and all expected counterexamples before writing the table.
CI never regenerates it: CI fails if the normative export differs from the
committed runtime table, or if a model/mutant/configuration is missing.

Counterexample logs and a hash-stamped summary are retained in the chosen output
directory. The CI artifact contains these synthetic traces. The checked-in
remote-formal-validation.json records the local verification result.
