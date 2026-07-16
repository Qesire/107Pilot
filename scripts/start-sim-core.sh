#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root/simulator/compose"

docker compose --env-file .env.example -f compose.yml up -d --force-recreate \
  mariadb \
  slurmdbd \
  slurmctld \
  worker-1 \
  worker-2 \
  login-node-sim \
  slurmrestd

bash "$root/scripts/apply-sim-real107-profile.sh"
