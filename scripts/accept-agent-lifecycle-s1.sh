#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ "${1:-}" == "--manifest" ]]; then
  exec python3 "$root/scripts/agent_lifecycle_acceptance.py" manifest s1
fi
exec python3 "$root/scripts/agent_lifecycle_acceptance.py" s1
