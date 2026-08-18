#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
project="pilot107-a1-fault-$$"
compose_file="$root/simulator/compose/compose.yml"
compose=(docker compose -p "$project" -f "$compose_file" --profile apps)

cd "$root"

# Deterministic D0 barriers assert the crash-window invariants before the live
# process restart matrix: one Turn, contiguous events, invocation idempotency,
# fencing, and terminal-or-recoverable state.
PYTHONPATH=src uv run --extra dev pytest -q \
  tests/test_pilot_agent_a1_vertical.py \
  tests/test_agent_session_service.py::test_submit_persists_turn_before_idempotent_outbox_enqueue \
  tests/agent/test_tool_gateway.py::test_gateway_rejects_stale_fence_without_reader_access

cleanup() {
  "${compose[@]}" down --volumes --remove-orphans >/dev/null 2>&1 || true
}
trap cleanup EXIT

export PILOT107_A1_PROJECT="$project"
export PILOT107_A1_KEEP_STACK=1
export PILOT107_A1_API_PORT="$((40000 + $$ % 10000))"

# api_after_turn_commit: the durable Turn/state file exists before API restart.
bash scripts/smoke-pilot-agent-a1.sh --submit-only
api_id="$("${compose[@]}" ps -q pilot107-api)"
worker_id="$("${compose[@]}" ps -q pilot107-worker)"
agentd_id="$("${compose[@]}" ps -q pilot-agentd)"
if [[ -z "$api_id" || -z "$worker_id" || -z "$agentd_id" ]]; then
  echo "pilot Agent A1 fault matrix failed: target container is missing" >&2
  exit 1
fi
docker stop "$api_id"
docker start "$api_id"
bash scripts/smoke-pilot-agent-a1.sh --verify-existing

# worker_after_outbox_claim: D0 injects the exact claim barrier above; this live
# restart proves the same persisted Turn remains observable across the process.
docker stop "$worker_id"
docker start "$worker_id"
bash scripts/smoke-pilot-agent-a1.sh --verify-existing

# agentd_after_tool_result: D0 interrupts immediately after one persisted tool
# result; restart the real shared Agentd and verify the durable terminal view.
docker stop "$agentd_id"
docker start "$agentd_id"
bash scripts/smoke-pilot-agent-a1.sh --verify-existing

# browser_after_event_n: every verification performs a bounded first page and
# reconnects after its last durable event ID, rejecting gaps and duplicates.
bash scripts/smoke-pilot-agent-a1.sh --verify-existing

echo "pilot Agent A1 deterministic fault and restart matrix passed"
