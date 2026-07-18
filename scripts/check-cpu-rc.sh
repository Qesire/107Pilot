#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
compose_dir="$root/simulator/compose"
env_file="${PILOT107_CPU_RC_ENV_FILE:-$compose_dir/.env.cpu-rc}"
project_name="${PILOT107_CPU_RC_PROJECT_NAME:-pilot107-cpu-rc}"
compose=(
  docker compose
  --project-name "$project_name"
  --env-file "$env_file"
  -f "$compose_dir/compose.yml"
  -f "$compose_dir/compose.competition.yml"
  -f "$compose_dir/compose.cpu-rc.yml"
  --profile competition
)

"${compose[@]}" config >/dev/null
partitions="$("${compose[@]}" exec -T slurmctld sinfo -h -o '%P')"
if [[ "$partitions" != *"CPU-RC"* ]] || [[ "$partitions" == *"GPU"* ]]; then
  echo "unexpected CPU RC partitions: $partitions" >&2
  exit 1
fi

https_port="$(awk -F= '/^PILOT107_HTTPS_PORT=/{print $2}' "$env_file" | tail -1)"
export PILOT107_COMPETITION_BASE_URL="${PILOT107_COMPETITION_BASE_URL:-https://127.0.0.1:${https_port:-8443}/api/v1}"
export PILOT107_EXPECT_CPU_ONLY=1
export PILOT107_SMOKE_PARTITION=CPU-RC
export PILOT107_SMOKE_QOS=qos_cpu_rc
PYTHONPATH="$root/src" python3 "$root/scripts/smoke_competition_web.py"
