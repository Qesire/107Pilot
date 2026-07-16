#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHONPATH="$root/src" uv run --extra dev python "$root/scripts/probe_real107_rest_readonly.py"
