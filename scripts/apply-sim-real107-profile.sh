#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root/simulator/compose"

compose=(docker compose --env-file .env.example -f compose.yml)
profile_file="$root/config/platform_profiles/simulator-real107-behavior.yaml"
cluster_name="pilot107-sim"

for _ in {1..60}; do
  if "${compose[@]}" exec -T login-node-sim sacctmgr -n list cluster >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

run_sacctmgr() {
  "${compose[@]}" exec -T login-node-sim sacctmgr -i "$@" >/dev/null
}

query_sacctmgr() {
  "${compose[@]}" exec -T login-node-sim sacctmgr -nP "$@"
}

set_qos_limit() {
  local qos="$1"
  local max_wall="$2"
  local cpu="$3"
  local mem="$4"
  local gpu="$5"

  # The real platform publishes these as MaxTRESPerUser values. Keep a matching
  # per-job limit in the simulator so an oversized one-off request is rejected
  # at submission under DenyOnLimit. GrpTRES is deliberately high: a simulator
  # group limit would otherwise model a different policy and can mask the
  # per-user behavior we want to exercise.
  run_sacctmgr modify qos "$qos" set MaxWall="$max_wall" || true
  run_sacctmgr modify qos "$qos" set MaxTRESPerJob=cpu="$cpu",mem="$mem" || true
  run_sacctmgr modify qos "$qos" set MaxTRESPerUser=cpu="$cpu",mem="$mem" || true
  run_sacctmgr modify qos "$qos" set GrpTRES=cpu=99999,mem=99999G || true
  if [[ "$gpu" != "0" ]]; then
    if has_gpu_tres; then
      run_sacctmgr modify qos "$qos" set MaxTRESPerJob=cpu="$cpu",mem="$mem",gres/gpu="$gpu" || true
      run_sacctmgr modify qos "$qos" set MaxTRESPerUser=cpu="$cpu",mem="$mem",gres/gpu="$gpu" || true
      run_sacctmgr modify qos "$qos" set GrpTRES=cpu=99999,mem=99999G,gres/gpu=99999 || true
    fi
  fi
}

has_gpu_tres() {
  "${compose[@]}" exec -T login-node-sim sacctmgr -n list tres format=Type%20,Name%20 |
    grep -q 'gres.*gpu'
}

require_qos() {
  local qos="$1"
  if ! query_sacctmgr show qos format=Name%40 | grep -Fxq "$qos"; then
    echo "profile apply failed: QoS not present: $qos" >&2
    exit 1
  fi
}

require_assoc_has_qos() {
  local user="$1"
  local qos="$2"
  local row
  row="$(query_sacctmgr show assoc where user="$user" format=User,Account,QOS,DefaultQOS || true)"
  if [[ "$row" != *"$qos"* ]]; then
    echo "profile apply failed: association for $user does not include $qos: $row" >&2
    exit 1
  fi
}

require_assoc_excludes_qos() {
  local user="$1"
  local qos="$2"
  local row
  row="$(query_sacctmgr show assoc where user="$user" format=User,Account,QOS,DefaultQOS || true)"
  if [[ "$row" == *"$qos"* ]]; then
    echo "profile apply failed: association for $user unexpectedly includes $qos: $row" >&2
    exit 1
  fi
}

if [[ ! -r "$profile_file" ]]; then
  echo "missing simulator behavior profile: $profile_file" >&2
  exit 1
fi

# The profile is the single behavior contract. Shell stays deliberately simple:
# values below mirror config/platform_profiles/simulator-real107-behavior.yaml
# and tests parse the YAML to keep this script, slurm.conf, gres.conf, and smoke
# behavior in sync without adding a fragile shell YAML parser.
run_sacctmgr add cluster "$cluster_name" || true
run_sacctmgr add account students Cluster="$cluster_name" Description=Students Organization=pilot107 || true
run_sacctmgr add account competition Cluster="$cluster_name" Description=Competition Organization=pilot107 || true
run_sacctmgr add account legacy Cluster="$cluster_name" Description=Legacy Organization=pilot107 || true

qos_names=(
  normal
  qos_cpu-6530
  qos_cpu-8358p
  qos_gpu-rtx5090
  qos_gpu-a100
  qos_p107-rtx5090
  qos_p107-a100
  qos_stu001
  qos_stu_default
  qos_stu_small
  qos_stu_medium
  qos_stu_medium_2gpu
  qos_stu_large
  qos_stu_long
  qos_stu_cpu_long
)

