# Agent Context Continuation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the 64-step hard failure with bounded checkpoint/yield/continuation and keep long Sessions usable through traceable context compaction.

**Architecture:** Persist cumulative budgets and checkpoint lineage in the control plane. Pi uses `transformContext` only to build a bounded next-call view; at 64 steps the current Turn yields after a complete tool boundary and the store atomically creates one continuation Turn and outbox message.

**Tech Stack:** Python Session/Turn stores, SQLite/PostgreSQL, durable outbox, TypeScript pilot-agentd, Pi Agent Core `transformContext`, pytest and Vitest.

## Global Constraints

- Compaction never deletes durable messages, events, receipts, Evidence or checkpoints.
- Security/profile/capability rules are never summarized away.
- 48 steps is a warning/compaction threshold; `>=64` yields rather than fails.
- Cumulative 256 steps, 24 provider calls or 8 no-progress rejections produce `agent_runtime_budget_exhausted`.
- Yield happens only after a complete tool result/checkpoint boundary.
- Old Turn yield, continuation Turn creation, Session active-turn update and outbox enqueue are one idempotent control-plane transaction.
- Continuation obtains a fresh capability and never extends one Turn past capability expiry.
- TDD and SQLite/PostgreSQL parity are mandatory.

---

### Task 1: Add persisted Turn lineage and cumulative budget fields

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
- Produces: yielded Turn state, parent Turn, checkpoint digest, per-Turn/cumulative counters and continuation flag.

- [ ] **Step 1: Write failing store tests**

Create three tests that assert yielding persists a checkpoint without failing the Session, continuation
usage adds to lineage totals, and a stale fence cannot yield or update any counter.

- [ ] **Step 2: Confirm RED on both backends**

- [ ] **Step 3: Add additive schema and fenced counter updates**

- [ ] **Step 4: Confirm GREEN**

```bash
PYTHONPATH=src uv run --extra dev pytest -q \
  tests/agent/test_store.py tests/agent/test_store_contract.py \
  tests/agent/test_postgres_store.py
```

- [ ] **Step 5: Commit Task 1**

```bash
git add src/pilot107/agent/session.py src/pilot107/agent/store.py \
  src/pilot107/agent/postgres_store.py src/pilot107/agent/migrations.py \
  src/pilot107/core/postgres_domain_schema.py tests/agent/test_store.py \
  tests/agent/test_store_contract.py tests/agent/test_postgres_store.py
git commit -m "feat: persist agent turn continuation lineage"
```

### Task 2: Atomically yield and enqueue one continuation

**Files:**
- Modify: `src/pilot107/agent/store.py`
- Modify: `src/pilot107/agent/postgres_store.py`
- Modify: `src/pilot107/services/agent_session_service.py`
- Test: `tests/agent/test_store_contract.py`
- Test: `tests/test_agent_session_service.py`

**Interfaces:**
- Produces:
  `yield_turn_and_enqueue_continuation(turn_id, claim, final_checkpoint, usage_delta, continuation_request_key, continuation_message) -> tuple[yielded_turn, continuation_turn, outbox]`.

- [ ] **Step 1: Write failing atomicity/idempotency tests**

```text
test_yield_creates_one_continuation_and_outbox_in_same_transaction
test_yield_replay_by_parent_turn_returns_existing_continuation
test_crash_injection_never_leaves_yielded_turn_without_outbox
test_stale_parent_fence_cannot_create_continuation
```

- [ ] **Step 2: Confirm RED**

- [ ] **Step 3: Implement the transaction bundle**

If Session/Turn and outbox stores cannot share a transaction, introduce a same-database runtime transaction adapter rather than calling ordinary `submit_message` after setting Session idle.

- [ ] **Step 4: Confirm GREEN**

```bash
PYTHONPATH=src uv run --extra dev pytest -q \
  tests/agent/test_store_contract.py tests/test_agent_session_service.py
```

- [ ] **Step 5: Commit Task 2**

