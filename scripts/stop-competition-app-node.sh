#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
compose_dir="$root/simulator/compose"
env_file="${PILOT107_COMPETITION_ENV_FILE:-$compose_dir/.env.competition}"
if [[ ! -f "$env_file" ]]; then
  env_file="$compose_dir/.env.competition.example"
fi

docker compose \
  --env-file "$env_file" \
  -f "$compose_dir/compose.competition-app-node.yml" \
  down
