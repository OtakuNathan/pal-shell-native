"""Check remote safety/liveness, reject known mutants, and verify the runtime table."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile

from export_remote_transitions import DESTINATION, ROOT, export

CHECKS = (
    ("RemoteExecution", "RemoteExecution", None),
    ("RemoteParallel", "RemoteExecution", None),
    ("RemoteEpoch", "RemoteExecution", None),
    ("RemotePrivilege", "RemoteExecution", None),
    ("RemoteShutdown", "RemoteExecution", None),
    ("RemoteEventDelivery", "RemoteEventDelivery", None),
    ("RemoteRestoreUnsafe", "RemoteExecution", "NoResurrection"),
    ("RemoteApprovalUnsafe", "RemoteExecution", "NoSignedRegression"),
    ("RemoteControlUnsafe", "RemoteExecution", "ControlNeedsOwnReceipt"),
    ("RemoteResurrectionUnsafe", "RemoteEventDelivery", "NoResurrection"),
    ("RemoteRegressionUnsafe", "RemoteEventDelivery", "NoRegression"),
    ("RemoteHeadBlockUnsafe", "RemoteEventDelivery", "temporal"),
    ("RemoteSettlementWitness", "RemoteExecution", "NoSettlementWitness"),
    ("RemoteConcurrencyWitness", "RemoteExecution", "NoConcurrencyWitness"),
)


def check(jar: Path, directory: Path):
    inventory = {path.stem for path in (ROOT / "spec/remote").glob("*.cfg")}
    expected = {config for config, _, _ in CHECKS} | {"ExportRemoteOperation"}
    if inventory != expected:
        raise ValueError(f"Unclassified or missing model configurations: {inventory ^ expected}")
    summary = {
        "jar_sha256": hashlib.sha256(jar.read_bytes()).hexdigest(),
        "model_sha256": {
            str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted((ROOT / "spec/remote").iterdir()) if path.suffix in {".tla", ".cfg"}
        },
        "runtime_table_matches": False,
        "checks": [],
    }
    for config, module, expected in CHECKS:
        print(f"Checking {config}...", flush=True)
        result = subprocess.run([
            "java", "-Xmx512m", "-XX:+UseParallelGC", "-jar", str(jar),
            "-workers", "1", "-metadir", str(directory / config),
            "-config", str(ROOT / f"spec/remote/{config}.cfg"),
            str(ROOT / f"spec/remote/{module}.tla"),
        ], cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
           timeout=180, check=False)
        (directory / f"{config}.log").write_text(result.stdout)
        if expected:
            marker = ("Temporal properties were violated" if expected == "temporal"
                      else f"Invariant {expected} is violated")
            passed = result.returncode != 0 and marker in result.stdout
        else:
            passed = result.returncode == 0 and "Model checking completed. No error" in result.stdout
        stats = re.findall(r"([\d,]+) states generated, ([\d,]+) distinct states found", result.stdout)
        row = {"config": config, "passed": passed, "expected_counterexample": expected,
               "returncode": result.returncode,
               "kind": "witness" if config.endswith("Witness") else "negative" if expected else "positive"}
        if stats:
            row.update(generated=int(stats[-1][0].replace(",", "")),
                       distinct=int(stats[-1][1].replace(",", "")))
        summary["checks"].append(row)
        (directory / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
        if not passed:
            raise SystemExit(result.stdout)
        print(f"  {'Expected counterexample' if expected else 'Passed'}"
              f" ({row.get('distinct', '?')} states)", flush=True)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("jar", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--write-table", action="store_true",
                        help="Regenerate the runtime table only after every model check passes")
    args = parser.parse_args()
    jar = args.jar.resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix="pal-remote-models-") as temporary:
        directory = args.output_dir.resolve() if args.output_dir else Path(temporary)
        directory.mkdir(parents=True, exist_ok=True)
        summary = check(jar, directory)
        rendered = export(jar)
        if args.write_table:
            DESTINATION.write_text(rendered)
        elif not DESTINATION.exists() or DESTINATION.read_text() != rendered:
            raise SystemExit("Runtime reducer differs from TLA; rerun with --write-table")
        summary["runtime_table_matches"] = True
        summary["runtime_table_sha256"] = hashlib.sha256(rendered.encode()).hexdigest()
        (directory / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
        print("Runtime transition table matches TLA.", flush=True)


if __name__ == "__main__":
    main()
