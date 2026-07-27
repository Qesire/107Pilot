#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
compose_file="$root/simulator/compose/compose.postgres-control-test.yml"
test_port="${PILOT107_TEST_POSTGRES_PORT:-55432}"
test_host="${PILOT107_TEST_POSTGRES_HOST:-127.0.0.1}"
mode="${PILOT107_TEST_POSTGRES_MODE:-docker}"
local_postgres_root=""
local_data_dir=""
local_pg_ctl=""

if [[ ! "$test_port" =~ ^[1-9][0-9]{1,4}$ ]]; then
  echo "PILOT107_TEST_POSTGRES_PORT must be a TCP port" >&2
  exit 2
fi
if [[ ! "$test_host" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]; then
  echo "PILOT107_TEST_POSTGRES_HOST must be an IPv4 address" >&2
  exit 2
fi

cleanup() {
  if [[ "$mode" == "docker" ]]; then
    docker compose -f "$compose_file" down --volumes --remove-orphans >/dev/null 2>&1 || true
  elif [[ -n "$local_data_dir" && -n "$local_pg_ctl" ]]; then
    "$local_pg_ctl" -D "$local_data_dir" -m fast -w stop >/dev/null 2>&1 || true
  fi
  if [[ -n "$local_postgres_root" ]]; then
    rm -rf -- "$local_postgres_root"
  fi
}
trap cleanup EXIT

case "$mode" in
  docker)
    docker compose -f "$compose_file" up -d --wait postgres-control-test
    export PILOT107_TEST_POSTGRES_DSN="postgresql://pilot107_test:pilot107-test-only@${test_host}:${test_port}/pilot107_control_test"
    ;;
  local)
    if ! command -v pg_config >/dev/null; then
      echo "local PostgreSQL mode requires pg_config" >&2
      exit 2
    fi
    postgres_bin_dir="$(pg_config --bindir)"
    initdb="$postgres_bin_dir/initdb"
    local_pg_ctl="$postgres_bin_dir/pg_ctl"
    createdb="$postgres_bin_dir/createdb"
    if [[ ! -x "$initdb" || ! -x "$local_pg_ctl" || ! -x "$createdb" ]]; then
      echo "local PostgreSQL mode requires initdb, pg_ctl, and createdb" >&2
      exit 2
    fi
    local_postgres_root="$(mktemp -d "${TMPDIR:-/tmp}/pilot107-postgres-domain.XXXXXX")"
    local_data_dir="$local_postgres_root/data"
    local_socket_dir="$local_postgres_root/socket"
    mkdir "$local_socket_dir"
    "$initdb" --no-locale --encoding=UTF8 --auth-local=trust --auth-host=trust \
      --username=pilot107_test --pgdata="$local_data_dir" >/dev/null
    # Some Docker hosts SNAT gateway traffic through a non-bridge source IP.
    # The server only listens on the explicit test address, and this rule is
    # limited to the disposable smoke role so the containers can connect
    # without guessing that implementation-specific source address.
    printf 'host all pilot107_test 0.0.0.0/0 trust\n' >> "$local_data_dir/pg_hba.conf"
    if ! "$local_pg_ctl" -D "$local_data_dir" \
      -l "$local_postgres_root/postgres.log" \
      -o "-h $test_host -p $test_port -k $local_socket_dir" -w start >/dev/null; then
      echo "local PostgreSQL failed to start:" >&2
      sed -n '1,160p' "$local_postgres_root/postgres.log" >&2 || true
      exit 1
    fi
    "$createdb" -h "$test_host" -p "$test_port" -U pilot107_test pilot107_control_test
    export PILOT107_TEST_POSTGRES_DSN="postgresql://pilot107_test@${test_host}:${test_port}/pilot107_control_test"
    ;;
  *)
    echo "PILOT107_TEST_POSTGRES_MODE must be docker or local" >&2
    exit 2
    ;;
esac

export PILOT107_TEST_POSTGRES_ALLOW_RESET=1
if [[ "${PILOT107_TEST_POSTGRES_EXPORT_AS_APP_DSN:-0}" == "1" ]]; then
  export PILOT107_POSTGRES_DSN="$PILOT107_TEST_POSTGRES_DSN"
fi
cd "$root"
if (($# == 0)); then
  PYTHONPATH=src uv run --all-extras pytest -q tests/test_postgres_domain_stores.py
fi
if (($# > 0)); then
  if [[ "$1" != "--" ]]; then
    echo "pass an optional test command after --" >&2
    exit 2
  fi
  shift
  if (($# == 0)); then
    echo "a test command is required after --" >&2
    exit 2
  fi
  "$@"
fi
