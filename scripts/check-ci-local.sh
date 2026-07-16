#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

uv run --extra dev ruff check src tests scripts
uv run --extra dev mypy src
uv run --extra dev pytest -q

npm run typecheck
npm test -- --run
npm run build

sh simulator/compose/scripts/check-compose-config.sh

echo "local CI checks passed"
