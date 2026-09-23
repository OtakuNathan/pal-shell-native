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
