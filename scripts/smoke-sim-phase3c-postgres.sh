#!/usr/bin/env bash
set -euo pipefail

# Run the live Docker Slurm Phase 3C workflow against an explicitly disposable
# PostgreSQL instance. Local mode creates a UTF-8 cluster below /tmp and
# cleans it up afterward; callers with the Docker image may set mode=docker.

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PILOT107_TEST_POSTGRES_MODE="${PILOT107_TEST_POSTGRES_MODE:-local}"

exec bash "$root/scripts/smoke-postgres-domain-migration.sh" -- \
  env PYTHONPATH=src uv run --all-extras python scripts/smoke_sim_phase3c.py
