#!/usr/bin/env bash
set -euo pipefail

job_id="$(
  docker exec pilot107-sim-login-node-sim-1 bash -lc "
    cd /public/home/alice
    printf '#!/bin/bash\nhostname\nsleep 1\necho done\n' > sleep.sbatch
    chown alice:alice sleep.sbatch
    gosu alice sbatch --parsable \
      --partition Students \
      --qos qos_stu_medium_2gpu \
      --gres gpu:A100:1 \
      --cpus-per-task 1 \
      --time 00:05:00 \
      sleep.sbatch
  "
)"

for _ in {1..10}; do
  row="$(
    docker exec pilot107-sim-login-node-sim-1 \
      sacct -nP -j "$job_id" -X -o JobIDRaw,User,State,ExitCode | head -n 1
  )"
  if [[ "$row" == "${job_id}|alice|COMPLETED|0:0" ]]; then
    break
  fi
  sleep 1
done

docker exec pilot107-sim-login-node-sim-1 sacct -j "$job_id" -X -o JobIDRaw,User,State,ExitCode
docker exec pilot107-sim-login-node-sim-1 cat "/public/home/alice/slurm-${job_id}.out"

if [[ "$row" != "${job_id}|alice|COMPLETED|0:0" ]]; then
  echo "unexpected accounting row: $row" >&2
  exit 1
fi

echo "smoke job ${job_id} ok"
