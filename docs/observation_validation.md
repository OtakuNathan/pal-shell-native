# Observation/wakeup validation — 2026-09-16

Baseline: Native `88078e837f67b3c6b7773001e2b15ad43669d1ca` and Pal
`ec8c863accc5e77e3447034337f5ebbb895e7d7f`. The paired Pal implementation is
`e50f466963dc0f20a94d8ca9caf4aa5885438c1e`. This is an unreleased 0.4.0 candidate,
not a rollback to the previously published 0.4.0 implementation.

## Native checks

On Linux aarch64, CPython 3.13.5:

- Built a new 0.4.0 wheel from source, installed into an isolated artifact directory.
  Both native modules and the worker imported from that directory during acceptance.
- Independent native/worker tests: 67 passed, 2 skipped, 26 subtests. Skips are the
  Windows-only prototype and opt-in 95-second idle RPC check.
- Pal host acceptance against the final wheel: 122 passed, 19 subtests. This includes
  local and remote execution, Bunshin sandbox activation, independent request/observe
  timing, paging/recovery, attention changes and ACK failures.
- Package compatibility tests cover cold-import verification and rejection of
  hosts missing the observation hooks. A final resident coverage regression suite
  passed 21 tests: an older captured event cannot mark a newer state as delivered.
- Artifact inspection compared packaged owner/plugin source bytes with this checkout.
  The package excludes Python bytecode caches.

Race regressions cover atomic L1 failure/retry, separate state/event/byte coverage,
old and new event ordering, unwatch after resident claim, unwatch during preparation,
ACK overlap, no duplicate model request after ACK failure, new-turn state reuse, and
50 unchanged rounds without context growth. Worker observation performs 50 reads
with an operation-journal limit of one without consuming additional entries. A real
remote output download resumes at its validated byte offset after a lost RPC.

The sandbox test initially failed with an interpreter located under `/tmp`, outside
its executable mount. Re-running with the machine's `/usr/bin/python3` passed; no
sandbox restriction was weakened. Deprecation/compiler warnings remain visible in
the logs. No macOS, Windows or disposable privileged-container run is claimed here.

## TLA+

`scripts/check_session_tla.sh` runs both models with the pinned TLC 1.7.4 jar
(SHA-256 `936a262061c914694dfd669a543be24573c45d5aa0ff20a8b96b23d01e050e88`).

- Native lifecycle: 253,635 generated / 60,824 distinct states, no errors.
- Host composition: 163,035 generated / 44,788 distinct states, no errors;
  configured safety invariants and wake liveness passed.

The bounds and fairness assumptions are documented in `shell_session_lifecycle.md`.
The model is an abstraction checked alongside implementation regressions, not a
proof of all unbounded executions, network availability or upstream cache billing.

## Activation

The working service, runtime configuration, earlier binaries and original main
checkouts were not replaced. Pal core requires separate process activation; the
matching plugin and wheel are separate installable artifacts. No remote worker,
tag or public release was updated. Publish the paired Pal commit before any future
Native CI run that fetches the pinned host revision. Runtime activation should use
new versioned artifact directories and wait for current work to become idle.
