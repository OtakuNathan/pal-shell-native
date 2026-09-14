0.2.0 adds an independent user worker and authenticated remote shell RPC.

- Reuse Dynabridge RPC and FF/libuv, with a separate shell projection.
- Keep Runtime/session/PTY ownership independent of SSH and client connections.
- Add execution deduplication, Runtime fencing, bounded output snapshots and metadata.
- Add signed single-use privileged-operation grants, remote-only askpass and a
  non-setuid root monitor, with physical-target acceptance still required.
- Generate Linux user-service / Mac LaunchAgent definitions without activation.
- Build standalone worker archives and Mac Intel wheels alongside existing platforms.
- Ship the Pal-side remote Hub/Slot plugin as a companion palpkg using Pal's
  existing package installation and lifecycle, with native prerequisite checks.
- Preserve existing local Runtime API and add optional hard output bounds.
- Add an experimental Windows PowerShell process adapter using the same Runtime,
  with Job Object cancellation and SSH-forwarded loopback RPC. No ConPTY or power
  management; Windows publication remains outside the release matrix.

This package does not enable remote in Pal, alter user configuration, install
privileged helpers as root, activate a service, or restart anything. Use matching
Pal integration and follow `docs/remote-shell.md` in that repository.
