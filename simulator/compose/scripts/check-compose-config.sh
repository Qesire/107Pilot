#!/usr/bin/env sh
set -eu

cd "$(dirname "$0")/.."

docker compose --env-file .env.example -f compose.yml config >/dev/null
docker compose \
  --env-file .env.competition.example \
  -f compose.yml \
  -f compose.competition.yml \
  --profile competition \
  config >/dev/null
docker compose \
  --env-file .env.competition.example \
  -f compose.yml \
  -f compose.competition.yml \
  -f compose.competition-slurm-host.yml \
  --profile competition \
  config >/dev/null
docker compose \
  --env-file .env.competition.example \
  -f compose.competition-app-node.yml \
  config >/dev/null

echo "base, competition, slurm-host, and app-node compose configs ok"
