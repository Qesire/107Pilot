#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
compose_file="$root/simulator/compose/compose.yml"
env_file="$root/simulator/compose/.env.example"

if [[ "${1:-}" == "--contract" ]]; then
  cd "$root"
  uv run pytest -q \
    tests/test_agent_turn_worker.py::test_provider_unavailable_blocks_only_the_scoped_generative_project \
    tests/agent/test_workspace_snapshot.py::test_snapshot_keeps_large_weights_metadata_only
  exit 0
fi

default_compose=(docker compose --env-file "$env_file" -f "$compose_file")
"${default_compose[@]}" up -d mariadb slurmdbd slurmctld worker-1 worker-2 login-node-sim >/dev/null
for _attempt in $(seq 1 60); do
  if "${default_compose[@]}" exec -T --user alice login-node-sim \
    sinfo --noheader --format '%P' >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
"${default_compose[@]}" exec -T --user alice login-node-sim \
  sinfo --noheader --format '%P' >/dev/null
(
  cd "$root"
  PYTHONPATH="$root/src:$root/scripts" uv run python \
    scripts/smoke_agent_lifecycle_boundaries.py live-large-file
)

project="pilot107-lifecycle-boundaries-$$"
isolated=(docker compose -p "$project" -f "$compose_file" --profile apps)
cleanup() {
  "${isolated[@]}" down --volumes --remove-orphans >/dev/null 2>&1 || true
}
trap cleanup EXIT
bash "$root/scripts/init-local-secrets.sh"
PILOT107_AGENTD_MODEL_PROFILE=campus-default \
PILOT107_LLM_BASE_URL= \
PILOT107_LLM_API_KEY= \
PILOT107_LLM_MODEL= \
  "${isolated[@]}" up -d --no-deps pilot-agentd pilot107-api pilot107-worker >/dev/null

api_id="$("${isolated[@]}" ps -q pilot107-api)"
agentd_id="$("${isolated[@]}" ps -q pilot-agentd)"
for _attempt in $(seq 1 60); do
  api_health="$(docker inspect --format '{{.State.Health.Status}}' "$api_id")"
  agentd_health="$(docker inspect --format '{{.State.Health.Status}}' "$agentd_id")"
  if [[ "$api_health" == "healthy" && "$agentd_health" == "healthy" ]]; then
    break
  fi
  sleep 1
done
if [[ "$(docker inspect --format '{{.State.Health.Status}}' "$api_id")" != "healthy" ]]; then
  "${isolated[@]}" logs pilot107-api pilot107-worker pilot-agentd >&2
  echo "lifecycle boundary stack readiness timed out" >&2
  exit 1
fi
network="$(docker inspect --format '{{range $name, $_ := .NetworkSettings.Networks}}{{$name}}{{end}}' "$api_id")"
data_volume="$(docker inspect --format '{{range .Mounts}}{{if eq .Destination "/var/lib/pilot107"}}{{.Name}}{{end}}{{end}}' "$api_id")"
if [[ -z "$network" || -z "$data_volume" ]]; then
  echo "lifecycle boundary stack network or data volume is missing" >&2
  exit 1
fi

docker run --rm \
  --network "$network" \
  -v "$data_volume:/var/lib/pilot107" \
  -v "$root:/workspace:ro" \
  --read-only \
  --cap-drop ALL \
  --security-opt no-new-privileges:true \
  --user 10700:10700 \
  -e PYTHONDONTWRITEBYTECODE=1 \
  -e PYTHONPATH=/workspace/src:/workspace/scripts \
  -e PILOT107_DB_PATH=/var/lib/pilot107/pilot107.db \
  -e PILOT107_EVIDENCE_ROOT=/var/lib/pilot107/evidence \
  -w /workspace \
  "${PILOT107_API_IMAGE:-pilot107/api:local}" \
  python3 scripts/smoke_agent_lifecycle_boundaries.py live-model-unavailable
