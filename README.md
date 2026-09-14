# pal-shell-native

Optional **in-process extension backend** for [Pal](https://github.com/OtakuNathan/Pal).
It runs POSIX shell processes using Flux Foundry, dynabridge and statically linked
libuv. Install it into the Python environment used by Pal, then select
`PAL_SHELL_BACKEND=native`. Ordinary Pal installations keep the Python backend.

This is a CPython wheel, **not a `.palpkg` sidecar plugin**. It does not replace
Pal, its tool schemas, result pager, approval policy or Bunshin lifecycle. Those
host integrations stay in Pal. Installation never edits Pal or restarts a service.
Pal can read the procedure below and perform the integration on its user's behalf.

## Compatibility

- Backend 0.1.x exports `_pal_shell_runtime`, `API_VERSION = 1`.
- Known compatible Pal baseline: commit
  `da16c294f143d040f3bfc064411c4244156cefeb` (resident and Bunshin native integration).
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
"$PAL_PYTHON" -m pip install /absolute/path/to/pal_shell_native-0.1.0-MATCHING_TAGS.whl
"$PAL_PYTHON" -c 'import _pal_shell_runtime as n; print(n.__file__, n.API_VERSION)'
"$PAL_PYTHON" /absolute/path/to/test_backend.py
```

If Pal deliberately uses an OS-managed Python, do not override its package
protections. Instead install with `--target` into a **new versioned directory**
under the actual runtime root (for example `<runtime-root>/native/shell-runtime/0.1.0`).
Test that directory with the same interpreter and a temporary `PYTHONPATH`; then
add it to Pal's launch environment, preserving its existing import paths.
Check `n.__file__` to detect any older `.so` shadowing the new installation.
Do not overwrite or unload a binary already mapped by a running process.

Set `PAL_SHELL_BACKEND=native` in Pal's actual launcher/service environment. For
a systemd user service, use a service drop-in; retain existing `PYTHONPATH` entries
if adding a target directory. A venv installation normally needs no new import path.
Record the previous configuration before changing it. Changes take effect in a
new process: **notify the user to restart Pal after verification**.

## Instructions for Pal performing its own upgrade

The user's request to install this backend authorizes the following preparation.
Perform it, then tell the user exactly what changed and that a restart is needed.
Do not turn installation into an unexplained source rewrite.

1. Inspect your real launch command, Python executable, runtime root, platform,
   current backend and installed module location. Preserve unrelated local edits,
   service settings, proxy settings and credentials. Do not print secrets.
2. Check compatibility with the baseline above. If the native host adapter and
   backend factory are already present, **install and configure only**; no source
   changes are necessary. If they are absent, prepare a normal Pal upgrade to a
   compatible revision, preserving local changes. Do not invent a parallel tool
   implementation from this binary's low-level API. If that cannot be reconciled,
   explain the concrete conflict before switching the backend.
3. Download the matching release wheel, verify its checksum, and test it in a
   staging environment with the same Python version. If no wheel matches, build
   from the release source below. Install the verified result into your Pal venv
   or a fresh versioned target directory; retain the previous backend/binary.
4. Run the installed-wheel tests and the host check below **in a separate process**
   using the future launch environment. Confirm the imported module path, API
   version, short output, background completion, timeout, file output and cleanup.
   For source changes, also run Pal's native host integration suite. A successful
   import alone is not an execution test.
5. Set the persistent backend configuration only after tests pass. Review and
   report the package version, module path, files/configuration changed, checks
   run, failures if any, and exact rollback instructions. Do not commit unrelated
   edits or silently change the selected model or tool policy.
6. **Notify the user to restart; do not restart your own host or hot-unload the
   extension.** Explain that live shell sessions do not survive restart. Let
   outstanding work finish and deliver retained output before the user restarts.
   After restart, verify `shell_status` reports native and run a harmless smoke
   command. Bunshin may be remounted through its normal lifecycle when only its
   Python host code changed; replacing a loaded native module needs a new process.

Host check (using the future launch environment, from a compatible Pal install):

```bash
"$PAL_PYTHON" - <<'PY'
import asyncio
import _pal_shell_runtime as native
from pal.execution.native_shell.adapter import ShellRuntime

async def main():
    assert native.API_VERSION == 1
    print('Loaded:', native.__file__)
    runtime = ShellRuntime()
    try:
        short = await runtime.run('printf native-ok')
        assert short['session_id'] == 0 and short['stdout'] == 'native-ok'
        background = await runtime.run('sleep 0.2; printf done', wait_ms=0)
        assert background['session_id'] > 0
        result = await runtime.read(background['session_id'], wait_ms=5000)
        assert result['status'] == 'exited' and result['stdout'] == 'done'
    finally:
        await runtime.close()

asyncio.run(main())
PY
```

Rollback: restore the previous launch settings or explicitly set
`PAL_SHELL_BACKEND=python`, and notify the user to restart. Missing native modules
are startup errors when native is selected. Never retry a failed or uncertain
native command through Python automatically: its effects may already have occurred.
Keep old versioned binaries until no running process uses them.

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

Pal retains the full resident, Bunshin, paging and cancellation integration suite
under `native/shell_runtime/tests`; its CI installs this backend at a pinned
revision. Backend CI also runs those tests against the compatible Pal baseline.

## Ownership and limits

The extension owns native executors, process groups, PTYs, output files and
session IDs. Pal owns asyncio delivery, write admission, L1 acknowledgement,
result handles, tool discovery, approvals and model wakeups. A response wait
expiring exposes a session; a hard timeout cancels the process. Small output is
returned inline and large output is retained in files for Pal's existing pager.

This is a process execution backend, not a sandbox or durable process supervisor.
Close runtimes before Python finalization. Live sessions cannot resume after a
crash/restart. Descendants deliberately escaping process groups are outside its
ownership; an escaped descendant holding an output FD can delay cleanup. The
spawn exec-error handshake is not covered by a cancellable startup deadline.

See [THIRD_PARTY.md](THIRD_PARTY.md) for dependency provenance and licenses.

## Independent remote worker (0.2)

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
