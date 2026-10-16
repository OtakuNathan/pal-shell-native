"""Export the normative TLC-evaluated reducer, or check the committed copy."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "spec/remote/RemoteOperation.tla"
DESTINATION = ROOT / "pal_plugin/pal_shell_contracts/_remote_transitions.py"


def export(jar: Path) -> str:
    with tempfile.TemporaryDirectory(prefix="pal-tla-export-") as directory:
        result = subprocess.run([
            "java", "-Xmx256m", "-XX:+UseParallelGC", "-jar", str(jar.resolve()),
            "-workers", "1", "-metadir", directory,
            "-config", str(ROOT / "spec/remote/ExportRemoteOperation.cfg"),
            str(ROOT / "spec/remote/ExportRemoteOperation.tla"),
        ], cwd=ROOT, text=True, capture_output=True, check=False)
    if result.returncode:
        raise RuntimeError(result.stdout + result.stderr)
    edges, permits, metadata = {}, {}, {}
    host = {"ADOPT": {}, "STALE": {}, "POLL": {}}
    valid = {}
    for line in result.stdout.splitlines():
        if not line.startswith(tuple(f'"{tag}|' for tag in ("EDGE", "PERMIT", "META", "VALID", *host))):
            continue
        fields = json.loads(line).split("|")
        if fields[0] == "EDGE":
            _, kind, hold, phase, event, target = fields
            key = kind, hold == "TRUE", phase, event
            if key in edges:
                raise ValueError(f"Duplicate transition: {key}")
            edges[key] = target
        elif fields[0] == "PERMIT":
            _, kind, phase, relation, same, allowed = fields
            permits[kind, phase, relation, same == "TRUE"] = allowed == "TRUE"
        elif fields[0] == "META":
            _, phase, public, unsigned = fields
            metadata[phase] = public, unsigned == "TRUE"
        elif fields[0] == "VALID":
            _, kind, phase, allowed = fields
            valid[kind, phase] = allowed == "TRUE"
        else:
            host[fields[0]][tuple(f == "TRUE" for f in fields[1:-1])] = (
                fields[-1] if fields[0] == "POLL" else fields[-1] == "TRUE")
    kinds = sorted({key[0] for key in edges})
    phases = sorted(metadata)
    events = sorted({key[3] for key in edges})
    if len(edges) != len(kinds) * 2 * len(phases) * len(events) or not edges:
        raise ValueError("Incomplete transition export")
    if len(permits) != len(kinds) * len(phases) * 3 * 2:
        raise ValueError("Incomplete authorization export")
    lines = [
        '"""Generated from spec/remote/RemoteOperation.tla; do not edit."""',
        f"SPEC_SHA256 = {hashlib.sha256(SPEC.read_bytes()).hexdigest()!r}",
        f"KINDS = frozenset({tuple(kinds)!r})",
        f"PHASES = frozenset({tuple(phases)!r})",
        f"EVENTS = frozenset({tuple(events)!r})",
        f"PUBLIC_PHASE = {dict((p, metadata[p][0]) for p in phases)!r}",
        f"UNSIGNED_PHASES = frozenset({tuple(p for p in phases if metadata[p][1])!r})",
        f"VALID_PHASES = frozenset({tuple(key for key, allowed in sorted(valid.items()) if allowed)!r})",
        "",
        "# Self transitions are omitted; all inputs are validated by the adapter.",
        "TRANSITIONS = {",
    ]
    if len(valid) != len(kinds) * len(phases):
        raise ValueError("Incomplete phase type export")
    lines.extend(f"    {key!r}: {target!r}," for key, target in sorted(edges.items())
                 if key[2] != target)
    lines.extend(["}", "", "PERMITTED = frozenset({"])
    lines.extend(f"    {key!r}," for key, allowed in sorted(permits.items()) if allowed)
    lines.extend(["})", ""])
    for tag, expected_count in (("ADOPT", 8), ("STALE", 16), ("POLL", 4)):
        if len(host[tag]) != expected_count:
            raise ValueError(f"Incomplete {tag} export")
        lines.append(f"{tag} = {dict(sorted(host[tag].items()))!r}")
    lines.append("")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("jar", type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    rendered = export(args.jar)
    if args.check:
        if not DESTINATION.exists() or DESTINATION.read_text() != rendered:
            raise SystemExit("Remote reducer is stale; run scripts/export_remote_transitions.py JAR")
        print("Remote reducer matches the TLC export.")
    else:
        DESTINATION.write_text(rendered)
        print(f"Exported {DESTINATION.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
