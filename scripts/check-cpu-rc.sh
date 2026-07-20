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

# P1-4: bind running container image IDs to RELEASE_MANIFEST.json.
# After the partition + HTTP smoke checks pass, verify every running cpu-rc
# container's image ID matches the content_digest of the manifest record whose
# reference matches the container's Config.Image. Skipped (with a warning, not
# a failure) when RELEASE_MANIFEST.json is absent (e.g. dev runs outside a
# bundle). Set PILOT107_SKIP_IMAGE_BINDING_CHECK=1 to skip explicitly.
manifest_default="$root/RELEASE_MANIFEST.json"
if [[ "${PILOT107_SKIP_IMAGE_BINDING_CHECK:-0}" == "1" ]]; then
  echo "skipping image binding check (PILOT107_SKIP_IMAGE_BINDING_CHECK=1)"
elif [[ -f "${PILOT107_RELEASE_MANIFEST_PATH:-$manifest_default}" ]]; then
  PILOT107_RELEASE_MANIFEST_PATH="${PILOT107_RELEASE_MANIFEST_PATH:-$manifest_default}" \
    PILOT107_CPU_RC_ENV_FILE="$env_file" \
    PILOT107_CPU_RC_PROJECT_NAME="$project_name" \
    bash "$root/scripts/verify-cpu-rc-image-binding.sh" >/dev/null
else
  echo "WARNING: RELEASE_MANIFEST.json not found; skipping image binding check (set PILOT107_RELEASE_MANIFEST_PATH to enable)" >&2
fi
