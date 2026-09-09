Optional native shell backend extracted from Pal, with the existing Python backend retained as the default.

- CPython 3.11–3.13 wheels for Linux glibc 2.28+ x86_64/aarch64 and macOS arm64.
- In-process Flux Foundry/dynabridge execution with statically linked libuv.
- Retained sessions, PTY, cancellation, bounded inline output and complete file output.
- README describes agent-assisted installation, compatibility checks, rollback and user-controlled restart.
- Release publication requires installed-wheel tests, source-archive build, Pal resident/Bunshin integration and GIL lifetime checks.

Download README.md first. This is a native Python extension, not a sidecar `.palpkg`. It requires compatible Pal host integration (baseline `da16c294f143d040f3bfc064411c4244156cefeb`). It does not edit Pal or restart it during installation.