```bash
git add src/pilot107/agent/store.py src/pilot107/agent/postgres_store.py \
  src/pilot107/services/agent_session_service.py tests/agent/test_store_contract.py \
  tests/test_agent_session_service.py
git commit -m "feat: atomically enqueue continuation turns"
```

### Task 3: Extend agentd protocol with non-failure yield

**Files:**
- Modify: `src/pilot107/agent/protocol.py`
- Modify: `services/pilot-agentd/src/protocol.ts`
- Modify: `schemas/agent/v1/turn-event.schema.json`
- Modify: `services/pilot-agentd/src/turn-executor.ts`
- Test: `tests/agent/test_protocol.py`
- Test: `services/pilot-agentd/tests/protocol.test.ts`
- Test: `services/pilot-agentd/tests/turn-executor.test.ts`

**Interfaces:**
- Produces: `turn_yielded` event with checkpoint, usage delta and `continuation_required=true`.

- [ ] **Step 1: Replace old expected-failure test with a new failing yield test**

```typescript
it("yields at 64 completed steps with a checkpoint", async () => {
  const events = await runLoopingTurn({ initialCumulativeSteps: 0 });
  expect(events.at(-1)?.type).toBe("turn_yielded");
  expect(events.at(-1)?.payload.continuation_required).toBe(true);
  expect(events.at(-1)?.payload.checkpoint).toBeDefined();
});
```

Add a separate test proving cumulative 256 steps still fails with `agent_runtime_budget_exhausted`.

- [ ] **Step 2: Confirm RED for the new behavior**

- [ ] **Step 3: Implement high-water and absolute budget semantics**

Do not delete the safety budget. Stop only after the current tool batch has fully committed; emit warning at 48 and yield at `>=64`.

- [ ] **Step 4: Confirm GREEN and typecheck**

```bash
cd services/pilot-agentd
npx vitest run tests/protocol.test.ts tests/turn-executor.test.ts
npm run typecheck
```

- [ ] **Step 5: Commit Task 3**

```bash
git add src/pilot107/agent/protocol.py services/pilot-agentd/src/protocol.ts \
  schemas/agent/v1/turn-event.schema.json services/pilot-agentd/src/turn-executor.ts \
  tests/agent/test_protocol.py services/pilot-agentd/tests/protocol.test.ts \
  services/pilot-agentd/tests/turn-executor.test.ts
git commit -m "feat: yield agent turns at step high water"
```

### Task 4: Build deterministic bounded context compaction

**Files:**
- Create: `services/pilot-agentd/src/context-compaction.ts`
- Modify: `services/pilot-agentd/src/turn-executor.ts`
- Modify: `services/pilot-agentd/src/checkpoint.ts`
- Create: `services/pilot-agentd/tests/context-compaction.test.ts`
- Test: `services/pilot-agentd/tests/checkpoint.test.ts`

**Interfaces:**
- Produces: `compactContext(messages, policy, refs) -> AgentMessage[]` for Pi `transformContext`.

- [ ] **Step 1: Write failing preservation/bounds tests**

```text
preserves current goal and explicit approvals
preserves latest complete tool receipt and unfinished task
preserves workspace revision/digest and failure code
does not copy large source/evidence bodies when a digest ref exists
falls back to bounded raw window when summary generation fails
never mutates durable checkpoint messages
```

- [ ] **Step 2: Confirm RED**

- [ ] **Step 3: Implement deterministic compaction first**

Use typed extraction and bounded text, not an additional unconstrained LLM call. Store a summary digest and source event range in checkpoint metadata. If later model-generated summaries are added, they remain an optional refinement behind the same validation contract.

- [ ] **Step 4: Wire `transformContext` into `new Agent` and emit `context_compaction_failed` on fallback**

- [ ] **Step 5: Confirm GREEN**

```bash
cd services/pilot-agentd
npx vitest run tests/context-compaction.test.ts tests/checkpoint.test.ts tests/turn-executor.test.ts
```

