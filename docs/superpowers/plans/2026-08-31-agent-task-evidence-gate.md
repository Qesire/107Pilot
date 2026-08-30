# AgentTask Evidence Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ensure a scheduling receipt never completes an AgentTask and only finalized, integrity-verified Evidence—plus Capsule when explicitly required—can wake the follow-up Agent Turn.

**Architecture:** Keep the existing asynchronous Builder → AgentTask → Run pipeline. Add an explicit completion policy and immutable Evidence gate receipt, then make the AgentTask finalizer re-read authoritative Run/Evidence/Capsule facts under Task CAS before emitting a ready outbox message.

**Tech Stack:** Python 3.11+, dataclasses/enums, SQLite and PostgreSQL stores, durable outbox, Slurm Run reconciliation, EvidenceBinder, pytest, JSON Schema, React/TypeScript.

## Global Constraints

- Preserve schedule receipt as a non-terminal acknowledgement; do not hold a Pi Turn open while Slurm runs.
- New writes use the new schema; legacy records remain readable and are marked `legacy_gate_unverified` until reconciled.
- `evidence_required` never waits for Capsule; `evidence_and_capsule_required` requires Capsule state `ready`.
- Until the Workspace Live Revision phase lands, the gate receipt carries the existing immutable workspace
  snapshot digest with `workspace_revision=null` and `legacy_boundary=true`; it never invents a live
  revision. Phase 3 makes live revision/digest mandatory for all new Run/Evidence writes.
- Successful completion requires Run `SUCCEEDED`, successful ExitCode, finalized Evidence, and verified integrity.
- Failed/cancelled/orphaned Runs must terminate with explicit outcome; bounded collection exhaustion becomes `evidence_unavailable`, never fabricated Evidence or infinite waiting.
- SQLite and PostgreSQL changes ship together.
- Follow TDD: every production change follows a focused failing test whose failure reason is recorded before implementation.

---

### Task 1: Freeze AgentTask gate domain and wire contract

**Files:**
- Modify: `src/pilot107/agent/tasks.py`
- Modify: `schemas/agent/v2/agent-task.schema.json`
- Modify: `src/pilot107/api/agent_task_routes.py`
- Test: `tests/agent/test_lifecycle_schemas.py`
- Test: `tests/agent/test_task_store_contract.py`

**Interfaces:**
- Produces: `AgentTaskCompletionPolicy`, `AgentTaskGateState`, `AgentTaskGateReceipt`.
- Preserves: legacy `AgentTaskState.SUCCEEDED` on reads; new successful writes use the existing wire-compatible terminal value until the API version is deliberately bumped.

- [ ] **Step 1: Write failing domain and schema tests**

```python
def test_schedule_receipt_is_non_terminal_and_declares_completion_policy():
    receipt = AgentTaskScheduleReceipt(
        task_id="task-1",
        run_id="run-1",
        completion_policy=AgentTaskCompletionPolicy.EVIDENCE_REQUIRED,
        submit_state="admitted",
    )
    assert receipt.is_terminal is False

def test_capsule_policy_is_explicit_not_inferred_from_vm_mode():
    assert AgentTaskCompletionPolicy("evidence_required").requires_capsule is False
    assert AgentTaskCompletionPolicy("evidence_and_capsule_required").requires_capsule is True
```

- [ ] **Step 2: Run the focused tests and confirm RED**

Run:

```bash
PYTHONPATH=src uv run --extra dev pytest -q \
  tests/agent/test_lifecycle_schemas.py \
  tests/agent/test_task_store_contract.py
```

Expected: failures because completion policy and gate receipt do not exist.

- [ ] **Step 3: Add the minimal typed contract**

```python
class AgentTaskCompletionPolicy(StrEnum):
    EVIDENCE_REQUIRED = "evidence_required"
    EVIDENCE_AND_CAPSULE_REQUIRED = "evidence_and_capsule_required"

    @property
    def requires_capsule(self) -> bool:
        return self is self.EVIDENCE_AND_CAPSULE_REQUIRED

@dataclass(frozen=True)
class AgentTaskScheduleReceipt:
    task_id: str
    run_id: str
    completion_policy: AgentTaskCompletionPolicy
    submit_state: str

    @property
    def is_terminal(self) -> bool:
        return False

@dataclass(frozen=True)
class AgentTaskGateReceipt:
    run_id: str
    evidence_refs: tuple[str, ...]
    evidence_digest: str
    integrity_verified_at: str
    workspace_revision: int | None
    workspace_digest: str
    legacy_boundary: bool
    capsule_ref: str | None
    capsule_state: str
```

