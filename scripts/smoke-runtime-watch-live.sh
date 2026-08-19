#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

# The scheduler fixture exercises real bounded file reads, persisted restart
# recovery, terminal drain, alert detection, and the 100-watch fairness gate.
uv run pytest \
  tests/runtime_watch/test_reader.py \
  tests/runtime_watch/test_scheduler.py \
  tests/test_runtime_watch_api.py \
  -q

npm test -- --run RuntimeWatchPanel