- [ ] **Step 6: Commit Task 4**

```bash
git add services/pilot-agentd/src/context-compaction.ts \
  services/pilot-agentd/src/turn-executor.ts services/pilot-agentd/src/checkpoint.ts \
  services/pilot-agentd/tests/context-compaction.test.ts \
  services/pilot-agentd/tests/checkpoint.test.ts
git commit -m "feat: compact durable agent context"
```

### Task 5: Handle yield in the Python Worker and refresh capability

**Files:**
- Modify: `src/pilot107/worker/agent_turn_worker.py`
- Modify: `src/pilot107/services/agent_session_service.py`
- Modify: `src/pilot107/agent/capabilities.py`
- Test: `tests/test_agent_turn_worker.py`
- Test: `tests/test_agent_session_service.py`
- Test: `tests/agent/test_capabilities.py`

**Interfaces:**
- Consumes: `turn_yielded` and the atomic store method.
- Produces: one continuation dispatch with new Turn lease/fencing/capability.

- [ ] **Step 1: Write failing worker tests**

```text
test_yield_event_creates_one_continuation_and_acks_parent_outbox
test_worker_crash_after_yield_replays_same_continuation
test_continuation_uses_fresh_capability_and_parent_checkpoint
test_heartbeat_loss_during_yield_cannot_write_old_fence
```

- [ ] **Step 2: Confirm RED**

- [ ] **Step 3: Implement yield handling before generic terminal handling**

The parent is yielded, not completed/failed. The continuation message is system-generated, carries no new user authority, and inherits only existing Session/project/task bindings.

- [ ] **Step 4: Confirm GREEN**

```bash
PYTHONPATH=src uv run --extra dev pytest -q \
  tests/test_agent_turn_worker.py tests/test_agent_session_service.py \
  tests/agent/test_capabilities.py
```

- [ ] **Step 5: Commit Task 5**

```bash
git add src/pilot107/worker/agent_turn_worker.py \
  src/pilot107/services/agent_session_service.py src/pilot107/agent/capabilities.py \
  tests/test_agent_turn_worker.py tests/test_agent_session_service.py \
  tests/agent/test_capabilities.py
git commit -m "feat: resume yielded agent turns"
```

### Task 6: Verify long-session behavior and UI semantics

**Files:**
- Modify: `apps/web/src/types.ts`
- Modify: `apps/web/src/AgentSessionPanel.tsx`
- Test: `apps/web/src/AgentSessionPanel.test.tsx`
- Modify: `scripts/smoke-vm-agent-task.py`

- [ ] **Step 1: Write failing UI test showing yielded as continuing, not failed**

- [ ] **Step 2: Confirm RED and implement minimal copy/state mapping**

- [ ] **Step 3: Extend smoke with forced high-water and process restart**

Assert exactly one continuation, no repeated completed invocation, bounded context, new capability, cumulative budget persistence, and terminal completion after continuation.

- [ ] **Step 4: Run completion verification**

```bash
PYTHONPATH=src uv run --extra dev pytest -q \
  tests/agent/test_store_contract.py tests/test_agent_turn_worker.py \
  tests/test_agent_session_service.py tests/agent/test_capabilities.py
cd services/pilot-agentd && npm run check
cd ../../apps/web && npm test -- --run src/AgentSessionPanel.test.tsx
cd ../.. && bash scripts/smoke-vm-agent-task.sh
```

- [ ] **Step 5: Commit Task 6**

```bash
git add apps/web/src/types.ts apps/web/src/AgentSessionPanel.tsx \
  apps/web/src/AgentSessionPanel.test.tsx scripts/smoke-vm-agent-task.py
git commit -m "test: prove bounded agent continuation"
```

## Completion Gate

The plan is complete only when a fresh crash/restart smoke proves: 64 steps yield rather than fail; exactly one continuation is durable; completed side effects are not repeated; context remains bounded and traceable; fresh capability/fencing is used; and cumulative 256/24/8 limits still fail safely.
