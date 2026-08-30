# Agent Invocation Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Recover safely from crashes between a tool side effect, its terminal receipt, the Turn checkpoint, and outbox acknowledgement without repeating an unknown mutation.

**Architecture:** Separate provider invocation identity from durable domain operation identity. Tool Gateway commits the authoritative terminal invocation receipt first; Agent Turn persistence then commits the corresponding event/checkpoint, while stale reconciliation repairs either gap using fencing and durable receipts.

**Tech Stack:** Python durable stores, SQLite/PostgreSQL, capability gateway, daemon heartbeat threads, TypeScript pilot-agentd, Pi Agent Core, JSON Schema, pytest and Vitest.

## Global Constraints

- `tool_call_id` is audit metadata, never the identity of a mutating domain operation.
- Mutating tools without stable request key, target resource and target revision fail closed.
- Delivery is at-least-once; exactly-once observable effects come from idempotent domain receipts and reconciliation.
- Gateway receipt and Turn checkpoint are two durable commit points, not a distributed transaction.
- Old fencing tokens cannot renew, finish, checkpoint, ack, or retry after reclaim.
- Heartbeats run independently of model events.
- SQLite and PostgreSQL behavior must satisfy the same contract.
- Follow RED → GREEN → refactor for every task.

---

### Task 1: Extend invocation lifecycle and persistence

**Files:**
- Modify: `src/pilot107/agent/session.py`
- Modify: `src/pilot107/agent/store.py`
- Modify: `src/pilot107/agent/postgres_store.py`
- Modify: `src/pilot107/agent/migrations.py`
- Modify: `src/pilot107/core/postgres_domain_schema.py`
- Test: `tests/agent/test_store.py`
- Test: `tests/agent/test_store_contract.py`
- Test: `tests/agent/test_postgres_store.py`

**Interfaces:**
- Produces: `reserve_invocation`, `renew_invocation`, `commit_invocation_receipt`, `mark_invocation_stale`, `claim_invocation_reconciliation`.

- [ ] **Step 1: Write failing store contract tests**

Create four tests with these exact assertions:

```text
test_terminal_receipt_replays_by_operation_key_across_provider_ids
  reserve provider call A, commit one receipt, reserve provider call B with the same operation key,
  assert created=False and the stored receipt/result digest are identical.
test_stale_invocation_fence_cannot_commit_result
  reclaim an expired invocation, finish with the old fence, assert AgentSessionConflict.
test_unknown_mutation_is_not_claimable_for_handler_retry
  mark a mutating invocation unknown, request execution again, assert no executable claim is returned.
test_invocation_heartbeat_prevents_reclaim
  renew before expiry, advance past the original expiry, assert a second worker cannot claim it.
```

Assert states:

```text
reserved → running → completed | failed
running → stale → reconciling → completed | failed | unknown
```

- [ ] **Step 2: Run all three backend suites and confirm RED**

- [ ] **Step 3: Add additive columns/indexes and row mappings**

Add `durable_operation_key`, `causation_root_key`, canonical intent/arguments digest, lease owner/expiry/heartbeat/fence, result digest/ref, side-effect receipt, attempt and reconciliation state. Add a unique operation-key scope independent of `(turn_id, idempotency_key)`; retain the old unique key for legacy replay.

- [ ] **Step 4: Implement fenced transitions and terminal receipt replay**

- [ ] **Step 5: Run store contracts and confirm GREEN**

```bash
PYTHONPATH=src uv run --extra dev pytest -q \
  tests/agent/test_store.py tests/agent/test_store_contract.py \
  tests/agent/test_postgres_store.py
```

- [ ] **Step 6: Commit Task 1**

```bash
git add src/pilot107/agent/session.py src/pilot107/agent/store.py \
  src/pilot107/agent/postgres_store.py src/pilot107/agent/migrations.py \
  src/pilot107/core/postgres_domain_schema.py tests/agent/test_store.py \
  tests/agent/test_store_contract.py tests/agent/test_postgres_store.py
git commit -m "feat: add recoverable agent tool invocations"
```

### Task 2: Define stable operation identity in Python and TypeScript protocols

**Files:**
- Modify: `src/pilot107/agent/protocol.py`
- Modify: `services/pilot-agentd/src/protocol.ts`
- Modify: `services/pilot-agentd/src/tool-gateway.ts`
- Modify: `schemas/agent/v1/checkpoint.schema.json`
- Test: `tests/agent/test_protocol.py`
- Test: `services/pilot-agentd/tests/protocol.test.ts`
- Test: `services/pilot-agentd/tests/tool-gateway.test.ts`

**Interfaces:**
- Produces: `DurableOperationIdentity` and canonical digest parity between Python and TypeScript.

- [ ] **Step 1: Write cross-language fixture tests**

```typescript
it("keeps operation identity stable when provider tool call id changes", () => {
  expect(operationKey({ ...fixture, toolCallId: "a" }))
    .toBe(operationKey({ ...fixture, toolCallId: "b" }));
});
```

