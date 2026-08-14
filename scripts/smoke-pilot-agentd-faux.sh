#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
compose_file="$root/simulator/compose/compose.yml"
project="pilot107-agentd-smoke-$$"
agentd_image="${PILOT107_AGENTD_IMAGE:-pilot107/agentd:local}"
python_image="${PILOT107_API_IMAGE:-pilot107/api:local}"
token="faux-smoke-token"
compose=(docker compose -p "$project" -f "$compose_file" --profile apps)

cleanup() {
  "${compose[@]}" down --remove-orphans >/dev/null 2>&1 || true
}
trap cleanup EXIT

PILOT107_AGENTD_IMAGE="$agentd_image" \
PILOT107_AGENTD_MODEL_PROFILE=faux-default \
PILOT107_AGENTD_FAUX_SCENARIO=a0-smoke \
PILOT107_AGENTD_TOKEN="$token" \
  "${compose[@]}" up -d pilot-agentd

container_id="$("${compose[@]}" ps -q pilot-agentd)"
if [[ -z "$container_id" ]]; then
  echo "pilot-agentd faux smoke failed: Agentd container was not created" >&2
  exit 1
fi

for _attempt in $(seq 1 30); do
  if [[ "$(docker inspect --format '{{.State.Health.Status}}' "$container_id")" == "healthy" ]]; then
    break
  fi
  sleep 1
done
if [[ "$(docker inspect --format '{{.State.Health.Status}}' "$container_id")" != "healthy" ]]; then
  "${compose[@]}" logs pilot-agentd >&2
  echo "pilot-agentd faux smoke failed: readiness timeout" >&2
  exit 1
fi

network="$(docker inspect --format '{{range $name, $_ := .NetworkSettings.Networks}}{{$name}}{{end}}' "$container_id")"
if [[ -z "$network" ]]; then
  echo "pilot-agentd faux smoke failed: Agentd network was not found" >&2
  exit 1
fi

docker run --rm \
  --network "$network" \
  --read-only \
  --cap-drop ALL \
  --security-opt no-new-privileges:true \
  --user 10700:10700 \
  -e PYTHONDONTWRITEBYTECODE=1 \
  -e PYTHONPATH=/workspace/src \
  -e PILOT107_AGENTD_URL=http://pilot-agentd:8091 \
  -e PILOT107_AGENTD_TOKEN="$token" \
  -e PILOT107_AGENTD_MODEL_PROFILE=faux-default \
  -v "$root:/workspace:ro" \
  -w /workspace \
  "$python_image" \
  python3 scripts/smoke-pilot-agentd-faux.py
