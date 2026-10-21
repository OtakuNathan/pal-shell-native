0.6.0 adds the remote file protocol and one exact target resolver.

- `read_file`, `edit_file` and `write_file` now work on remote targets through an
  explicit worker file protocol (version 1). The worker keeps file authority:
  read-before-edit digests, stale-content rejection and bounded chunked transfers
  return the same receipts as local calls. A worker that does not advertise the
  protocol rejects these calls with `file_protocol_unsupported` instead of
  guessing at a fallback.
- Every entry point resolves a target with one exact registry lookup: an integer
  ID, or an exact configured name or shortcut. Numeric strings are aliases, never
  IDs; unknown, ambiguous or non-positive values fail closed with `invalid_target`
  or `ambiguous_target`. Zero remains the local host.
- Plugin 0.6.0 verifies the host at load for the worker input and file modules,
  file delivery protocol v1 and history retirement, and refuses to load against
  an older host runtime instead of starting partially.
- Activation: install the 0.6.0 native wheel on the Pal host, install
  plugin-remote-0.6.0.palpkg, then replace each worker bundle and its running
  process. Host and plugin move together; a worker on an older bundle keeps its
  execution tools and gains the file trio only after its bundle and process are
  replaced.
- Native API 2 and remote protocol 3 are unchanged.
- CI checks the exported file engine against Pal
  `c26c25fb45cd122a2ac92cd602dceb6a48ad3674`; a later unrelated Pal commit does not
  change the exported engine.

Unreleased: temporary side-question execution ownership.

- Calls marked `transient_execution` retain temporary shell-session ownership.
  Automatic observations and completion turns do not deliver their output to
  the resident conversation. The host drains acknowledgements, terminates and
  releases remaining temporary sessions at the end of the one-shot turn, while
  preserving main-owned sessions. Tool schemas and permissions are unchanged.
- Cleanup survives cancellation of the side turn. Unconfirmed remote operations
  retain their original tickets; reconnect queries those tickets and retires late
  sessions and output without resubmitting commands or restoring a conversation.
- Temporary readers borrow main-owned session/output data without acknowledging
  or releasing it. Read failures and cleanup leave the original delivery pending.
- Backend tests exercise the temporary-invocation metadata and native retirement
  boundary against the workflow's pinned Core. Pal owns the `/btw` fork/lifecycle
  integration tests, so these plugin tests do not require an unreleased Core API.
- Deploy these host-plugin changes together with Core's temporary-turn lifecycle.
  They do not require a native C++ rebuild; the worker environment fix below does
  require rebuilding the standalone worker bundle.

Unreleased: frozen Linux worker subprocess environment.

- Bundled workers restore `LD_LIBRARY_PATH_ORIG` before launching external
  programs, or remove the bundle's `LD_LIBRARY_PATH` when no original existed.
  User-provided paths survive; an explicitly saved empty original is restored
  exactly. PyInstaller may normalize an initially empty path to an unset value;
  both retain system-default lookup. Unfrozen workers are unchanged. This prevents
  bundled OpenSSL from overriding system tool libraries.
- Shell guidance explains that `pkill -f` can match the outer shell command line.
- Activation requires rebuilding and restarting the remote worker. Updating host
  plugin guidance alone does not replace an already running worker.

Unreleased: concurrent shell sessions without session-wide write exclusion.

- Pal continues to dispatch each turn's tool calls sequentially. A retained shell
  session no longer prevents another shell command or other write tools from
  running, locally or on the same remote target.
- Running results remind the agent to avoid conflicting input-file changes and
  output writes, and to verify results again if relevant inputs changed.
- Classified verification uses the same admission policy and still waits for its
  execution result before recording evidence. Nested execution no longer rejects
  itself through a host write lock.
- CI and the documented Pal baseline now use
  `9d8cbe906906522e1f4a0d2a50a709385a4209f3`, which supports the plugin's
  enum-field tool discovery. Host CI uses pytest to include both unittest and
  pytest integration cases.
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
