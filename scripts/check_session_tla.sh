#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
jar="${1:-${TLA2TOOLS_JAR:-}}"
[[ -f "$jar" ]] || { echo 'Supply a pinned tla2tools.jar path' >&2; exit 2; }
java -XX:+UseParallelGC -jar "$jar" -workers "${TLC_WORKERS:-1}" -cleanup \
  -config spec/session/SessionLifecycle.cfg spec/session/SessionLifecycle.tla
