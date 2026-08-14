#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$root/src" \
  uv run --extra dev python "$root/scripts/smoke-campus-llm.py"