Add schema fields for `completion_policy`, non-terminal schedule receipt, terminal gate receipt, and `legacy_gate_unverified` without changing credential or capability exposure.

- [ ] **Step 4: Run focused tests and confirm GREEN**

- [ ] **Step 5: Commit only Task 1 files**

```bash
git add src/pilot107/agent/tasks.py schemas/agent/v2/agent-task.schema.json \
  src/pilot107/api/agent_task_routes.py tests/agent/test_lifecycle_schemas.py \
  tests/agent/test_task_store_contract.py
git commit -m "feat: define agent task completion gates"
```

### Task 2: Add additive Task persistence and CAS transitions

**Files:**
- Modify: `src/pilot107/agent/task_store.py`
- Modify: `src/pilot107/agent/postgres_task_store.py`
- Modify: `src/pilot107/agent/migrations.py`
- Modify: `src/pilot107/core/postgres_domain_schema.py`
- Test: `tests/agent/test_task_store_contract.py`
- Create: `tests/agent/test_postgres_task_store.py`

**Interfaces:**
- Produces: `advance_gate(task_id, lease, gate_state, receipt=None)` and `finalize_task(task_id, lease, gate_receipt, result)`.
- Consumes: Task 1 policy and receipt types.

- [ ] **Step 1: Add backend-contract tests that reject premature completion**

```python
def test_store_rejects_success_without_terminal_gate(store, running_task, lease):
    with pytest.raises(AgentTaskConflict):
        store.finalize_task(running_task.task_id, lease=lease, gate_receipt=None,
                            result=AgentTaskResult.succeeded(("run:1",)))

def test_stale_fence_cannot_finalize_gate(store, running_task, stale_lease, receipt):
    with pytest.raises(AgentTaskConflict):
        store.finalize_task(running_task.task_id, lease=stale_lease,
                            gate_receipt=receipt,
                            result=AgentTaskResult.succeeded(receipt.evidence_refs))
```

- [ ] **Step 2: Run SQLite and PostgreSQL contract tests and confirm RED**

- [ ] **Step 3: Add additive migrations**

Add columns for completion policy, gate state, schedule receipt ref, evidence refs/digest, integrity timestamp, capsule ref/state, causation/stage keys, reconciliation attempts, heartbeat, and legacy verification marker. Do not edit checksummed historical migration bodies.

- [ ] **Step 4: Implement CAS-only gate transitions**

`finalize_task` must require current lease owner, version, fencing token, unexpired lease, a verified gate receipt, and policy-compatible Capsule state in the same local store transaction.

- [ ] **Step 5: Run both store suites and confirm GREEN**

Run:

```bash
PYTHONPATH=src uv run --extra dev pytest -q \
  tests/agent/test_task_store_contract.py \
  tests/agent/test_postgres_task_store.py
```

- [ ] **Step 6: Commit Task 2**

```bash
git add src/pilot107/agent/task_store.py src/pilot107/agent/postgres_task_store.py \
  src/pilot107/agent/migrations.py src/pilot107/core/postgres_domain_schema.py \
  tests/agent/test_task_store_contract.py tests/agent/test_postgres_task_store.py
git commit -m "feat: persist agent task evidence gates"
```

### Task 3: Produce an immutable Evidence gate receipt

**Files:**
- Modify: `src/pilot107/core/evidence_binding.py`
- Modify: `src/pilot107/core/run_store.py`
- Modify: `src/pilot107/worker/evidence.py`
- Test: `tests/test_evidence_binding.py`
- Test: `tests/test_evidence.py`
- Test: `tests/test_run_store.py`

**Interfaces:**
- Produces: `EvidenceBinder.verify_terminal_gate(run_id, refs, workspace_boundary) -> AgentTaskGateReceipt`.
- Reuses: registered object scope, collection status, SHA-256, size, finalized timestamp, root containment, and bundle digest checks already enforced by `EvidenceBinder.bind`.

- [ ] **Step 1: Write failing gate tests**

```python
def test_terminal_gate_rejects_unfinalized_evidence(binder, run, evidence_ref):
    with pytest.raises(EvidenceBindingError):
        binder.verify_terminal_gate(run.run_id, (evidence_ref,), run.workspace_boundary)

def test_terminal_gate_binds_run_workspace_and_manifest_digest(binder, ready_run):
    receipt = binder.verify_terminal_gate(
        ready_run.run_id, ready_run.evidence_refs, ready_run.workspace_boundary
    )
    assert receipt.evidence_digest.startswith("sha256:")
    assert receipt.workspace_digest == ready_run.workspace_boundary.live_digest
```

