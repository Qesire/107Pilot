#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

host="${PILOT107_WEB_HOST:-127.0.0.1}"
port="${PILOT107_WEB_PORT:-3000}"
api_base_url="${PILOT107_WEB_API_BASE_URL:-http://127.0.0.1:8070}"
demo_user="${PILOT107_WEB_DEMO_USER:-alice}"

PYTHONPATH="$root/src" python3 -m pilot107.web.server \
  --host "$host" \
  --port "$port" \
  --api-base-url "$api_base_url" \
  --demo-user "$demo_user"
