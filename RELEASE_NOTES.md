0.4.0 adds explicit native session observation and renewable finite deadlines.

- Model session lifecycle independently in TLA+ before implementation; check one-shot
  watches, quiet unwatch, exact extension accounting and finite-budget termination.
- Separate foreground response waits, execution deadlines and observation state.
  Missing timeout_ms still means unlimited execution. Reads never renew a deadline.
- Add watch(wait_ms, extend_by_ms=0), extend(extend_by_ms) and unwatch session controls.
  Watch arms one background decision event; unwatch silences notifications without
  stopping the process. Termination and release remain separate operations.
- Extend the existing monotonic deadline, never now + delta. Reject expired,
  terminating and unlimited sessions; signed management budgets cannot be extended.
- Preserve event sequence and watch generation through native and worker snapshots.
  Remote control retries reuse the operation journal and do not double the extension.
- Keep terminal execution evidence when notifications are disabled. Unwatched terminal
  output is eligible for bounded native reclamation, rather than blocking capacity.
- Ship virtual-clock transition tests, real process/RPC regressions and a pinned TLC
  release gate alongside the existing platform acceptance matrix.

Requires a matching Pal adapter, native API 2, remote protocol 3 and the 0.4.0
worker/plugin. Old workers fail negotiation explicitly. Upgrading files does not
reload a mapped binary; activate the matching host when idle. Remote machines are
not modified by the local installer. No PyPI publication is introduced.