- [ ] **Step 2: Run the Evidence tests and confirm RED**

- [ ] **Step 3: Persist finalization/integrity facts**

Collector manifest completion must persist immutable object refs, manifest digest, finalized timestamp, workspace/source/platform bindings, and bounded collection failure metadata. `verify_terminal_gate` re-reads these facts and returns a receipt only after all checks pass.

- [ ] **Step 4: Add bounded collection exhaustion behavior**

After configured attempts, persist `collection_failed` with error evidence reference. Do not create a successful gate receipt.

- [ ] **Step 5: Run Evidence and RunStore suites and confirm GREEN**

```bash
PYTHONPATH=src uv run --extra dev pytest -q \
  tests/test_evidence_binding.py tests/test_evidence.py tests/test_run_store.py
```

- [ ] **Step 6: Commit Task 3**

```bash
git add src/pilot107/core/evidence_binding.py src/pilot107/core/run_store.py \
  src/pilot107/worker/evidence.py tests/test_evidence_binding.py \
  tests/test_evidence.py tests/test_run_store.py
git commit -m "feat: verify terminal agent task evidence"
```

### Task 4: Gate AgentTask finalization and follow-up creation

**Files:**
- Modify: `src/pilot107/services/agent_task_service.py`
- Modify: `src/pilot107/services/agent_session_service.py`
- Test: `tests/agent/test_agent_task_service.py`
- Test: `tests/test_agent_session_service.py`

**Interfaces:**
- Consumes: `EvidenceBinder.verify_terminal_gate` and Task store `advance_gate/finalize_task`.
- Produces: a ready outbox message only after a terminal Task transition.

- [ ] **Step 1: Write failing service tests for every gate**

Add tests named:

```text
test_schedule_receipt_never_completes_agent_task
test_run_terminal_without_finalized_evidence_stays_awaiting_evidence
test_evidence_required_task_completes_without_capsule
test_evidence_and_capsule_task_waits_for_capsule_ready
test_integrity_failure_blocks_task
test_collection_exhaustion_finishes_as_evidence_unavailable
test_ready_followup_is_idempotent_after_finalizer_crash
```

The first test must assert no `AGENT_TASK_READY_TOPIC` outbox row exists after only scheduling or observing Run terminal.

- [ ] **Step 2: Run service tests and confirm RED**

- [ ] **Step 3: Replace `_reconcile_one` terminal shortcut with policy-driven finalization**

```python
if run.state not in TERMINAL_RUN_STATES:
    return release_pending()
if run.state is RunState.SUCCEEDED:
    receipt = self.evidence_binder.verify_terminal_gate(
        run.run_id,
        self.run_store.list_terminal_evidence_refs(run.run_id),
        task.request.workspace_boundary,
    )
    if task.completion_policy.requires_capsule:
        receipt = self.capsule_gate.require_ready(run, receipt)
    completed = self.store.finalize_task(
        task.task_id,
        lease=lease,
        gate_receipt=receipt,
        result=AgentTaskResult(
            status="succeeded",
            evidence_refs=receipt.evidence_refs,
            error_code=None,
            message="validation Run and Evidence gate completed",
        ),
    )
else:
    completed = self._finalize_non_success_run_with_failure_evidence(
        task=task,
        run=run,
        lease=lease,
    )
self._enqueue_ready(completed)
```

The finalizer must re-read authoritative facts under Task CAS immediately before storing outcome.

- [ ] **Step 4: Ensure `_dispatch_ready` accepts only terminal, gate-verified Task versions**

- [ ] **Step 5: Run service tests and confirm GREEN**

```bash
PYTHONPATH=src uv run --extra dev pytest -q \
  tests/agent/test_agent_task_service.py tests/test_agent_session_service.py
```

- [ ] **Step 6: Commit Task 4**

```bash
git add src/pilot107/services/agent_task_service.py \
  src/pilot107/services/agent_session_service.py \
  tests/agent/test_agent_task_service.py tests/test_agent_session_service.py
git commit -m "fix: gate agent task followups on evidence"
```

### Task 5: Reorder Runtime Worker reconciliation and enforce Capsule policy

