#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
compose_dir="$root/simulator/compose"
env_file="${PILOT107_CPU_RC_ENV_FILE:-$compose_dir/.env.cpu-rc}"
project_name="${PILOT107_CPU_RC_PROJECT_NAME:-pilot107-cpu-rc}"

docker compose \
  --project-name "$project_name" \
  --env-file "$env_file" \
  -f "$compose_dir/compose.yml" \
  -f "$compose_dir/compose.competition.yml" \
  -f "$compose_dir/compose.cpu-rc.yml" \
  --profile competition \
  down
