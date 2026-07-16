#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHONPATH="$root/src" python3 "$root/scripts/report_sim_behavior_fidelity.py" "$@"
