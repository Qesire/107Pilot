#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
compose_dir="$root/simulator/compose"
compose=(
  docker compose
  --env-file "$compose_dir/.env.example"
  -f "$compose_dir/compose.yml"
)

# This smoke must exercise the Docker Slurm command gateway, not the
# deterministic DemoSlurmBackend.  Product behavior remains backend-neutral;
# the explicit backend selection is test provenance only.
# A Run is submitted by the API but reconciled and evidenced by the Worker.
# They therefore need the same transport to the same Slurm instance; changing
# only one would manufacture an API/Worker split-brain and invalidate the
# result of this live simulator smoke.
PILOT107_API_BACKEND=command-gateway PILOT107_WORKER_BACKEND=command-gateway "${compose[@]}" up -d \
  pilot107-api pilot107-worker pilot107-web

wait_healthy() {
  service="$1"
  for _ in $(seq 1 30); do
    container_id="$("${compose[@]}" ps -q "$service")"
    if [[ -n "$container_id" ]]; then
      status="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$container_id")"
      if [[ "$status" == "healthy" ]]; then
        return 0
      fi
      if [[ "$status" == "unhealthy" ]]; then
        "${compose[@]}" logs --tail=80 "$service" >&2
        return 1
      fi
    fi
    sleep 2
  done
  "${compose[@]}" logs --tail=80 "$service" >&2
  echo "$service did not become healthy" >&2
  return 1
}

wait_healthy pilot107-api
wait_healthy pilot107-worker
wait_healthy pilot107-web

PYTHONPATH="$root/src" python3 "$root/scripts/smoke_sim_web_interactions.py"
