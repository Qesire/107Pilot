#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Ensure the auth probe artifact exists (the submit smoke does not strictly
# require it, but running it first gives a clean signal that slurmrestd is up
# and JWT auth works before we spend submit quota).
bash "$root/scripts/probe-sim-rest-auth.sh" >/dev/null
PYTHONPATH="$root/src" python3 "$root/scripts/smoke_sim_rest_live.py"
