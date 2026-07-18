#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
compose_file="$root/simulator/compose/compose.postgres-control-test.yml"
test_port="${PILOT107_TEST_POSTGRES_PORT:-55432}"

cleanup() {
  docker compose -f "$compose_file" down --volumes --remove-orphans >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker compose -f "$compose_file" up -d --wait postgres-control-test

export PILOT107_TEST_POSTGRES_DSN="postgresql://pilot107_test:pilot107-test-only@127.0.0.1:${test_port}/pilot107_control_test"
export PILOT107_TEST_POSTGRES_ALLOW_RESET=1
uv run --all-extras pytest -q tests/test_control_repository.py
