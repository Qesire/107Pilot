#!/usr/bin/env bash
set -euo pipefail

# Exercise the live Web BFF, API and Worker against a disposable local
# PostgreSQL instance. Restore the regular SQLite-backed simulator afterward
# so a stopped temporary database cannot leave the app containers restarting.

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
compose_dir="$root/simulator/compose"
network_name="${PILOT107_SIM_NETWORK:-pilot107-sim_sim}"
gateway="$(docker network inspect "$network_name" --format '{{range .IPAM.Config}}{{.Gateway}}{{end}}')"

if [[ ! "$gateway" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]; then
  echo "could not determine an IPv4 gateway for $network_name" >&2
  exit 1
fi

restore_sqlite_apps() {
  env -u PILOT107_POSTGRES_DSN \
    docker compose --env-file "$compose_dir/.env.example" -f "$compose_dir/compose.yml" \
    up -d --force-recreate pilot107-api pilot107-worker pilot107-web >/dev/null
}
trap restore_sqlite_apps EXIT

PILOT107_TEST_POSTGRES_MODE=local \
PILOT107_TEST_POSTGRES_HOST="$gateway" \
PILOT107_TEST_POSTGRES_EXPORT_AS_APP_DSN=1 \
  bash "$root/scripts/smoke-postgres-domain-migration.sh" -- \
  bash "$root/scripts/smoke-sim-web-interactions.sh"
