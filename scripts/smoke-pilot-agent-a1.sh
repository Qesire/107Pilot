#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
compose_file="$root/simulator/compose/compose.yml"
project="${PILOT107_A1_PROJECT:-pilot107-a1-smoke-$$}"
api_port="${PILOT107_A1_API_PORT:-$((20000 + $$ % 20000))}"
python_image="${PILOT107_API_IMAGE:-pilot107/api:local}"
compose=(docker compose -p "$project" -f "$compose_file" --profile apps)

cleanup() {
  if [[ "${PILOT107_A1_KEEP_STACK:-0}" != "1" ]]; then
    "${compose[@]}" down --volumes --remove-orphans >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

bash "$root/scripts/init-local-secrets.sh"

PILOT107_API_PORT="127.0.0.1:${api_port}" \
PILOT107_AGENTD_MODEL_PROFILE=faux-default \
PILOT107_AGENTD_FAUX_SCENARIO=a1-smoke \
PILOT107_AGENTD_TOKEN=a1-smoke-agentd-token \
  "${compose[@]}" up -d --no-deps pilot-agentd pilot107-api pilot107-worker

api_id="$("${compose[@]}" ps -q pilot107-api)"
agentd_id="$("${compose[@]}" ps -q pilot-agentd)"
if [[ -z "$api_id" || -z "$agentd_id" ]]; then
  echo "pilot Agent A1 smoke failed: API or Agentd container is missing" >&2
  exit 1
fi

for _attempt in $(seq 1 60); do
  api_health="$(docker inspect --format '{{.State.Health.Status}}' "$api_id")"
  agentd_health="$(docker inspect --format '{{.State.Health.Status}}' "$agentd_id")"
  if [[ "$api_health" == "healthy" && "$agentd_health" == "healthy" ]]; then
    break
  fi
  sleep 1
done
if [[ "$(docker inspect --format '{{.State.Health.Status}}' "$api_id")" != "healthy" ]]; then
  "${compose[@]}" logs pilot107-api pilot107-worker pilot-agentd >&2
  echo "pilot Agent A1 smoke failed: readiness timeout" >&2
  exit 1
fi

network="$(docker inspect --format '{{range $name, $_ := .NetworkSettings.Networks}}{{$name}}{{end}}' "$api_id")"
if [[ -z "$network" ]]; then
  echo "pilot Agent A1 smoke failed: private app network was not found" >&2
  exit 1
fi
data_volume="$(docker inspect --format '{{range .Mounts}}{{if eq .Destination "/var/lib/pilot107"}}{{.Name}}{{end}}{{end}}' "$api_id")"
proxy_secret="$root/simulator/compose/secrets/proxy-hmac.local"
if [[ -z "$data_volume" || ! -f "$proxy_secret" ]]; then
  echo "pilot Agent A1 smoke failed: data volume or proxy secret is missing" >&2
  exit 1
fi
agentd_pid="$(docker inspect --format '{{.State.Pid}}' "$agentd_id")"
agentd_rss_before="$(awk '/^VmRSS:/{print $2}' "/proc/$agentd_pid/status")"

docker run --rm \
  --network "$network" \
  -v "$data_volume:/var/lib/pilot107" \
  -v "$proxy_secret:/run/secrets/pilot107-proxy-hmac:ro" \
  --read-only \
  --cap-drop ALL \
  --security-opt no-new-privileges:true \
  --user 10700:10700 \
  --group-add "$(stat -c '%g' "$proxy_secret")" \
  -e PYTHONDONTWRITEBYTECODE=1 \
  -e PYTHONPATH=/workspace/src \
  -e PILOT107_A1_BASE_URL=http://pilot107-api:8080 \
  -e PILOT107_DB_PATH=/var/lib/pilot107/pilot107.db \
  -e PILOT107_EVIDENCE_ROOT=/var/lib/pilot107/evidence \
  -e PILOT107_PROXY_HMAC_SECRET_FILE=/run/secrets/pilot107-proxy-hmac \
  -v "$root:/workspace:ro" \
  -w /workspace \
  "$python_image" \
  python3 scripts/smoke-pilot-agent-a1.py "$@"

# Report the shared Agentd process baseline without imposing a production
# capacity threshold on the deterministic local D1 fixture.
agentd_pid="$(docker inspect --format '{{.State.Pid}}' "$agentd_id")"
agentd_rss_after="$(awk '/^VmRSS:/{print $2}' "/proc/$agentd_pid/status")"
agentd_stats="$(docker stats --no-stream --format 'cpu={{.CPUPerc}} memory={{.MemUsage}}' "$agentd_id")"
printf 'pilot Agent A1 Agentd baseline: %s rss_before_kib=%s rss_after_kib=%s rss_delta_kib=%s\n' \
  "$agentd_stats" \
  "$agentd_rss_before" \
  "$agentd_rss_after" \
  "$((agentd_rss_after - agentd_rss_before))"

if [[ "${PILOT107_A1_KEEP_STACK:-0}" == "1" ]]; then
  printf 'A1 smoke stack retained: project=%s api_port=%s\n' "$project" "$api_port"
fi
