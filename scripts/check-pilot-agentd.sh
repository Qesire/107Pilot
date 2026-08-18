#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
node_image="node:22.19.0-bookworm-slim@sha256:cff78eb5aa1cf27dc2b6aeea9d31366415a43e9a9ea0ddec00d780b2b66fad0f"
cd "$root"

docker run --rm --user "$(id -u):$(id -g)" \
  -v "$root:/workspace" \
  -w /workspace/services/pilot-agentd \
  "$node_image" \
  npm ci --ignore-scripts

docker run --rm --user "$(id -u):$(id -g)" \
  -v "$root:/workspace" \
  -w /workspace/services/pilot-agentd \
  "$node_image" \
  npm run check

PYTHONPATH=src uv run --extra dev pytest \
  tests/agent \
  tests/test_agent.py \
  tests/core/test_agent_suggest.py \
  tests/test_remediation_llm.py \
  tests/test_agentd_compose.py \
  tests/test_architecture_boundaries.py \
  tests/test_pilot_agentd_vertical.py \
  tests/test_pilot_agent_a1_vertical.py \
  -q

sh simulator/compose/scripts/check-compose-config.sh

echo "pilot-agentd checks passed"
