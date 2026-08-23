#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
compose=(
  docker compose
  --env-file "$root/simulator/compose/.env.example"
  -f "$root/simulator/compose/compose.yml"
)

"${compose[@]}" up -d mariadb slurmdbd slurmctld worker-1 worker-2 login-node-sim >/dev/null
for _attempt in $(seq 1 60); do
  if "${compose[@]}" exec -T --user alice login-node-sim \
    sinfo --noheader --format '%P' >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
"${compose[@]}" exec -T --user alice login-node-sim \
  sinfo --noheader --format '%P' >/dev/null

cd "$root"
PYTHONPATH="$root/src" uv run python scripts/smoke_pilot_agent_a3_live.py
