#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

export PILOT107_DB_PATH="${PILOT107_DB_PATH:-$root/data/phase0/pilot107.db}"
export PILOT107_EVIDENCE_ROOT="${PILOT107_EVIDENCE_ROOT:-$root/data/phase0/evidence}"
export PILOT107_WORKER_BACKEND="${PILOT107_WORKER_BACKEND:-docker-compose-command}"
export PILOT107_COMPOSE_FILE="${PILOT107_COMPOSE_FILE:-$root/simulator/compose/compose.yml}"
export PILOT107_COMPOSE_ENV_FILE="${PILOT107_COMPOSE_ENV_FILE:-$root/simulator/compose/.env.example}"
export PILOT107_COMPOSE_WORKDIR="${PILOT107_COMPOSE_WORKDIR:-$root/simulator/compose}"
export PILOT107_COMPOSE_SERVICE="${PILOT107_COMPOSE_SERVICE:-login-node-sim}"
export PILOT107_ALLOWED_ROOTS="${PILOT107_ALLOWED_ROOTS:-/public/home/alice}"
export PILOT107_WORKER_HEALTH_PATH="${PILOT107_WORKER_HEALTH_PATH:-$root/data/phase0/worker-health.json}"

PYTHONPATH="$root/src" python3 -m pilot107.worker.service "$@"
