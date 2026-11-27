#!/usr/bin/env bash
# Measure the FAFO collector's CPU, memory and display continuity (see measure_overhead.py).
# Usage: scripts/measure-overhead.sh [--phase idle|active|stress|all] [--window S] [--loops N]
# Window up to 120 s, loops up to 8. Set FAFO_PYTHON to pick the interpreter (needs psutil).
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
py="${FAFO_PYTHON:-python3}"
exec "$py" "$here/measure_overhead.py" "$@"
