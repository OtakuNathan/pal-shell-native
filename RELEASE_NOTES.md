0.4.0 moves the complete Pal Native shell integration into this extension.

- The `remote` palpkg now supplies local Native `run_shell`, sessions, PTY controls,
  observations, remote routing, approvals and the remote setup skill.
- Pal supplies a generic lifecycle-fenced execution replacement port. Attach
  publishes the plugin's schema; idle detach restores the built-in shell. Busy
  detach preserves the active plugin and its work. Failed startup restores the
  original implementation.
- Bunshin discovers worker activation and sandbox dependencies from attached plugin
  metadata. `PAL_SHELL_BACKEND` is retired.
- Resident/Bunshin host acceptance tests and shell documentation live here.

Requires the Pal execution extension contract and matching 0.4.0 palpkg/wheel.
Native API 2 and remote protocol 3 are unchanged; FF, dynabridge and the modeled
native session state machine are unchanged. Update Pal core in a new host process;
install the palpkg and wheel as separate artifacts. Do not overwrite loaded binaries.


This reissued 0.4.0 release replaces the earlier 0.4.0 artifacts with the current
implementation. Identify artifacts by commit and SHA-256. The verified Pal baseline
is `3a75d38ffed0c992f110520647dc857bc3985666`.

- A returned session no longer incurs a second wait before the next model request.
  Request assembly reads only prepared host snapshots; observation updates do not
  independently wake models.
- Owner-serialized event claims and atomic L1 coverage prevent duplicate delivery
  and lost wakeups. ACK cleanup does not repeat committed model/tool effects.
- Protocol 3 optionally advertises journal-free observation. Older workers retain
  event delivery; remote downloads resume validated prefixes after interruption.
- Native and host-composition TLA+ models and focused race regressions accompany
  the implementation. Cache cost profiles and checkpoint policies are unchanged.