for qos in "${qos_names[@]}"; do
  run_sacctmgr add qos "$qos" || true
done

set_qos_limit qos_stu_default 04:00:00 4 16G 1
set_qos_limit qos_stu_small 08:00:00 8 32G 1
set_qos_limit qos_stu_medium 1-00:00:00 16 64G 1
set_qos_limit qos_stu_medium_2gpu 1-00:00:00 24 128G 2
set_qos_limit qos_stu_large 12:00:00 48 240G 4
set_qos_limit qos_stu_long 3-00:00:00 16 64G 1
set_qos_limit qos_stu_cpu_long 3-00:00:00 32 128G 0
set_qos_limit qos_p107-rtx5090 4-00:00:00 16 64G 4
set_qos_limit qos_p107-a100 4-00:00:00 16 64G 4

# The observed student QoS rows use DenyOnLimit, so oversized requests fail at
# submission rather than silently queuing forever. The simulator intentionally
# leaves competition QoS without that flag because the real rows do too.
for qos in \
  qos_stu_default \
  qos_stu_small \
  qos_stu_medium \
  qos_stu_medium_2gpu \
  qos_stu_large \
  qos_stu_long \
  qos_stu_cpu_long; do
  run_sacctmgr modify qos "$qos" set Flags=DenyOnLimit || true
done

if has_gpu_tres; then
  run_sacctmgr modify qos qos_stu_medium_2gpu set MaxJobsPU=4 MaxSubmitJobsPU=10 || true
  run_sacctmgr modify qos qos_stu_large set MaxJobsPU=4 MaxSubmitJobsPU=10 || true
  run_sacctmgr modify qos qos_p107-rtx5090 set MaxJobsPU=4 MaxSubmitJobsPU=10 || true
  run_sacctmgr modify qos qos_p107-a100 set MaxJobsPU=4 MaxSubmitJobsPU=10 || true
else
  run_sacctmgr modify qos qos_stu_medium_2gpu set MaxJobsPU=4 MaxSubmitJobsPU=10 || true
  run_sacctmgr modify qos qos_stu_large set MaxJobsPU=4 MaxSubmitJobsPU=10 || true
  run_sacctmgr modify qos qos_p107-rtx5090 set MaxJobsPU=4 MaxSubmitJobsPU=10 || true
  run_sacctmgr modify qos qos_p107-a100 set MaxJobsPU=4 MaxSubmitJobsPU=10 || true
fi

run_sacctmgr add user alice Account=students Cluster="$cluster_name" || true
run_sacctmgr modify user alice set DefaultAccount=students || true
run_sacctmgr modify user where user=alice account=students cluster="$cluster_name" set QOS=normal,qos_stu001,qos_stu_default,qos_stu_small,qos_stu_medium,qos_stu_medium_2gpu,qos_stu_large,qos_stu_long,qos_stu_cpu_long || true
run_sacctmgr modify user where user=alice account=students cluster="$cluster_name" set DefaultQOS=qos_stu_medium_2gpu || true

run_sacctmgr add user bob Account=students Cluster="$cluster_name" || true
run_sacctmgr modify user bob set DefaultAccount=students || true
run_sacctmgr modify user where user=bob account=students cluster="$cluster_name" set QOS=normal,qos_stu_default || true
run_sacctmgr modify user where user=bob account=students cluster="$cluster_name" set DefaultQOS=qos_stu_default || true

"${compose[@]}" exec -T login-node-sim scontrol reconfigure >/dev/null
for _ in {1..20}; do
  if "${compose[@]}" exec -T login-node-sim sinfo >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

for qos in "${qos_names[@]}"; do
  require_qos "$qos"
done
require_assoc_has_qos alice qos_stu_medium_2gpu
require_assoc_has_qos bob qos_stu_default
require_assoc_excludes_qos bob qos_stu_medium_2gpu
require_assoc_excludes_qos alice qos_p107-a100

echo "sim real107 profile applied"
echo "source profile: $profile_file"
"${compose[@]}" exec -T login-node-sim sacctmgr -n list qos \
  format=Name%30,MaxWall%16,MaxTRESPerJob%50,GrpTRES%50 |
  sed 's/[[:space:]]\+$//' || true
query_sacctmgr show assoc where user=alice,bob format=User,Account,QOS,DefaultQOS |
  sed 's/[[:space:]]\+$//' || true
