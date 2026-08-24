#!/usr/bin/env bash
set -euo pipefail

market_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
compose=(
  docker compose
  --env-file "$market_root/simulator/compose/.env.example"
  -f "$market_root/simulator/compose/compose.yml"
)

"${compose[@]}" up -d mariadb slurmdbd slurmctld worker-1 worker-2 login-node-sim >/dev/null
"${compose[@]}" restart slurmctld worker-1 worker-2 >/dev/null
for _attempt in $(seq 1 60); do
  if "${compose[@]}" exec -T --user alice login-node-sim \
    sinfo --states=idle --noheader --format '%N' 2>/dev/null | rg -q .; then
    break
  fi
  sleep 1
done
"${compose[@]}" exec -T --user alice login-node-sim \
  sinfo --states=idle --noheader --format '%N' | rg -q .

cd "$market_root"
PYTHONPATH="$market_root/src" uv run python scripts/smoke_pilot_agent_market_live.py