Add `test_mutating_tool_without_request_key_or_target_revision_fails_closed`; invoke the canonical
identity builder once without `user_request_key` and once without `target_revision`, and assert both
raise the stable protocol validation error before any HTTP request is sent.

- [ ] **Step 2: Confirm RED in Python and Vitest**

- [ ] **Step 3: Implement canonical identity**

```text
durable_operation_key = sha256(
  owner, session_id, tool_name, canonical_intent_digest,
  target_resource_id, target_revision, user_request_key
)
stage_operation_key = sha256(causation_root_key, stage_name, stage_input_digest)
```

Keep `invocation_id` tied to provider call for audit only.

- [ ] **Step 4: Confirm GREEN and typecheck**

```bash
PYTHONPATH=src uv run --extra dev pytest -q tests/agent/test_protocol.py
cd services/pilot-agentd && npx vitest run tests/protocol.test.ts tests/tool-gateway.test.ts && npm run typecheck
```

- [ ] **Step 5: Commit Task 2**

```bash
git add src/pilot107/agent/protocol.py services/pilot-agentd/src/protocol.ts \
  services/pilot-agentd/src/tool-gateway.ts schemas/agent/v1/checkpoint.schema.json \
  tests/agent/test_protocol.py services/pilot-agentd/tests/protocol.test.ts \
  services/pilot-agentd/tests/tool-gateway.test.ts
git commit -m "feat: decouple tool calls from operation identity"
```

### Task 3: Commit terminal receipts before returning ToolResult

**Files:**
- Modify: `src/pilot107/agent/tool_gateway.py`
- Modify: `src/pilot107/api/agent_tool_routes.py`
- Modify: `services/pilot-agentd/src/tool-gateway.ts`
- Test: `tests/agent/test_tool_gateway.py`
- Test: `tests/test_agent_tool_gateway_api.py`
- Test: `services/pilot-agentd/tests/tool-gateway.test.ts`

**Interfaces:**
- Produces: `ToolResult` containing result digest/ref and side-effect receipt ref.
- Consumes: Task 1 terminal receipt store and Task 2 identity.

- [ ] **Step 1: Write failing crash-window tests**

Add:

```text
test_gateway_replays_terminal_receipt_without_handler_repeat
test_gateway_unknown_mutation_never_reexecutes_handler
test_gateway_reconciles_domain_receipt_after_process_crash
test_result_exposes_digest_and_receipt_ref_without_secret
```

- [ ] **Step 2: Confirm RED**

- [ ] **Step 3: Implement gateway execution wrapper**

The wrapper reserves/claims, maintains invocation heartbeat, invokes the handler once, validates/redacts/bounds its result, atomically commits the terminal receipt, then returns the ToolResult. On stale replay it calls a typed domain reconciler; only handlers declaring `side_effect_free_retry=True` may run again when no receipt exists.

- [ ] **Step 4: Confirm GREEN across Python and TypeScript gateway suites**

- [ ] **Step 5: Commit Task 3**

```bash
git add src/pilot107/agent/tool_gateway.py src/pilot107/api/agent_tool_routes.py \
  services/pilot-agentd/src/tool-gateway.ts tests/agent/test_tool_gateway.py \
  tests/test_agent_tool_gateway_api.py services/pilot-agentd/tests/tool-gateway.test.ts
git commit -m "feat: commit terminal tool receipts"
```

### Task 4: Add reusable Turn and outbox heartbeats

**Files:**
- Create: `src/pilot107/worker/lease_heartbeat.py`
- Modify: `src/pilot107/worker/runtime_worker.py`
- Modify: `src/pilot107/worker/agent_turn_worker.py`
- Modify: `src/pilot107/core/control_repository.py`
- Modify: `src/pilot107/core/postgres_control_repository.py`
- Test: `tests/test_control_repository.py`
- Test: `tests/test_collection_outbox.py`
- Test: `tests/test_agent_turn_worker.py`

**Interfaces:**
- Produces: `LeaseHeartbeat(renew, interval, on_lost)` context manager.
- Replaces: collection-only `_OutboxHeartbeat` without changing its fencing behavior.

- [ ] **Step 1: Write failing long-stream tests**

Create these tests and assert respectively: neither Turn nor outbox can be reclaimed past the original
expiry while heartbeats succeed; heartbeat fencing loss calls agentd cancellation and interrupts the
Turn; and the old outbox fence cannot acknowledge after another worker reclaims it.

- [ ] **Step 2: Confirm RED**

- [ ] **Step 3: Extract generic heartbeat and wire both leases**

Heartbeat runs every `min(10s, lease/3)`, records the first fencing failure, requests agentd cancellation, stops new tool calls, and forces interrupted/retry handling. Never renew only when a Pi event arrives.

- [ ] **Step 4: Confirm GREEN**

```bash
PYTHONPATH=src uv run --extra dev pytest -q \
  tests/test_control_repository.py tests/test_collection_outbox.py \
  tests/test_agent_turn_worker.py
```

