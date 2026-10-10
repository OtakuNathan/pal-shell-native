Unreleased: concurrent shell sessions without session-wide write exclusion.

- Pal continues to dispatch each turn's tool calls sequentially. A retained shell
  session no longer prevents another shell command or other write tools from
  running, locally or on the same remote target.
- Running results remind the agent to avoid conflicting input-file changes and
  output writes, and to verify results again if relevant inputs changed.
- Classified verification uses the same admission policy and still waits for its
  execution result before recording evidence. Nested execution no longer rejects
  itself through a host write lock.
- Session ownership, output delivery, cancellation, capacity limits and lifecycle
  checks remain. UNKNOWN remote operations stay retained and are not replayed;
  independent commands can proceed. Controls on one session remain serialized.
- Running sessions reserve retained-result capacity before spawning; concurrent
  completions cannot evict unacknowledged output or exceed the configured bound.
- Native API 2 lease methods retain releasable tokens for compatibility, without
  granting exclusive write access. Remote protocol 3 is unchanged.
- Activation requires the updated native binary and host plugin. Remote workers
  need the updated native binary to allow concurrent sessions on their target.
  Fixing the host verification self-lock alone does not require a worker update;
  older workers remain compatible and keep their single-session restriction.
  Source edits and successful tests do not activate an existing Pal or worker.

0.4.2 names the owner in write admission rejections.

- The native Runtime appends the active writer id to `write_busy` errors, so a
  rejected write identifies the session or lease holding the context.
- The host plugin reports its live session and pending-request ids when another
  host write is active, pointing the caller at the blocking work directly.
- No behavior, API or protocol changes: Native API 2 and remote protocol 3 are
  unchanged. 0.4.1 peers interoperate; workers gain the detailed messages
  after upgrading.
- Activation: install the 0.4.2 native wheel and plugin-remote-0.4.2.palpkg on
  the Pal host and update worker bundles to 0.4.2 as convenient.

The verified Pal integration baseline remains
3a75d38ffed0c992f110520647dc857bc3985666.
Windows remains experimental and has no power or privilege management.

0.4.1 isolates Native shell execution admission by target.

- Local execution and each remote slot have independent execution barriers. A busy
  or UNKNOWN remote operation blocks only its own target. Busy submissions fail
  immediately; transport admission retains its existing bounded queue.
- An idle worker can restart and reconnect through its own slot without reloading
  the Hub or local Runtime. Old sessions remain bound to their original Runtime;
  UNKNOWN effects are never forgotten or replayed.
- Retained execution claims survive Hub replacement. Output downloads and event
  polling no longer serialize unrelated remote targets.
- Each configured unique shortcut generates run_shell_<shortcut> with a fixed
  target, preserving ordinary approval, output delivery and caller permissions.
- Native run_shell offers consent-based enrollment of newly reachable SSH hosts
  through pal.remote.setup.
- cwd expands ~ on the execution endpoint: Pal's home for local commands and the
  worker's home for remote commands, including privileged command preparation.

This reissued 0.4.1 also includes the POSIX TMPDIR compatibility fix used by the
user's phone worker: output files use a nonempty TMPDIR, falling back to /tmp only
when unset or empty. An unusable configured directory fails before spawning the
command; file privacy and cleanup are unchanged. This helps environments with a
nonstandard temporary directory, such as Termux. Android binaries are not part of
the release matrix; update phone builds from source using the existing setup.

This intentionally replaces the first 0.4.1 artifacts. Identify the reissue by its
Git tag commit and SHA256SUMS, not the version string alone.

Activation: install the 0.4.1 native wheel and plugin-remote-0.4.1.palpkg on the
Pal host, and update remote worker bundles to 0.4.1 for remote cwd expansion.
Coordinate active sessions and retained output before replacing loaded code or
restarting a worker. Publishing/installing files is not runtime activation.

Native API 2 and remote protocol 3 are unchanged; 0.4.0 workers remain compatible
but do not gain remote cwd expansion until updated. No Pal core, FF, Dynabridge,
native session state machine or privilege policy changes are required.
The verified Pal integration baseline remains
3a75d38ffed0c992f110520647dc857bc3985666.
Windows remains experimental and has no power or privilege management.