**Files:**
- Modify: `src/pilot107/worker/runtime_worker.py`
- Modify: `src/pilot107/worker/capsule.py`
- Test: `tests/test_runtime_worker.py`
- Test: `tests/test_worker_auto_capsule.py`
- Test: `tests/test_capsule.py`

**Interfaces:**
- Produces: collection → integrity → optional Capsule → Task finalization ordering.
- Preserves: best-effort Capsule for policies that do not require it.

- [ ] **Step 1: Write a failing tick-order regression test**

```python
def test_tick_cannot_dispatch_followup_before_collection_and_capsule_gate(worker):
    result = worker.tick()
    assert result.agent_task_ready == 0
    assert result.collection_completed == 1
    assert result.capsules_ready == 1
    assert worker.tick().agent_task_ready == 1
```

Use real stores; fake only external Slurm/collector boundaries.

- [ ] **Step 2: Confirm RED against current Task-before-collection ordering**

- [ ] **Step 3: Split tick into named phases and move Task finalization after collection/integrity/Capsule**

Required phase order:

```text
submit/reconcile Run
→ collect/finalize Evidence
→ verify integrity
→ build/reconcile required Capsule
→ finalize AgentTask
→ dispatch ready follow-up
```

- [ ] **Step 4: Run worker and Capsule tests and confirm GREEN**

- [ ] **Step 5: Commit Task 5**

```bash
git add src/pilot107/worker/runtime_worker.py src/pilot107/worker/capsule.py \
  tests/test_runtime_worker.py tests/test_worker_auto_capsule.py tests/test_capsule.py
git commit -m "fix: finalize agent tasks after evidence collection"
```

### Task 6: Update API/UI terminology and prove the live Slurm chain

**Files:**
- Modify: `src/pilot107/api/agent_task_routes.py`
- Modify: `apps/web/src/types.ts`
- Modify: `apps/web/src/AgentProjectPanel.tsx`
- Modify: `apps/web/src/AgentSessionPanel.tsx`
- Test: `apps/web/src/AgentProjectPanel.test.tsx`
- Test: `apps/web/src/AgentSessionPanel.test.tsx`
- Modify: `scripts/smoke-vm-agent-task.py`

**Interfaces:**
- UI renders schedule receipt as queued/running/awaiting evidence, never completed.
- Terminal result links only integrity-verified Evidence and policy-required Capsule.

- [ ] **Step 1: Write failing UI tests for non-terminal receipt copy and terminal gate copy**

- [ ] **Step 2: Confirm RED**

```bash
npm test -- --run src/AgentProjectPanel.test.tsx src/AgentSessionPanel.test.tsx
```

- [ ] **Step 3: Implement minimal typed rendering changes**

- [ ] **Step 4: Extend VM smoke assertions**

The smoke must assert one numeric Slurm Job ID, no ready follow-up before Evidence finalized/integrity verified, Capsule gating only for the required policy, and exactly one follow-up after API/Worker restart.

- [ ] **Step 5: Run focused and end-to-end verification**

```bash
PYTHONPATH=src uv run --extra dev pytest -q \
  tests/agent/test_agent_task_service.py tests/test_runtime_worker.py \
  tests/test_evidence_binding.py tests/test_capsule.py
cd apps/web && npm test -- --run src/AgentProjectPanel.test.tsx src/AgentSessionPanel.test.tsx
cd ../.. && bash scripts/smoke-vm-agent-task.sh
```

- [ ] **Step 6: Commit Task 6**

```bash
git add src/pilot107/api/agent_task_routes.py apps/web/src/types.ts \
  apps/web/src/AgentProjectPanel.tsx apps/web/src/AgentSessionPanel.tsx \
  apps/web/src/AgentProjectPanel.test.tsx apps/web/src/AgentSessionPanel.test.tsx \
  scripts/smoke-vm-agent-task.py
git commit -m "test: prove evidence-gated agent task completion"
```

## Completion Gate

Do not declare this plan complete until a fresh run proves:

```bash
PYTHONPATH=src uv run --extra dev pytest -q \
  tests/agent/test_task_store_contract.py \
  tests/agent/test_agent_task_service.py \
  tests/test_run_store.py tests/test_evidence.py tests/test_evidence_binding.py \
  tests/test_capsule.py tests/test_runtime_worker.py
cd services/pilot-agentd && npm run check
cd ../../apps/web && npm test
cd ../.. && bash scripts/smoke-vm-agent-task.sh
```

Read every exit code and failure count. Selected unit tests do not replace the live VM/Slurm causality assertion.
