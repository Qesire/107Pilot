#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
compose_dir="$root/simulator/compose"
env_file="${PILOT107_CPU_RC_ENV_FILE:-$compose_dir/.env.cpu-rc}"
project_name="${PILOT107_CPU_RC_PROJECT_NAME:-pilot107-cpu-rc}"
cluster_name="pilot107-cpu-rc"
compose=(
  docker compose
  --project-name "$project_name"
  --env-file "$env_file"
  -f "$compose_dir/compose.yml"
  -f "$compose_dir/compose.competition.yml"
  -f "$compose_dir/compose.cpu-rc.yml"
  --profile competition
)

for _ in $(seq 1 60); do
  if "${compose[@]}" exec -T slurmdbd sacctmgr -n list cluster >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

run_sacctmgr() {
  "${compose[@]}" exec -T slurmdbd sacctmgr -i "$@" >/dev/null
}

run_sacctmgr add cluster "$cluster_name" || true
run_sacctmgr add account competition Cluster="$cluster_name" Description=Competition Organization=pilot107 || true
run_sacctmgr add qos qos_cpu_rc || true
run_sacctmgr modify qos qos_cpu_rc set MaxWall=04:00:00 MaxTRESPerJob=cpu=4,mem=6G GrpTRES=cpu=4,mem=6G || true
run_sacctmgr add user alice Account=competition Cluster="$cluster_name" || true
run_sacctmgr modify user alice set DefaultAccount=competition || true
run_sacctmgr modify user where user=alice account=competition cluster="$cluster_name" set QOS=qos_cpu_rc || true
run_sacctmgr modify user where user=alice account=competition cluster="$cluster_name" set DefaultQOS=qos_cpu_rc || true

for _ in $(seq 1 60); do
  if "${compose[@]}" exec -T slurmctld scontrol ping >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
"${compose[@]}" exec -T slurmctld scontrol reconfigure >/dev/null
profile="$("${compose[@]}" exec -T slurmdbd sacctmgr -nP show assoc where user=alice format=User,Account,QOS,DefaultQOS)"
if [[ "$profile" != *"competition"* || "$profile" != *"qos_cpu_rc"* ]]; then
  echo "CPU RC association initialization failed: $profile" >&2
  exit 1
fi
echo "CPU RC Slurm profile initialized: $profile"
