0.3.0 adds multiplexed protocol-v2 transport and Linux signed management.

- Reuse the existing Dynabridge framing and FF uv_executor without changing either library.
- Share one persistent native RPC loop per Hub/worker, route out-of-order replies by
  request ID, and bound in-flight requests/frames/send queues.
- Keep other requests alive on cancellation/timeouts; remove the authenticated
  90-second idle disconnect. Never replay uncertain execution or PTY input.
- Classify privileged failures before commit as NOT_STARTED without retaining a
  write lock; preserve reconciliation tickets after an uncertain commit.
- Limit Linux sudo=True to approved apt update/install forms. A protected fixed
  sudoers helper verifies normalized signed actions and durably prevents replay.
- Pass literal package selectors to apt-get, preventing regex expansion or suffix
  operations. Preserve helper grants across asynchronous initial session snapshots,
  and revoke unused grants when the process exits.
- Queue excess Slot requests asynchronously with bounded FIFO admission, cancellation
  and NOT_STARTED expiry; never expose lease-capacity failures as uncertain execution.
- Generate administrator-reviewed Linux management templates without passwords;
  print the exact remote NEXT_STEPS path. Keep Mac Keychain behavior and Windows
  ordinary PowerShell execution; no Windows power or privilege management.
- Extend transport, lifecycle, management and installer acceptance, including
  Windows TCP multiplexing and an opt-in 95-second idle regression.

Requires matching 0.3.0 native wheel, remote palpkg and worker (protocol 2).
Coordinate active sessions/output before upgrading. No installer automatically
changes sudoers, restarts a worker/Pal or performs physical power actions.

Upgrade requirements:
- Update Pal to 3dbad0545c7b3892ed1afe896e57892c9693c637 or later.
- Install the wheel matching the Pal host's OS, architecture and Python 3.11/3.12/3.13,
  then install plugin-remote-0.3.0.palpkg in the same runtime.
- Ubuntu AMD64: use pal-shell-worker-ubuntu-latest.tar.gz (built on Ubuntu 24.04).
  Ubuntu ARM64: use pal-shell-worker-ubuntu-24.04-arm.tar.gz.
  Mac ARM64: use pal-shell-worker-macos-latest.tar.gz; Intel: macos-15-intel.
- Preserve worker identity/configuration and root operation journals. Reconcile
  UNKNOWN operations and deliver retained output before coordinated service updates.
- Linux sudo requires the new --setup-sudo templates and administrator installation
  of the protected management helper/sudoers; replacing the binary alone is insufficient.
  Normal execution does not require sudo setup. Existing SSH keys/targets can remain.
- Windows remains experimental, with separate CI artifacts; it is not included in
  these POSIX release bundles and has no power or privilege management.
