#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root/simulator/compose"

compose=(docker compose --env-file .env.example -f compose.yml)
profile_file="$root/config/platform_profiles/simulator-real107-behavior.yaml"

bash "$root/scripts/apply-sim-real107-profile.sh"

"${compose[@]}" exec -T login-node-sim sinfo -o "%P|%N|%T|%G"
"${compose[@]}" exec -T login-node-sim sacctmgr -n list qos format=Name%40 | grep -q 'qos_stu_medium_2gpu'

expect_submit_rejected() {
  local user="$1"
  local workdir="$2"
  shift 2
  local output
  local rc

  set +e
  output="$(
    "${compose[@]}" exec -T --user "$user" --workdir "$workdir" login-node-sim \
      sbatch --parsable "$@" --wrap 'hostname' 2>&1
  )"
  rc=$?
  set -e

  if [[ "$rc" -eq 0 ]]; then
    echo "submit unexpectedly succeeded for $user: $output" >&2
    echo "profile: $profile_file" >&2
    exit 1
  fi
}

submit_and_expect_completed() {
  local user="$1"
  local workdir="$2"
  local partition="$3"
  local qos="$4"
  shift 4
  local job_id
  local row

  job_id="$(
    "${compose[@]}" exec -T --user "$user" --workdir "$workdir" login-node-sim \
      sbatch --parsable \
        --partition "$partition" \
        --qos "$qos" \
        "$@" \
        --wrap 'hostname; echo real107-profile-ok'
  )"
  job_id="${job_id%%;*}"

  for _ in {1..20}; do
    row="$(
      "${compose[@]}" exec -T login-node-sim \
        sacct -nP -j "$job_id" -X -o JobIDRaw,User,Partition,QOS,State,ExitCode | head -n 1
    )"
    if [[ "$row" == "$job_id|$user|$partition|$qos|COMPLETED|0:0" ]]; then
      echo "$job_id"
      return 0
    fi
    sleep 1
  done

  echo "unexpected real107 profile accounting row: $row" >&2
  echo "profile: $profile_file" >&2
  exit 1
}

alice_assoc="$("${compose[@]}" exec -T login-node-sim sacctmgr -nP show assoc where user=alice format=User,Account,QOS,DefaultQOS)"
bob_assoc="$("${compose[@]}" exec -T login-node-sim sacctmgr -nP show assoc where user=bob format=User,Account,QOS,DefaultQOS)"
if [[ "$alice_assoc" != *"qos_stu_medium_2gpu"* ]]; then
  echo "alice association missing qos_stu_medium_2gpu: $alice_assoc" >&2
  exit 1
fi
if [[ "$bob_assoc" != *"qos_stu_default"* || "$bob_assoc" == *"qos_stu_medium_2gpu"* ]]; then
  echo "bob limited association unexpected: $bob_assoc" >&2
  exit 1
fi

# Behavior matrix from config/platform_profiles/simulator-real107-behavior.yaml.
expect_submit_rejected alice /public/home/alice --partition Students --qos definitely_not_allowed
expect_submit_rejected bob /public/home/bob --partition Students --qos qos_stu_medium_2gpu
expect_submit_rejected alice /public/home/alice --partition P107-A100 --qos qos_p107-a100
expect_submit_rejected alice /home/scc/alice --partition Students --qos qos_stu_large --gres gpu:A100:5

bob_job_id="$(
  submit_and_expect_completed \
    bob \
    /public/home/bob \
    Students \
    qos_stu_default \
    --cpus-per-task 1 \
    --time 00:05:00
)"

alice_job_id="$(
  submit_and_expect_completed \
    alice \
    /public/home/alice \
    Students \
    qos_stu_medium_2gpu \
    --gres gpu:A100:1 \
    --cpus-per-task 1 \
    --time 00:05:00
)"

alice_large_job_id="$(
  submit_and_expect_completed \
    alice \
    /home/scc/alice \
    Students \
    qos_stu_large \
    --gres gpu:A100:4 \
    --cpus-per-task 1 \
    --time 00:05:00
)"

echo "sim real107 profile smoke alice=${alice_job_id} alice_large=${alice_large_job_id} bob=${bob_job_id} oversized_gpu=rejected ok"
