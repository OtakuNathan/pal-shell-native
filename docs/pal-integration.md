# Pal integration ownership

`pal_plugin/pal_shell_native` owns the execution implementation and its schema,
guidance, session/PTY controls, output materialization, event delivery, privilege
approval and Bunshin session driver. `pal_shell_remote` owns the remote Hub and
transport. The package uses the existing `remote` installation identity.

Pal exposes `execution:extensions`, a stable logical execution handle. The plugin
returns a ModuleHandle with an execution extension. Under the normal plugin write
fence, Pal prepares the replacement registry generation and state port, then
activates plugin event/control contributions. Failure restores the previous
projection. Logical file state, grants, output snapshot ownership and the executor remain shared.

The plugin refuses detach while execution, undelivered output, approvals or
observation delivery remain active. Pal checks this before withdrawing any plugin
surface. Idle cleanup closes the plugin's native resources and remote Hub; Pal
restores the built-in schema without closing its shared executor. Whole-host
shutdown uses asynchronous quiesce before checkpoint and resource release.

The plugin manifest declares `[execution].role_entrypoint` and `worker_modules`.
Bunshin activates contributions only from enabled, attached installed packages;
its sandbox binds declared dependencies read-only. Native roles receive the local
session implementation, without resident remote target authority.

The CPython binary is loaded once per process. Plugin detach/reload does not unload
or replace that binary. A wheel ABI change requires host process replacement.

See [installation](../README.md), [remote setup](remote-shell.md), and
[session semantics](shell_session_lifecycle.md). Acceptance lives in
`tests/pal_host`; standalone binary/worker tests remain in `tests`.

## Execution result boundary

Model-facing results use an explicit execution-field allowlist. Session state,
exit status, stdout/stderr, relevant deadlines and next-step affordances are public.
Epochs, event sequence/generation, byte cursors, claims and ACK/journal records are
host-owned. Python logging uses Pal's existing OS logging sink; durable delivery
metadata remains in L1 rather than being replaced by logs.

A read materializes and validates the complete captured output before delivering
it. Large results use the existing pager, with execution status outside the
preview. A read failure reports `output_error` alongside the known command state;
an exited command with a read failure is not reported as a failed command.

The owner performs at most three attempts per recovery batch (250ms and 1s retry
intervals), never resubmitting a side effect. Only eligible execution events can
wake the model; state revisions and cleanup retries cannot. Persistent failures
retain bounded recovery state. A reported output error consumes no byte coverage
or native ACK and does not demand a model recovery call. New execution events or
an explicit new watch restore normal pending-work tracking. Failed model turns
are never retried as ACK recovery.

Pal passes the immutable request visibility view to the execution hook after
ordinary scoped context has been selected. Native queries turn-qualified tool
results and state proofs through that view and does not scan L1 history itself.
The hook only projects ready data and never waits for process or network activity.
The package requires the paired Pal revision pinned in CI; both commits must be
available before running remote CI. No worker wire-protocol or native ABI change
is required for this host-side refactor.

### Output-save failures and input snapshots

A delivered export error does not acknowledge native stdout/stderr. Failed output
stays retained until a successful export and delivery, or an explicit release.
`shell_session(action="read", session_id=...)` retries the captured failed export;
when a one-shot command has no session, its failure affordance supplies an
`output_ref` instead. That reference supports only read and release, never command
execution. Storage failures do not change the command's actual effect or status.

A shell invocation leases the output snapshots visible in its launching model
request before execution begins. The lease survives compaction, replacement of
request pins, and turn completion, and is released on observed execution termination
or session cleanup. Unresolved execution outcomes retain the lease until reconciled
or the owner shuts down; command strings are not parsed to infer ownership.

### Result guidance

Pair this plugin with Pal's result-guidance revision. Ordinary live, watched,
PTY, and terminal results report facts without repeating a session action menu;
static relationships remain in the tool descriptors. An actual output-save/read
failure supplies a validated `shell_session(action="read", session_id=...)`
recovery action, or an `output_ref` for retained one-shot output. This applies to
ordinary results, resident completion events, and active-turn observations,
including save failures first discovered by Pal's finalizer. Recovery exports
retained output and never reruns the command. Invalid-session recovery text is
carried by `recovery_hint` instead of being hidden in host-only details.

Pal validates routes, schemas, and role scope and deduplicates the actions.
Optional actions are outside the body truncation budget and cannot crowd out
execution facts; the complete rendered result can therefore exceed that budget.
These source changes require the paired Pal resident runtime activation; copying
plugin files alone does not activate a new execution runtime.
