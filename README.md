# pal-shell-native

Optional shell plugin for [Pal](https://github.com/OtakuNathan/Pal). Its `.palpkg`
owns the `run_shell` schema and guidance, session controls, remote routing,
observations, approvals and Bunshin integration. Its CPython wheel runs POSIX
processes through Flux Foundry, dynabridge and statically linked libuv.

Install the wheel in Pal's interpreter and attach the matching plugin package.
Attaching replaces Pal's built-in shell through the generic execution extension
port. Detaching restores the built-in shell after outstanding work is settled.
Installing the wheel alone does not activate Native shell.

## Compatibility

- Backend 0.4.0 exports `_pal_shell_runtime`, `API_VERSION = 2`.
- Known compatible Pal baseline: commit
  `3a75d38ffed0c992f110520647dc857bc3985666` (indexed L1 request visibility and execution extension contract).
  Compatible later versions must preserve that adapter contract.
- Release wheels target regular CPython 3.11–3.13 on Linux glibc 2.28+ (x86_64,
  aarch64) and macOS arm64. Linux aarch64 includes 64-bit Raspberry Pi OS with a
  matching Python/glibc. Use pip to check tags; never rename an incompatible wheel.
- No Windows, musl, free-threaded Python or subinterpreter support. Source builds
  also support macOS x86_64; binary availability is shown in each release.
- A wheel matches one CPython ABI and platform. libuv is bundled statically;
  the platform C/C++ runtime is still required.

## Install a release

Download this README, the matching wheel, `test_backend.py`, and `SHA256SUMS` from
[the release](https://github.com/OtakuNathan/pal-shell-native/releases). Verify the
checksum of each downloaded asset against that release's `SHA256SUMS` before use.
Release assets are the distribution channel; this project is not published to PyPI.

Set `PAL_PYTHON` to the **actual interpreter running Pal**, not whichever `python`
happens to be on PATH. In a Pal virtual environment, installation is:

```bash
PAL_PYTHON=/absolute/path/to/pal-venv/bin/python
"$PAL_PYTHON" -m pip install /absolute/path/to/pal_shell_native-0.4.0-MATCHING_TAGS.whl
"$PAL_PYTHON" -c 'import _pal_shell_runtime as n; print(n.__file__, n.API_VERSION)'
"$PAL_PYTHON" /absolute/path/to/test_backend.py
```

If Pal deliberately uses an OS-managed Python, do not override its package
protections. Instead install with `--target` into a **new versioned directory**
under the actual runtime root (for example `<runtime-root>/native/shell-runtime/0.4.0`).
Test that directory with the same interpreter and a temporary `PYTHONPATH`; then
add it to Pal's launch environment, preserving its existing import paths.
Check `n.__file__` to detect any older `.so` shadowing the new installation.
Do not overwrite or unload a binary already mapped by a running process.

Install `plugin-remote-0.4.0.palpkg` using Pal's package installation flow. For
offline preparation:

```sh
pal package install /absolute/path/to/plugin-remote-0.4.0.palpkg --runtime-root <runtime-root>
```

The package retains the `remote` plugin ID for upgrades from 0.4.0. Its entrypoint
is now `pal_shell_native.plugin`; `execution:extensions` is the required Pal port.
`PAL_SHELL_BACKEND` no longer selects an implementation. Preserve other launch
settings and import paths. A binary or Pal core upgrade requires a new process;
ordinary Python plugin reload uses `plugin_attach` at an idle lifecycle boundary.
Never overwrite a mapped binary or drop unresolved work to force a reload.

Validate wheel import location/API, then check the installed plugin's `run_shell`
schema contains `wait_ms`, `tty` and `target`, discover `shell_session`, and run a
harmless command. Test a background wait and observation before real work. The
acceptance suite also covers failed activation, busy detach, restored built-in
schema, remote routing and Bunshin sandbox dependencies without paid LLM calls.

To roll back the plugin, settle live sessions and retained output, then detach or
disable `remote` through Pal's lifecycle. The built-in shell becomes available.
Changing a wheel or reverting Pal core additionally requires restoring compatible
launch paths and restarting via the host supervisor. Keep previous artifacts and
runtime data. Never automatically retry an uncertain command on another backend.

## Build from source

Requirements: regular CPython 3.11+, its development headers, a C++17 compiler,
Git, and network access to download the checksum-pinned dependencies. pip's build
isolation supplies scikit-build-core, CMake (>=3.24) and Ninja when needed. The
build applies the pending dynabridge GIL fix in its own dependency tree. It does
not require sibling FF/dynabridge checkouts or a system libuv installation.

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip build
CMAKE_BUILD_PARALLEL_LEVEL=2 python -m build
python -m pip install dist/*.whl
python tests/test_backend.py
```

Run this with the same CPython minor version as Pal. A locally built Linux wheel
has a local `linux_*` tag; release CI builds and repairs `manylinux_2_28` wheels
with cibuildwheel. The source archive includes the build files and patch; building
it still requires fetching the pinned third-party archives.

For the 1,000-capture embedded-Python GIL lifetime regression, also install the
Python embedding development library and run:

```bash
cmake -S . -B build/check -DBUILD_TESTING=ON -DPython3_EXECUTABLE="$VIRTUAL_ENV/bin/python"
cmake --build build/check --parallel 2
ctest --test-dir build/check --output-on-failure
```

This repository owns the resident, Bunshin, paging and cancellation integration
suite in `tests/pal_host`. CI installs the pinned Pal baseline and this backend.
For local source checkouts, install Pal's test dependencies and this wheel, then:

```sh
PYTHONPATH=pal_plugin:tests/pal_host_support python -m unittest discover -s tests/pal_host -v
```

## Ownership and limits

The extension owns native executors, process groups, PTYs, output files and
session IDs. The plugin connects asyncio delivery, write admission, approvals and model wakeups
to Pal's generic event, L1, result pager and tool registry contracts. A response wait
expiring exposes a session; a hard timeout cancels the process. Small output is
returned inline and large output is retained in files for Pal's existing pager.

This is a process execution backend, not a sandbox or durable process supervisor.
Close runtimes before Python finalization. Live sessions cannot resume after a
crash/restart. Descendants deliberately escaping process groups are outside its
ownership; an escaped descendant holding an output FD can delay cleanup. The
spawn exec-error handshake is not covered by a cancellable startup deadline.

See [THIRD_PARTY.md](THIRD_PARTY.md) for dependency provenance and licenses.

## Independent remote worker (0.3)

This distribution also provides `pal-shell-worker` and `_pal_shell_rpc`.
The worker owns the existing Runtime independently of client connections; no tmux
or second Pal core is required. Its private Unix endpoint uses enrolled client
signatures, normally reached over strict public-key SSH forwarding. Dynabridge
provides the shell RPC projection and FF/libuv provides the async exchange.

Run `pal-shell-worker --help`. `--generate-client-key PATH` creates a mode-0600
identity without printing the private key. `--config FILE --write-service DIR
--executable PATH` writes a systemd user unit or LaunchAgent without activation.
`packaging/build_worker.py` builds a platform-specific standalone directory bundle
with PyInstaller; CI archives it preserving executable permissions. Linux x86_64,
Linux ARM64, Mac ARM64 and Mac Intel have separate build jobs.

The worker config includes worker/client identity, client public key, socket path,
Bash path, byte quotas and optional protected privilege helpers. Use a private
socket directory. A worker restart changes its epoch and cannot recover prior
sessions. Never remove a live worker socket or automatically replay an unknown
execution. Real sudo/key-store, logout and physical power behavior require target
acceptance. The service is a user process; the optional root-owned monitor is
invoked only through actual sudo and must never be installed setuid.

See the matching Pal tree's `docs/remote-shell.md` for complete configuration,
permission, output delivery and lifecycle contracts. The new remote tests run as
`python tests/test_remote.py` against the installed package. Existing `Runtime.run`
callers retain their behavior; `run_limited` is an additional bounded-output entry.
## Remote sudo setup

Run `pal-shell-worker --config /absolute/worker.toml --setup-sudo` in the
ordinary remote worker user's own terminal. On Linux 0.3 it prepares signed
management without passwords or Secret Service. Allowed forms are `apt update`
and `apt install PACKAGE...` (also `apt-get`), approved individually through Pal's
existing `run_shell(sudo=True)` path. No flags, local packages, repository changes,
upgrades/removal or arbitrary root commands. Shutdown is separately opt-in and
still requires approval; cloud/Windows/Mac power must remain disabled.

The wizard generates a fixed no-argument sudoers helper, root policy, worker
fragment and `NEXT_STEPS.txt`. It prints the exact remote absolute path and quoted
`cat` command, including for output directories containing spaces. The default is
`~/.local/share/pal-shell-sudo-setup/setup-*/`. Linux setup also copies a complete
extracted standalone worker bundle into that directory and generates
`install-root.py` plus `INSTALL_MANIFEST.json`. When running from a wheel/venv,
provide the extracted bundle path; when running the standalone worker, its own
bundle is the default. A venv is not a protected standalone bundle.

After reviewing the generated script, policy and sudoers, run the exact printed
command in your own terminal:

```sh
sudo /usr/bin/python3 -I /absolute/setup-directory/install-root.py
```

This requires system Python 3.10+ and asks only for the system's one-time sudo
authentication. The installer verifies a root-owned staged copy, strips writable
and setuid/setgid modes, checks `visudo -cf` and `visudo -c`, and backs up replaced
configuration. A failed configuration publication is rolled back. It reuses an
identical installed bundle; different content requires a new destination. The
default destination is versioned by bundle content, keeping the old bundle intact.
Pending root journal records block installation and are never deleted. Coordinate
remote submissions before installing; configuration installation is not a drain
or authorization to interrupt tasks. The manifest detects changes since setup,
not publisher authenticity: start with trusted release assets.

Installation does not run apt, store passwords, merge the worker configuration or
restart services. Follow `NEXT_STEPS.txt` to activate the worker configuration and
verify one approved operation separately. The helper
independently verifies signed operations and durably reserves each ID before
execution. Retain its root journal through upgrades; an uncertain operation must
be queried, never reexecuted by deleting state. NOPASSWD must not be granted to a
shell, apt directly, or the old arbitrary-command helper.

macOS retains Keychain, protected askpass and its explicit STORE prompt. Windows
remains unsupported for sudo/power. The wizard neither activates services nor
claims E2E. Verify approved `apt update` on Linux (approved `id` on Mac) separately.

## Multiplexed transport and upgrades

Install matching 0.4.0 wheel, worker and palpkg: protocol-v3 negotiation rejects an
old worker before command submission. A 64-bit transport request ID wraps the
unchanged Dynabridge payload. One caller-owned libuv RPC loop/executor per Hub or
worker owns accepts, connected I/O and FF request awaits. Python business callbacks
and the native process Runtime keep their existing owner loops. Slot locking is
limited to connection lifecycle; slow replies do not block unrelated requests.

Limits are 32 in-flight calls per connection, 1 MiB frames and 8 MiB queued sends.
Slots asynchronously queue up to 128 more calls in FIFO order for at most 30 seconds;
queued cancellation/detach never sends a request, and waiting timeout/overflow is
NOT_STARTED. This does not serialize active RPC round trips.
Cancellation/timeouts retain the channel and discard late replies without replay.
Idle authenticated connections no longer expire after 90 seconds. Handshake and
partial-frame deadlines remain 30 seconds. Runtime epochs and execution IDs remain
independent of transport IDs. Upgrade during a coordinated window after resolving
active sessions and retained outputs; building/installing files does not reload Pal.

## Companion Pal plugin

`pal_plugin/` owns the Native execution implementation, tools, session lifecycle
adapter, Bunshin driver, remote Hub, target Slots, setup skill and manifest.
It ships as `plugin-remote-0.4.0.palpkg` alongside the native wheels and independent
worker bundles. The worker needs no Pal installation; the client plugin runs in
Pal's host interpreter and reuses its ports, sidecar and resource lifecycle APIs.

With Pal's package tools installed, build using:

```sh
pal package build pal_plugin --output dist
```

Install the matching native wheel into Pal's interpreter first, then use
`pal package install dist/plugin-remote-0.4.0.palpkg --runtime-root <runtime-root>`
for offline preparation, or the running host's authorized package installation
flow. Verification checks the native Runtime, RPC client and resident Pal contract;
it does not install dependencies or restart services. The plugin uses the existing
community package lifecycle and defaults to enabled. A missing or empty target
configuration starts no Hub process. Native wheel installation alone does not
install the companion palpkg into any runtime.

Pal integration tests need `pal_plugin` on PYTHONPATH when testing source checkouts.
Standalone worker bundles intentionally contain only the worker-side package.

## Experimental Windows worker

The Windows adapter reuses Runtime's session/reactor and FF completion state, with
CreateProcessW and Job Objects owning noninteractive PowerShell processes and
descendants. It supports background sessions, query/terminate, timeouts and bounded
UTF-8 output. ConPTY, input/resize, privilege and machine power management are not
implemented; unsupported operations fail before execution. This does not enable
Pal's Python shell or claim Windows Pal client support.

Build with CMake, a Windows C++ toolchain and matching Python development files.
Run `tests/test_windows_worker.py` with both compiled extensions and `python/` on
PYTHONPATH. `packaging/build_windows_worker.ps1` assembles an isolated directory
bundle from a complete Python distribution with msgpack/cryptography installed,
plus the compiled `.pyd` files. The independent `windows-prototype.yml` CI runs on
Windows x64 / Python 3.13 for pull requests, main pushes and manual dispatch. It
builds both native extensions, tests PowerShell sessions, then repeats those tests
using the bundled interpreter and uploads a prototype ZIP with its SHA-256.
Windows artifacts remain outside the official release matrix. This checks the
worker, not Windows Pal clients or an SSH deployment.

Configure an absolute PowerShell path, a private endpoint JSON path (`socket_path`),
`tcp_port` and `shutdown_policy = "disabled"`. The server listens only on loopback;
Pal's `worker_port` target setting forwards to it through authenticated SSH.
RPC client identity enrollment is unchanged. Start the worker independently of
the SSH session under an ordinary user, for example with a manually triggered,
limited-privilege interactive Scheduled Task. No startup/shutdown actions are
configured for this target. See Pal's `docs/remote-shell.md` for the full contract.


## Session observation and deadlines (0.4.0)

`run` retains its existing response wait: no result is returned until exit or that
wait expires. Expiry returns a live session, not failure. The process deadline is
independent and optional. Native control methods take a request ID and session ID:
`watch(request, session, wait_ms, extend_by_ms)`, `extend(request, session, delta)`
and `unwatch(request, session)`. Watch returns immediately, replaces one pending
watch, and can atomically extend a finite deadline. Unwatch suppresses future model
notifications; it does not terminate the process, release its running write lease,
or detach it from its Runtime's lifetime. Read never rearms or renews anything.

Snapshots carry `event_kind`, `event_sequence`, `watch_generation`, `watching`,
`elapsed_ms`, nullable `remaining_ms`, and nullable `wake_remaining_ms`. Unsolicited
native terminal snapshots still reach the delivery adapter for bookkeeping when
unwatched; they must not start an agent turn. The host rechecks observation generation
before delivery and uses stable event IDs for L1 deduplication. It appends updates
instead of editing old tool results. Output is retained under the existing bounded
lifetime: a retired output is not evidence that its command never ran.

Finite deadline extensions are additive and rejected at/after expiry or termination.
No deadline returns `no_deadline`; extensions do not invent a new finite budget.
The remote operation ID is an idempotency key: query/retry that operation after a
lost reply instead of submitting a fresh extension. Signed management operations
reject renewal with `deadline_not_extendable`.

Run `scripts/check_session_tla.sh /path/to/tla2tools.jar` for the independent model,
`ctest --test-dir build/check --output-on-failure` for native transitions and lifetime,
and `python tests/test_session_lifecycle.py` for installed-extension acceptance.
