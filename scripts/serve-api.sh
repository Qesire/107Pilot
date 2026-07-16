#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

host="${PILOT107_API_HOST:-127.0.0.1}"
port="${PILOT107_API_PORT:-8070}"
db_path="${PILOT107_DB_PATH:-$root/data/phase0/pilot107.db}"
evidence_root="${PILOT107_EVIDENCE_ROOT:-$root/data/phase0/evidence}"
auth_required="${PILOT107_AUTH_REQUIRED:-0}"
trusted_user_header="${PILOT107_TRUSTED_USER_HEADER:-X-Pilot107-User}"
api_backend="${PILOT107_API_BACKEND:-none}"
allowed_roots="${PILOT107_ALLOWED_ROOTS:-/public/home/alice}"

auth_args=()
if [[ "$auth_required" == "1" || "$auth_required" == "true" ]]; then
  auth_args+=(--auth-required)
fi

PYTHONPATH="$root/src" python3 -m pilot107.api.dev_server \
  --db-path "$db_path" \
  --evidence-root "$evidence_root" \
  --host "$host" \
  --port "$port" \
  --backend "$api_backend" \
  --allowed-roots "$allowed_roots" \
  --trusted-user-header "$trusted_user_header" \
  "${auth_args[@]}"
