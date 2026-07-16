#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
compose_dir="$root/simulator/compose"
compose=(
  docker compose
  --env-file "$compose_dir/.env.example"
  -f "$compose_dir/compose.yml"
)

PILOT107_API_BACKEND=in-memory "${compose[@]}" up -d pilot107-api

for _ in $(seq 1 30); do
  container_id="$(PILOT107_API_BACKEND=in-memory "${compose[@]}" ps -q pilot107-api)"
  if [[ -n "$container_id" ]]; then
    status="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$container_id")"
    if [[ "$status" == "healthy" ]]; then
      break
    fi
    if [[ "$status" == "unhealthy" ]]; then
      PILOT107_API_BACKEND=in-memory "${compose[@]}" logs --tail=80 pilot107-api >&2
      exit 1
    fi
  fi
  sleep 2
done

PILOT107_API_BACKEND=in-memory "${compose[@]}" exec -T pilot107-api python3 - <<'PY'
import json
import urllib.request

base_url = "http://127.0.0.1:8080/api/v1"
headers = {"Content-Type": "application/json", "X-Pilot107-User": "alice"}
payload = {
    "workdir": "/public/home/alice",
    "script": "#!/bin/bash\nhostname\n",
    "resource_plan": {
        "partition": "Students",
        "qos": "qos_stu_medium_2gpu",
        "nodes": 1,
        "ntasks": 1,
        "cpus_per_task": 1,
        "time_limit": "00:05:00",
    },
}

def post(path, body):
    request = urllib.request.Request(
        url=f"{base_url}{path}",
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))

prepared = post("/runs/prepare", payload)
submitted = post(f"/runs/{prepared['run_id']}/submit", {})
assert prepared["owner"] == "alice", prepared
assert submitted["submit_strategy"] == "in_memory", submitted
assert submitted["job_id"], submitted
print(f"api container submit smoke {submitted['run_id']} job={submitted['job_id']}")
PY

echo "api container submit smoke ok"