- [ ] **Step 5: Commit Task 4**

```bash
git add src/pilot107/worker/lease_heartbeat.py src/pilot107/worker/runtime_worker.py \
  src/pilot107/worker/agent_turn_worker.py src/pilot107/core/control_repository.py \
  src/pilot107/core/postgres_control_repository.py tests/test_control_repository.py \
  tests/test_collection_outbox.py tests/test_agent_turn_worker.py
git commit -m "feat: heartbeat agent runtime leases"
```

### Task 5: Persist checkpoint after each terminal invocation receipt

**Files:**
- Modify: `services/pilot-agentd/src/checkpoint.ts`
- Modify: `services/pilot-agentd/src/events.ts`
- Modify: `services/pilot-agentd/src/turn-executor.ts`
- Modify: `src/pilot107/worker/agent_turn_worker.py`
- Modify: `src/pilot107/agent/store.py`
- Modify: `src/pilot107/agent/postgres_store.py`
- Test: `services/pilot-agentd/tests/checkpoint.test.ts`
- Test: `services/pilot-agentd/tests/turn-executor.test.ts`
- Test: `tests/test_agent_turn_worker.py`

**Interfaces:**
- Produces: checkpoint candidate containing operation/result/receipt references.
- Produces: atomic TurnStore method
  `append_tool_result_and_checkpoint(turn_id, claim, sequence, invocation_receipt, checkpoint)`.

- [ ] **Step 1: Write failing two-commit recovery tests**

```text
test_terminal_invocation_receipt_before_checkpoint_rebuilds_checkpoint
test_checkpoint_commit_before_outbox_ack_is_idempotent
test_provider_id_change_restores_completed_operation_without_handler
test_partial_tool_progress_never_enters_completed_checkpoint
```

- [ ] **Step 2: Confirm RED**

- [ ] **Step 3: Extend checkpoint schema and digest**

Include parent digest, session/turn, completed operation key, arguments/result digest, result/side-effect receipt refs, workspace boundary, active Task refs and usage. Do not embed large artifact bodies.

- [ ] **Step 4: Add atomic Turn event/checkpoint commit and ledger hydration**

If Gateway receipt exists but Turn checkpoint lags, recovery hydrates it from the ledger. If no terminal receipt exists, recovery enters stale reconciliation and never invents completion.

- [ ] **Step 5: Run Python/Vitest suites and confirm GREEN**

```bash
PYTHONPATH=src uv run --extra dev pytest -q tests/test_agent_turn_worker.py tests/agent/test_store.py
cd services/pilot-agentd && npx vitest run tests/checkpoint.test.ts tests/turn-executor.test.ts
```

- [ ] **Step 6: Commit Task 5**

```bash
git add services/pilot-agentd/src/checkpoint.ts services/pilot-agentd/src/events.ts \
  services/pilot-agentd/src/turn-executor.ts src/pilot107/worker/agent_turn_worker.py \
  src/pilot107/agent/store.py src/pilot107/agent/postgres_store.py \
  services/pilot-agentd/tests/checkpoint.test.ts \
  services/pilot-agentd/tests/turn-executor.test.ts tests/test_agent_turn_worker.py
git commit -m "feat: checkpoint terminal tool receipts"
```

### Task 6: Prove stale reconciliation and backend parity

**Files:**
- Modify: `src/pilot107/worker/runtime_worker.py`
- Modify: `tests/test_pilot_agent_a1_vertical.py`
- Modify: `tests/agent/test_postgres_store.py`
- Modify: `services/pilot-agentd/tests/readonly-turn.integration.test.ts`

**Interfaces:**
- Runtime tick reconciles expired invocation leases before dispatching new Turn work.

- [ ] **Step 1: Add failing crash/restart vertical cases at every commit boundary**

- [ ] **Step 2: Confirm each test fails for the intended missing recovery path**

- [ ] **Step 3: Implement minimal stale reconciliation orchestration**

- [ ] **Step 4: Run focused full verification**

```bash
PYTHONPATH=src uv run --extra dev pytest -q \
  tests/agent/test_store.py tests/agent/test_store_contract.py \
  tests/agent/test_postgres_store.py tests/agent/test_tool_gateway.py \
  tests/test_agent_turn_worker.py tests/test_control_repository.py \
  tests/test_pilot_agent_a1_vertical.py
cd services/pilot-agentd && npm run check
```

- [ ] **Step 5: Commit Task 6**

```bash
git add src/pilot107/worker/runtime_worker.py tests/test_pilot_agent_a1_vertical.py \
  tests/agent/test_postgres_store.py \
  services/pilot-agentd/tests/readonly-turn.integration.test.ts
git commit -m "test: prove agent invocation crash recovery"
```

## Completion Gate

Fresh verification must show zero failures for both stores, Gateway, agentd and vertical crash recovery. A passing normal replay test is insufficient unless the suite injects failure after side effect, after terminal receipt, after checkpoint commit, and before outbox ack.
