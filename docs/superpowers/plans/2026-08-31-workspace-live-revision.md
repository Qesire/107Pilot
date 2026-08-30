# Workspace Live Revision Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent concurrent Agent or user edits from overwriting each other and recover deterministically when a process crashes between file application and ChangeSet persistence.

**Architecture:** Preserve the immutable imported base snapshot, add a monotonic live revision/digest and fenced single-writer lease, and execute mutations through a write-ahead journal with PREPARED, FILES_APPLIED and COMMITTED boundaries.

**Tech Stack:** Python, bounded POSIX file operations, SQLite/PostgreSQL stores, SHA-256 manifests, pytest.

## Global Constraints

- Database and filesystem are not one ACID transaction; journal/reconciliation provides observable atomicity.
- Every mutation requires expected live revision/digest and a valid writer fence.
- Immutable base snapshot remains unchanged and separately addressable.
- A crash resolves to commit, rollback, or `conflicted`; never guess and overwrite.
- Run, Evidence, Capsule and publication bind the exact workspace revision/digest used.
- Existing workspaces backfill revision 1 from their bounded live manifest or become `conflicted`.
- SQLite/PostgreSQL parity and TDD are mandatory.

---

### Task 1: Define live workspace and mutation journal domain

**Files:**
- Modify: `src/pilot107/agent/workspace.py`
- Modify: `src/pilot107/agent/project_store.py`
- Modify: `src/pilot107/agent/postgres_project_store.py`
- Modify: `src/pilot107/agent/migrations.py`
- Modify: `src/pilot107/core/postgres_domain_schema.py`
- Test: `tests/agent/test_workspace_snapshot.py`
- Create: `tests/agent/test_project_store.py`

**Interfaces:**
- Produces: `WorkspaceLiveBoundary`, `WorkspaceWriterLease`, `WorkspaceMutationJournal`.

- [ ] **Step 1: Write failing model/backend tests**

Create three tests that assert: import keeps the original base digest and initializes live revision 1;
the same workspace/operation key cannot describe different mutation content; and a reclaimed writer's
old fencing token cannot change journal state.

- [ ] **Step 2: Confirm RED**

- [ ] **Step 3: Add additive schema and typed records**

```python
@dataclass(frozen=True)
class WorkspaceLiveBoundary:
    revision: int
    digest: str

class WorkspaceMutationState(StrEnum):
    PREPARED = "prepared"
    FILES_APPLIED = "files_applied"
    COMMITTED = "committed"
    ROLLED_BACK = "rolled_back"
    CONFLICTED = "conflicted"
```

Persist writer owner/expiry/fence and a journal containing expected/result boundaries, before/after file digests, temp paths, operation key and journal digest.

- [ ] **Step 4: Confirm GREEN on SQLite and PostgreSQL contracts**

- [ ] **Step 5: Commit Task 1**

```bash
git add src/pilot107/agent/workspace.py src/pilot107/agent/project_store.py \
  src/pilot107/agent/postgres_project_store.py src/pilot107/agent/migrations.py \
  src/pilot107/core/postgres_domain_schema.py tests/agent/test_workspace_snapshot.py \
  tests/agent/test_project_store.py
git commit -m "feat: define live workspace revisions"
```

### Task 2: Implement writer lease and revision CAS

**Files:**
- Modify: `src/pilot107/agent/project_store.py`
- Modify: `src/pilot107/agent/postgres_project_store.py`
- Modify (created in Task 1): `tests/agent/test_project_store.py`
- Create: `tests/agent/test_postgres_project_store.py`

**Interfaces:**
- Produces: `claim_workspace_writer`, `renew_workspace_writer`, `prepare_workspace_mutation`, `mark_files_applied`, `commit_workspace_mutation`, `mark_workspace_conflicted`.

- [ ] **Step 1: Write failing concurrency tests**

Create three concurrency tests that assert exactly one of two prepares against the same boundary wins,
a commit rejects a changed live digest, and writer reclaim increments the fence and rejects the prior
writer.

