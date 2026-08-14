#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
api_image="${PILOT107_API_IMAGE:-pilot107/api:local}"
worker_image="${PILOT107_WORKER_IMAGE:-pilot107/worker:local}"
web_image="${PILOT107_WEB_IMAGE:-pilot107/web:local}"
agentd_image="${PILOT107_AGENTD_IMAGE:-pilot107/agentd:local}"

docker build \
  -t "$api_image" \
  -t "$worker_image" \
  -t "$web_image" \
  -f "$root/apps/Dockerfile" \
  "$root"

docker build \
  -t "$agentd_image" \
  -f "$root/services/pilot-agentd/Dockerfile" \
  "$root"
