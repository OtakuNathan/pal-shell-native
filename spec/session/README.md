# Native session lifecycle

Run `scripts/check_session_tla.sh /path/to/tla2tools.jar`. The model is independent
of Pal. It treats reactor time as a monotonic discrete clock; a zero deadline means
unlimited execution. It models two sessions, two watch generations, and one uniquely
identified extension per session. A repeated extension operation stutters: the
operation journal must return the original receipt, not apply the delta again.

Actions map to native owner transitions: Watch arms/replaces a one-shot timer,
Extend adds to the existing deadline, Unwatch disables unsolicited delivery without
stopping execution, Wake creates a generation-scoped event, Stop/Expire begin
termination, Finish records exit, and Deliver commits the observation. Stale timer
callbacks must not invoke Wake; callbacks carry the generation captured at arming.
The delivery adapter serializes Unwatch and Deliver and deduplicates committed IDs.
Transport retransmission is not a second Deliver action.

Safety checks cover duplicate observations, exact extension accounting, quiet
unwatched sessions, and obsolete pending waits. Time progresses fairly up to the
bounded horizon; expiration and reaping a terminating process are fair. Natural exit
of an unlimited process is deliberately not assumed. FiniteTerminates assumes that
requested process termination eventually succeeds. This model does not prove OS
signal delivery, L1 persistence, transport correctness, or the implementation itself;
those require the corresponding native and host regressions.

The checked TLC jar is version 2.19 (08 August 2024, revision 5a47802), SHA256
`936a262061c914694dfd669a543be24573c45d5aa0ff20a8b96b23d01e050e88`.
