#!/usr/bin/env bash
set -euo pipefail

a4_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
compose=(
  docker compose
  --env-file "$a4_root/simulator/compose/.env.example"
  -f "$a4_root/simulator/compose/compose.yml"
)

"${compose[@]}" up -d mariadb slurmdbd slurmctld worker-1 worker-2 login-node-sim >/dev/null
# D1 owns this isolated simulator stack. Restart scheduler daemons together so
# long-lived containers cannot retain stale wall-clock state across host resume.
"${compose[@]}" restart slurmctld worker-1 worker-2 >/dev/null
for _attempt in $(seq 1 60); do
  if "${compose[@]}" exec -T --user alice login-node-sim \
    sinfo --states=idle --noheader --format '%N' 2>/dev/null | grep -q .; then
    break
  fi
  sleep 1
done
"${compose[@]}" exec -T --user alice login-node-sim \
  sinfo --states=idle --noheader --format '%N' | grep -q .

cd "$a4_root"
PYTHONPATH="$a4_root/src" uv run python scripts/smoke_pilot_agent_a4_live.py
