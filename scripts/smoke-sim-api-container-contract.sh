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
contract = {
    "recipe_version_id": "recipe_python_cpu@1.0.0",
    "project": {"workdir": "/public/home/alice"},
    "entry": {"command": "echo contract-container-ok"},
    "resources": {
        "partition": "Students",
        "qos": "qos_stu_medium_2gpu",
        "nodes": 1,
        "ntasks": 1,
        "cpus_per_task": 1,
        "time_limit": "00:05:00",
    },
}

def get(path):
    request = urllib.request.Request(url=f"{base_url}{path}", headers={"X-Pilot107-User": "alice"})
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))

def post(path, body):
    request = urllib.request.Request(
        url=f"{base_url}{path}",
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))

recipes = get("/recipes")
assert recipes["items"][0]["recipe_id"] == "recipe_python_cpu", recipes
validated = post("/contracts/validate", contract)
assert validated["status"] == "OK", validated
created = post("/contracts", contract)
prepared = post("/runs/prepare", {"contract_id": created["contract_id"]})
submitted = post(f"/runs/{prepared['run_id']}/submit", {})
assert prepared["state"] == "VALIDATED", prepared
assert "contract-container-ok" in prepared["preview"]["submitted_script"], prepared
assert submitted["submit_strategy"] == "in_memory", submitted
print(
    "api container contract smoke "
    f"contract={created['contract_id']} run={submitted['run_id']} job={submitted['job_id']}"
)
PY

echo "api container contract smoke ok"
