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
  -f "$compose_dir/compose.yml" \
  -f "$compose_dir/compose.competition.yml" \
  --profile competition \
  config >/dev/null

https_port="$(awk -F= '/^PILOT107_HTTPS_PORT=/{print $2}' "$env_file" | tail -1)"
https_port="${https_port:-8443}"
export PILOT107_COMPETITION_BASE_URL="${PILOT107_COMPETITION_BASE_URL:-https://127.0.0.1:${https_port}/api/v1}"

PYTHONPATH="$root/src" python3 "$root/scripts/smoke_competition_web.py"
