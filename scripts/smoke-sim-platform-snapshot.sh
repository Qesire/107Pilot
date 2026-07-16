#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

bash scripts/check-sim-core.sh
PYTHONPATH=src uv run python scripts/smoke_sim_platform_snapshot.py
