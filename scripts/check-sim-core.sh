#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root/simulator/compose"

docker compose --env-file .env.example -f compose.yml ps
docker compose --env-file .env.example -f compose.yml exec -T login-node-sim sinfo
docker compose --env-file .env.example -f compose.yml exec -T login-node-sim scontrol ping