- [ ] **Step 2: Confirm RED on both backends**

- [ ] **Step 3: Implement local transaction CAS methods**

Every transition matches workspace owner/id, writer token, unexpired lease, journal version, and expected live boundary. `commit_workspace_mutation` increments revision exactly once and is idempotent by operation key.

- [ ] **Step 4: Confirm GREEN**

```bash
PYTHONPATH=src uv run --extra dev pytest -q \
  tests/agent/test_project_store.py tests/agent/test_postgres_project_store.py
```

- [ ] **Step 5: Commit Task 2**

```bash
git add src/pilot107/agent/project_store.py src/pilot107/agent/postgres_project_store.py \
  tests/agent/test_project_store.py tests/agent/test_postgres_project_store.py
git commit -m "feat: fence workspace mutation writers"
```

### Task 3: Replace direct patch persistence with journaled file application

**Files:**
- Modify: `src/pilot107/agent/workspace.py`
- Test: `tests/agent/test_workspace_patch.py`

**Interfaces:**
- Consumes: Task 2 store methods.
- Produces: journal-backed
  `WorkspaceEditor.apply_patches(workspace_id, owner, patches, expected_live_revision, expected_live_digest, operation_key, writer_lease)`.

- [ ] **Step 1: Add failing mutation and crash-injection tests**

```text
test_patch_increments_live_revision_and_digest_once
test_patch_rejects_stale_workspace_boundary_before_file_write
test_crash_after_prepared_leaves_recoverable_journal
test_crash_after_files_applied_never_exposes_committed_revision
test_operation_key_replay_returns_original_changeset
```

- [ ] **Step 2: Confirm RED**

- [ ] **Step 3: Implement bounded temp-write/rename phase**

Write temp files under the workspace, record their paths/digests in PREPARED, fsync where supported, rename only after fence validation, mark FILES_APPLIED, re-read and hash the manifest, then commit the new boundary and ChangeSet. Preserve before images for rollback.

- [ ] **Step 4: Confirm GREEN**

```bash
PYTHONPATH=src uv run --extra dev pytest -q tests/agent/test_workspace_patch.py
```

- [ ] **Step 5: Commit Task 3**

```bash
git add src/pilot107/agent/workspace.py tests/agent/test_workspace_patch.py
git commit -m "feat: journal workspace file mutations"
```

### Task 4: Reconcile interrupted workspace journals

**Files:**
- Create: `src/pilot107/services/workspace_reconciliation_service.py`
- Modify: `src/pilot107/worker/runtime_worker.py`
- Create: `tests/agent/test_workspace_reconciliation_service.py`
- Test: `tests/test_runtime_worker.py`

**Interfaces:**
- Produces: `WorkspaceReconciliationService.reconcile_due(limit) -> ReconciliationBatch`.

- [ ] **Step 1: Write failing state/disk matrix tests**

Cover PREPARED with no files changed, PREPARED with partial change, FILES_APPLIED with correct after digest, FILES_APPLIED with unknown digest, and stale writer return.

- [ ] **Step 2: Confirm RED**

- [ ] **Step 3: Implement deterministic decisions**

```text
old digest everywhere → rollback journal
verified after digest everywhere → fenced CAS commit
mixed state with complete before images → fenced rollback
unprovable state → workspace conflicted
```

- [ ] **Step 4: Wire reconciliation before new workspace mutations in worker tick**

- [ ] **Step 5: Confirm GREEN**

```bash
PYTHONPATH=src uv run --extra dev pytest -q \
  tests/agent/test_workspace_reconciliation_service.py tests/test_runtime_worker.py
```

- [ ] **Step 6: Commit Task 4**

```bash
git add src/pilot107/services/workspace_reconciliation_service.py \
  src/pilot107/worker/runtime_worker.py \
  tests/agent/test_workspace_reconciliation_service.py tests/test_runtime_worker.py
git commit -m "feat: reconcile interrupted workspace mutations"
```

