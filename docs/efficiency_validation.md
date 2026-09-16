# Execution boundary efficiency validation

Unreleased 0.4.0 candidate, validated on Linux aarch64 with CPython 3.13.5.
Paired Pal: `40e8bb25a818891409bc546c1e63d03220af184e`.
Native comparison baseline: `3cd969a64c6daef04cce6f782e0b88dbd38c6a0f`.
This change retains native API 2 and worker protocol 3; the unchanged C++ runtime
was exercised using the previously built, verified baseline binary.

## Observed improvement

A deterministic owner fixture compared the baseline implementation with this
candidate. No provider or paid model requests were made.

| Scenario | Baseline | Candidate |
| --- | ---: | ---: |
| 50 rounds changing only output byte counts: extra context messages | 50 | 0 |
| Extra model-facing text in those rounds, characters | 18,841 | 0 |
| Source revision after those rounds | 51 | 1 |
| Messages for one ready execution event | 2 | 1 |

Output growth remains available to the harness's progress detector and explicit
reads. It no longer becomes a state-only context update each round. Event output
and its matching state are one message. These observations establish local
projection behavior, not upstream cache reads, writes or billing.

## Validation

| Check | Result |
| --- | --- |
| Pal core-a | 761 passed, 7 skipped |
| Pal core-b | 1,021 passed |
| Pal bunshin-a | 466 passed |
| Pal bunshin-b | 478 passed |
| Follow-up batch/edit/read/write/logical file-state suites | 119 passed |
| Follow-up indexed visibility, batch edit and prompt lifecycle | 32 passed |
| Native complete host suite | 130 passed |
| Follow-up install/activation compatibility checks | 4 passed |
| Native standalone runtime/worker tests | 67 passed, 2 skipped |
| SessionLifecycle TLC | 60,824 distinct states, no errors |
| HostObservation TLC | 157,948 distinct states, no errors |

Follow-up checks cover final changes made after their broad batch started; rows
overlap and are not a count of distinct tests. Skips include gated real-provider
integration and unavailable platform-specific cases. macOS and Windows execution
were not verified locally. Remote tests used independent local worker endpoints.

Host regressions exercise frozen requests, owner-atomic claim/park transitions,
failed L1 commits, missing visible state proof, byte coverage, stale events,
output read failures, bounded recovery, lost submit/release replies, and ACK-only
cleanup. Failed model continuations are not replayed. Indexed message lookup uses
turn-scoped IDs, with rollback, compaction, restoration and expired-context tests.

Reproduction uses the paired Pal source on `PYTHONPATH`, this repository's
`pal_plugin` and `tests/pal_host_support`, and the matching native binary/worker:

```sh
python -m pytest -q tests/pal_host
python -m pytest -q tests --ignore=tests/pal_host
scripts/check_session_tla.sh /path/to/tla2tools.jar
```

The Pal batches use `python scripts/run_test_batch.py <batch>` in the paired
checkout. TLC used the existing finite configurations and pinned local jar.

## Artifact and activation

Local package: `/tmp/pal-efficiency-artifacts/plugin-remote-0.4.0.palpkg`.
SHA-256: `fec860ba7466d231481c659063872d535d4436a4be979324780f833a96c069e8`.
The archive CRC, verification hook and all 24 packaged Python source files were
checked against the working source. The install hook and activation entry point
both reject Pal builds without indexed request visibility.

No tag, release, push, main merge or live activation was performed. Existing
runtime configuration and the original Pal checkout's unrelated changes remain
untouched. CI is pinned to the paired Pal commit; publish that commit before
running remote CI when publication is later requested.
