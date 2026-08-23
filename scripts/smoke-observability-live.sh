#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
compose=(
  docker compose
  --env-file "$root/simulator/compose/.env.example"
  -f "$root/simulator/compose/compose.yml"
)

config="$("${compose[@]}" exec -T --user alice login-node-sim scontrol show config)"
if [[ "$config" != *"JobAcctGatherType       = jobacct_gather/linux"* ]]; then
  echo "observability live smoke requires JobAcctGatherType=jobacct_gather/linux" >&2
  exit 1
fi

PYTHONPATH="$root/src" python3 "$root/scripts/smoke_observability_live.py"
