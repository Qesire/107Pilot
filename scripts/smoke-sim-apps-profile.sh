#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
compose_dir="$root/simulator/compose"
compose=(
  docker compose
  --env-file "$compose_dir/.env.example"
  -f "$compose_dir/compose.yml"
)

PILOT107_API_BACKEND=demo PILOT107_WORKER_BACKEND=demo "${compose[@]}" up -d pilot107-api pilot107-worker pilot107-web

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

PILOT107_API_BACKEND=demo PILOT107_WORKER_BACKEND=demo "${compose[@]}" exec -T pilot107-api python3 -c \
  "import json, urllib.request; print(json.loads(urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2).read())['status'])"
PILOT107_API_BACKEND=demo PILOT107_WORKER_BACKEND=demo "${compose[@]}" exec -T pilot107-worker test -s /var/lib/pilot107/worker-health.json
PILOT107_API_BACKEND=demo PILOT107_WORKER_BACKEND=demo "${compose[@]}" exec -T pilot107-web python3 - <<'PY'
import json
import urllib.request

assert urllib.request.urlopen("http://127.0.0.1:3000/", timeout=2).status == 200
recipes = json.loads(urllib.request.urlopen("http://127.0.0.1:3000/api/v1/recipes", timeout=5).read())
assert recipes["items"][0]["recipe_id"] == "recipe_python_cpu", recipes
PY

echo "apps profile smoke ok"
