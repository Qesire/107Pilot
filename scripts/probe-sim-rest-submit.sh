#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
bash "$root/scripts/probe-sim-rest-auth.sh" >/dev/null
PYTHONPATH="$root/src" python3 "$root/scripts/probe_sim_rest_submit.py"
