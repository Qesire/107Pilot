#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
compose_dir="$root/simulator/compose"
env_file="${PILOT107_COMPETITION_ENV_FILE:-$compose_dir/.env.competition}"
env_template="$compose_dir/.env.competition.example"

if [[ ! -f "$env_file" ]]; then
  cp "$env_template" "$env_file"
  echo "created $env_file from template"
fi

if [[ "${PILOT107_SKIP_BUILD:-0}" != "1" ]]; then
  bash "$root/scripts/build-slurm-sim-image.sh"
fi

compose=(
  docker compose
  --env-file "$env_file"
  -f "$compose_dir/compose.yml"
  -f "$compose_dir/compose.competition.yml"
  -f "$compose_dir/compose.competition-slurm-host.yml"
  --profile competition
)

"${compose[@]}" up -d \
  mariadb \
  slurmdbd \
  slurmctld \
  slurmrestd \
  worker-1 \
  worker-2 \
  pilot107-command-gateway

wait_healthy() {
  local service="$1"
  for _ in $(seq 1 60); do
    local container_id
    container_id="$("${compose[@]}" ps -q "$service")"
    if [[ -n "$container_id" ]]; then
      local status
      status="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$container_id")"
      if [[ "$status" == "healthy" || "$status" == "none" ]]; then
        return 0
      fi
    fi
    sleep 2
  done
  "${compose[@]}" logs --tail=100 "$service" >&2
  echo "$service did not become healthy" >&2
  return 1
}

wait_healthy mariadb
wait_healthy pilot107-command-gateway

gateway_port="$(awk -F= '/^PILOT107_COMMAND_GATEWAY_PORT=/{print $2}' "$env_file" | tail -1)"
gateway_port="${gateway_port:-127.0.0.1:18090}"
echo "competition Slurm host is running: command-gateway=${gateway_port}"