### Task 5: Bind Builder, publication, Run and Evidence to live boundary

**Files:**
- Modify: `src/pilot107/services/project_agent_service.py`
- Modify: `src/pilot107/services/builder_workflow_service.py`
- Modify: `src/pilot107/agent/publisher.py`
- Modify: `src/pilot107/services/agent_task_service.py`
- Modify: `src/pilot107/core/run_store.py`
- Modify: `src/pilot107/worker/evidence.py`
- Modify: `src/pilot107/worker/capsule.py`
- Test: `tests/agent/test_builder_workflow_service.py`
- Test: `tests/agent/test_workspace_publisher.py`
- Test: `tests/agent/test_agent_task_service.py`

**Interfaces:**
- Every request/receipt carries immutable base digest plus live revision/digest.

- [ ] **Step 1: Write failing stale-boundary integration tests**

Add tests proving user edits invalidate an old Builder submit, publication rejects a stale ChangeSet, and Run/Evidence/Capsule share the submitted live boundary.

- [ ] **Step 2: Confirm RED**

- [ ] **Step 3: Thread live boundary and operation key through typed services**

Do not overload the old snapshot digest. Legacy clients receive a structured conflict that includes the current boundary and immutable base ref.

- [ ] **Step 4: Confirm GREEN**

```bash
PYTHONPATH=src uv run --extra dev pytest -q \
  tests/agent/test_builder_workflow_service.py \
  tests/agent/test_workspace_publisher.py \
  tests/agent/test_agent_task_service.py
```

- [ ] **Step 5: Commit Task 5**

```bash
git add src/pilot107/services/project_agent_service.py \
  src/pilot107/services/builder_workflow_service.py src/pilot107/agent/publisher.py \
  src/pilot107/services/agent_task_service.py src/pilot107/core/run_store.py \
  src/pilot107/worker/evidence.py src/pilot107/worker/capsule.py \
  tests/agent/test_builder_workflow_service.py tests/agent/test_workspace_publisher.py \
  tests/agent/test_agent_task_service.py
git commit -m "feat: bind runs to live workspace revisions"
```

### Task 6: Backfill and end-to-end conflict acceptance

**Files:**
- Modify: `src/pilot107/agent/migrations.py`
- Modify: `src/pilot107/core/postgres_domain_schema.py`
- Modify: `scripts/smoke-vm-agent-task.py`
- Create: `tests/agent/test_workspace_migration.py`

- [ ] **Step 1: Write failing legacy backfill tests**

- [ ] **Step 2: Implement revision-1 backfill or conflicted fallback**

- [ ] **Step 3: Extend live smoke to inject a competing user edit**

The old Agent patch must fail with `workspace_conflict`; after a fresh context read and recomputed patch, exactly one new revision may commit and the Slurm Run must bind that revision.

- [ ] **Step 4: Run completion verification**

```bash
PYTHONPATH=src uv run --extra dev pytest -q \
  tests/agent/test_workspace_snapshot.py tests/agent/test_project_store.py \
  tests/agent/test_postgres_project_store.py tests/agent/test_workspace_patch.py \
  tests/agent/test_workspace_reconciliation_service.py \
  tests/agent/test_builder_workflow_service.py tests/agent/test_workspace_publisher.py
bash scripts/smoke-vm-agent-task.sh
```

- [ ] **Step 5: Commit Task 6**

```bash
git add src/pilot107/agent/migrations.py src/pilot107/core/postgres_domain_schema.py \
  scripts/smoke-vm-agent-task.py tests/agent/test_workspace_migration.py
git commit -m "test: prove workspace revision recovery"
```

## Completion Gate

Completion requires fresh evidence for backend parity, crash recovery at PREPARED and FILES_APPLIED, exactly-one concurrent writer, stale user-edit protection, and a real Slurm Run whose Evidence records the same immutable base plus live revision/digest.
