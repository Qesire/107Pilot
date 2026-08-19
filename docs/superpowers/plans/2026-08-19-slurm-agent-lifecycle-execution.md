# Slurm Agent Lifecycle Completion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete the gap from the finished A1 durable read-only Agent Turn to a unified, observable, recoverable Slurm experiment lifecycle covering isolated project editing, asynchronous validation, safe publication, formal Runs, remediation, and market reuse.

**Architecture:** Preserve the existing Run/Worker/Evidence control plane as the execution authority and extend the A1 AgentSession substrate with focused domain stores for projects, workspaces, AgentTask, Runtime Watch, and resource observation. Build the three independent foundations—Workspace, Runtime Watch, and resource observation—before A3/A4 integration; then adapt the existing Remediation and Market services into the unified Agent lifecycle instead of replacing their proven state machines.

**Tech Stack:** Python 3.12, SQLite and PostgreSQL 16 contract stores, FastAPI/stdlib HTTP adapters, React/TypeScript, Node 22.19.0, Pi 0.84.1, durable outbox/lease/fencing, Docker Slurm 25.11.2 simulator, pytest, mypy, Ruff, Vitest, Playwright.

## Global Constraints

- The application-side `pilot-agentd` is the Agent brain; no Pi/Node/Python Agent process may run persistently on a Slurm login node.
- A sleeping AgentSession uses no resident Pi process, and a pending/running AgentTask releases its Pi Turn.
- Pi receives only versioned, allowlisted tools and never receives SSH credentials, Slurm tokens, MFA material, arbitrary host shell, or unrestricted remote paths.
- Cluster files remain the source of truth; code/configuration may enter an owner-isolated AgentWorkspace, while datasets, checkpoints, and files at or above 5 GiB remain metadata-only.
- Workspace publication and formal Run submission require explicit user approval; only validation inside a previously approved AgentResourceEnvelope may execute automatically.
- Every write is owner-scoped, idempotent, version checked, lease protected where asynchronous, and fenced against stale writers.
- Run state, Runtime Watch state, resource-observation state, Evidence collection state, Diagnosis state, and Agent workflow state remain separate.
- Missing, unsupported, stale, partial, insufficient-coverage, and zero-valued measurements are distinct states.
- Runtime Watch and observability APIs only read persisted product facts; browser/API/Agent reads never trigger direct Slurm or filesystem polling.
- D0 means deterministic local tests, D1 means Docker Slurm 25.11.2 live behavior, S1 means the 8C/16G VM, R0 means an authorized read-only real-cluster probe, and R1 means authorized real 107 service integration. Evidence from one environment must not be relabeled as another.
- SQLite and PostgreSQL implementations must pass the same store contracts before a new durable aggregate is production-selectable.
- Each task follows red-green-refactor, ends with a focused commit, and clears P0/P1 review findings before the next dependent task begins.
- Preserve current `/api/v1/agent-sessions`, Remediation, Run, Evidence, Market, and file-transfer contracts unless a versioned replacement and compatibility test are included.

---

## Delivery Graph

```text
Task 1 schema freeze
  ├── Task 2 A1 Web surface
  ├── Tasks 3–6 Workspace foundation
  ├── Tasks 7–9 Runtime Watch
  └── Tasks 10–12 resource observation

Tasks 3–6 + existing A1 + existing Run/Evidence
  └── Tasks 13–14 A3 asynchronous Slurm validation

Tasks 10–12 + Tasks 13–14 + existing WorkflowPolicy
  └── Task 15 artifact-aware DAG and array recovery

Tasks 7–9 + Tasks 13–15
  └── Tasks 16–17 A4 safe publication and formal Run

Tasks 10–12 + Tasks 16–17
  └── Tasks 18–19 A5 remediation and market unification

All functional tasks
  └── Tasks 20–21 PostgreSQL, deployment, and environment gates
```

Runtime Watch and resource observation are independent fact providers. They may be developed concurrently with Workspace after Task 1, but A4 cannot pass until Runtime Watch is integrated, and A5 resource advice cannot pass until resource observation is integrated.

## File Structure

New files are split by aggregate responsibility:

```text
schemas/agent/v2/project-session.schema.json
schemas/agent/v2/workspace-changeset.schema.json
schemas/agent/v2/agent-task.schema.json
schemas/runtime-watch/v1/runtime-watch.schema.json
schemas/observability/v1/resource-observation.schema.json

src/pilot107/agent/project.py                 # project/blueprint invariants
src/pilot107/agent/project_store.py           # SQLite project/workspace persistence
src/pilot107/agent/postgres_project_store.py  # PostgreSQL parity
src/pilot107/agent/workspace.py               # snapshot/import/patch/diff policies
src/pilot107/agent/sandbox.py                 # bounded local validation executor
src/pilot107/agent/tasks.py                   # AgentTask and resource envelope model
src/pilot107/agent/task_store.py              # SQLite task persistence
src/pilot107/agent/postgres_task_store.py     # PostgreSQL parity
src/pilot107/services/project_agent_service.py
src/pilot107/services/agent_task_service.py
src/pilot107/api/project_agent_routes.py

src/pilot107/runtime_watch/model.py
src/pilot107/runtime_watch/store.py
src/pilot107/runtime_watch/postgres_store.py
src/pilot107/runtime_watch/reader.py
src/pilot107/runtime_watch/evaluator.py
src/pilot107/runtime_watch/service.py
src/pilot107/api/runtime_watch_routes.py

src/pilot107/observability/model.py
src/pilot107/observability/store.py
src/pilot107/observability/postgres_store.py
src/pilot107/observability/adapters.py
src/pilot107/observability/collector.py
src/pilot107/observability/evaluator.py
src/pilot107/observability/service.py
src/pilot107/api/observability_routes.py

apps/web/src/AgentSessionPanel.tsx
apps/web/src/AgentProjectPanel.tsx
apps/web/src/RuntimeWatchPanel.tsx
apps/web/src/RunResourcePanel.tsx
```

Existing files remain composition roots or adapters: `agent/store_factory.py`, `api/asgi_app.py`, `api/service.py`, `worker/service.py`, `worker/runtime_worker.py`, `core/run_service.py`, `core/run_store.py`, `services/remediation_service.py`, and Market services.

---

### Task 1: Freeze Cross-Subsystem Schemas and State Ownership

**Files:**
- Create: `schemas/agent/v2/project-session.schema.json`
- Create: `schemas/agent/v2/workspace-changeset.schema.json`
- Create: `schemas/agent/v2/agent-task.schema.json`
- Create: `schemas/runtime-watch/v1/runtime-watch.schema.json`
- Create: `schemas/observability/v1/resource-observation.schema.json`
- Modify: `schemas/agent/v2/README.md`
- Test: `tests/agent/test_lifecycle_schemas.py`
- Test: `services/pilot-agentd/tests/lifecycle-schema.test.ts`

**Interfaces:**
- Consumes: existing `pilot107.agent/v2` Turn/tool envelopes and existing Run IDs.
- Produces: canonical state names and wire fields consumed by every later task.

- [ ] **Step 1: Write failing schema-presence and golden-payload tests**

```python
@pytest.mark.parametrize("relative", [
    "agent/v2/project-session.schema.json",
    "agent/v2/workspace-changeset.schema.json",
    "agent/v2/agent-task.schema.json",
    "runtime-watch/v1/runtime-watch.schema.json",
    "observability/v1/resource-observation.schema.json",
])
def test_lifecycle_schema_accepts_its_golden_payload(relative: str) -> None:
    schema = json.loads((SCHEMA_ROOT / relative).read_text())
    Draft202012Validator(schema).validate(GOLDENS[relative])
```

- [ ] **Step 2: Run the focused test and confirm missing-schema failure**

Run: `uv run pytest tests/agent/test_lifecycle_schemas.py -q`

Expected: FAIL with `FileNotFoundError` for the first new schema.

- [ ] **Step 3: Define exact state and identity contracts**

The schemas must freeze these discriminants:

```text
ProjectOrigin = blank | template | existing | failed_run
ProjectState = drafting | editing | validating | awaiting_approval | publishing | ready | blocked | cancelled
ChangeSetState = draft | reviewable | approved | publishing | published | conflicted | failed | cancelled
AgentTaskState = pending | running | succeeded | failed | cancelled | auth_required
RuntimeWatchState = watching | waiting_for_log | active | quiet_backoff | degraded | finalizing | stopped
MeasurementAvailability = available | unsupported | not_collected | insufficient_coverage | invalid
```

Every aggregate includes `owner`, immutable aggregate ID, `version`, timestamps, schema version, and explicit references rather than embedded credentials or paths supplied by the model.

- [ ] **Step 4: Run Python and TypeScript schema contracts**

Run: `uv run pytest tests/agent/test_lifecycle_schemas.py -q && npm --prefix services/pilot-agentd test -- --run lifecycle-schema`

Expected: both suites PASS and reject unknown state values, missing owner, and additional security-sensitive fields.

- [ ] **Step 5: Commit the frozen contracts**

```bash
git add schemas tests/agent/test_lifecycle_schemas.py services/pilot-agentd/tests/lifecycle-schema.test.ts
git commit -m "docs: freeze agent lifecycle schemas"
```

---

### Task 2: Expose Finished A1 Sessions in the Web Product

**Files:**
- Create: `apps/web/src/AgentSessionPanel.tsx`
- Create: `apps/web/src/AgentSessionPanel.test.tsx`
- Modify: `apps/web/src/types.ts`
- Modify: `apps/web/src/api.ts`
- Modify: `apps/web/src/api.test.ts`
- Modify: `apps/web/src/query.ts`
- Modify: `apps/web/src/AgentPage.tsx`
- Modify: `apps/web/src/AgentPage.test.ts`
- Modify: `tests/ui/visual.spec.js`

**Interfaces:**
- Consumes: `GET/POST /api/v1/agent-sessions`, Turn creation/cancel, and durable event replay from A1.
- Produces: `AgentSessionPanel` with session creation, Turn submission, ordered event rendering, resume cursor, cancel, and explicit separation from Remediation.

- [ ] **Step 1: Add failing API and event-replay tests**

```ts
it("replays durable events after the last rendered event id", async () => {
  server.expectGet("/api/v1/agent-sessions/s1/events?after_event_id=7");
  await api.agentSessionEvents("alice", "s1", 7);
});
```

- [ ] **Step 2: Run the focused Web tests**

Run: `npm test -- AgentSessionPanel api`

Expected: FAIL because `agentSessionEvents` and `AgentSessionPanel` do not exist.

- [ ] **Step 3: Add typed A1 client functions and the panel**

Expose exactly:

```ts
agentSessions(user, signal?)
createAgentSession(user, { profile, request_key })
agentSession(user, sessionId, signal?)
createAgentTurn(user, sessionId, { message, request_key, expected_state_version })
cancelAgentTurn(user, sessionId, turnId, expectedStateVersion)
agentSessionEvents(user, sessionId, afterEventId, signal?)
```

`AgentPage` must show two explicit modes: `Conversation` uses the A1 APIs; `Repair` retains the existing Remediation UI and contracts.

- [ ] **Step 4: Verify reconnect, owner isolation error, and current remediation regression**

Run: `npm test -- --run && npm run typecheck`

Expected: PASS with duplicate global event IDs ignored, equal per-Turn sequences retained, and the existing Remediation tests unchanged.

- [ ] **Step 5: Commit the A1 product surface**

```bash
git add apps/web/src tests/ui/visual.spec.js
git commit -m "feat: expose durable agent conversations"
```

---

### Task 3: Add Experiment Project and Blueprint Persistence

**Files:**
- Create: `src/pilot107/agent/project.py`
- Create: `src/pilot107/agent/project_store.py`
- Create: `src/pilot107/agent/postgres_project_store.py`
- Create: `tests/agent/test_project_store_contract.py`
- Modify: `src/pilot107/agent/store_factory.py`

**Interfaces:**
- Consumes: Task 1 project schema and existing owner identity.
- Produces: `ExperimentProjectSessionRecord`, `ProjectBlueprint`, `ProjectStore`, `SQLiteProjectStore`, and `PostgresProjectStore`.

- [ ] **Step 1: Write the dual-backend store contract**

```python
def test_project_version_cas_and_owner_scope(store: ProjectStore) -> None:
    created = store.create_project(owner="alice", origin="blank", goal="sum numbers")
    updated = store.save_blueprint(created.project_id, "alice", created.version, BLUEPRINT)
    assert updated.version == created.version + 1
    with pytest.raises(ProjectConflict):
        store.save_blueprint(created.project_id, "alice", created.version, BLUEPRINT)
    with pytest.raises(KeyError):
        store.get_project(created.project_id, "bob")
```

- [ ] **Step 2: Run SQLite contract tests and confirm the import failure**

Run: `uv run pytest tests/agent/test_project_store_contract.py -q`

Expected: FAIL because `pilot107.agent.project` does not exist.

- [ ] **Step 3: Implement immutable records and CAS stores**

`ProjectBlueprint` must contain goal, entrypoints, files, validation plan, Contract intent, expected outputs, dependency metadata, and open questions. Stable IDs use caller request keys or deterministic digests; duplicate create requests return the same project.

- [ ] **Step 4: Run SQLite and temporary PostgreSQL contracts**

Run: `uv run pytest tests/agent/test_project_store_contract.py -q`

Expected: SQLite PASS; PostgreSQL PASS when `PILOT107_TEST_POSTGRES_DSN` is set and otherwise explicit SKIP.

- [ ] **Step 5: Commit the project aggregate**

```bash
git add src/pilot107/agent/project.py src/pilot107/agent/project_store.py src/pilot107/agent/postgres_project_store.py src/pilot107/agent/store_factory.py tests/agent/test_project_store_contract.py
git commit -m "feat: persist agent experiment projects"
```

---

### Task 4: Snapshot Cluster Projects into Owner-Isolated Workspaces

**Files:**
- Create: `src/pilot107/agent/workspace.py`
- Create: `tests/agent/test_workspace_snapshot.py`
- Modify: `src/pilot107/adapters/ssh_relay.py`
- Modify: `src/pilot107/core/file_uploads.py`
- Modify: `src/pilot107/agent/project_store.py`

**Interfaces:**
- Consumes: `ProjectStore`, approved owner roots, typed SSH relay reads, and existing upload/file policies.
- Produces: `WorkspaceSnapshot`, `WorkspaceEntry`, and `WorkspaceImporter.create(project, source_ref) -> AgentWorkspaceRecord`.

- [ ] **Step 1: Write failing classification and containment tests**

```python
def test_snapshot_keeps_large_weights_metadata_only(importer: WorkspaceImporter) -> None:
    workspace = importer.create(PROJECT, source_ref="/public/home/alice/exp")
    weight = next(item for item in workspace.entries if item.path == "model.ckpt")
    assert weight.classification == "metadata_only"
    assert weight.content_ref is None
    assert weight.size_bytes == 5 * 1024**3
```

Also reject symlinks escaping the source root, devices, sockets, cross-owner roots, and path traversal.

- [ ] **Step 2: Run the focused tests**

Run: `uv run pytest tests/agent/test_workspace_snapshot.py -q`

Expected: FAIL because `WorkspaceImporter` is undefined.

- [ ] **Step 3: Implement bounded manifest-first import**

Classify entries as `editable`, `read_only`, `metadata_only`, or `excluded`; save source size/mtime/SHA-256 where obtainable; copy only allowed editable code/config files to `<workspace_root>/<owner>/<workspace_id>` using atomic writes.

- [ ] **Step 4: Verify negative paths and a typed SSH fixture**

Run: `uv run pytest tests/agent/test_workspace_snapshot.py tests/test_ssh_relay.py tests/test_file_upload_api.py -q`

Expected: PASS without adding arbitrary remote shell or arbitrary-path relay operations.

- [ ] **Step 5: Commit snapshot import**

```bash
git add src/pilot107/agent/workspace.py src/pilot107/adapters/ssh_relay.py src/pilot107/core/file_uploads.py src/pilot107/agent/project_store.py tests/agent/test_workspace_snapshot.py
git commit -m "feat: create isolated agent workspaces"
```

---

### Task 5: Add Bounded Patch, Diff, and Sandbox Validation

**Files:**
- Create: `src/pilot107/agent/sandbox.py`
- Create: `tests/agent/test_workspace_patch.py`
- Create: `tests/agent/test_workspace_sandbox.py`
- Modify: `src/pilot107/agent/workspace.py`
- Modify: `src/pilot107/agent/project_store.py`

**Interfaces:**
- Consumes: Task 4 AgentWorkspace and Task 1 ChangeSet schema.
- Produces: `WorkspaceChangeSet`, `WorkspaceEditor.apply_patch`, `WorkspaceEditor.diff`, and `SandboxExecutor.execute`.

- [ ] **Step 1: Write patch hash, traversal, and process-budget tests**

```python
def test_patch_requires_expected_source_digest(editor: WorkspaceEditor) -> None:
    with pytest.raises(WorkspaceConflict):
        editor.apply_patch("w1", "alice", "main.py", "0" * 64, PATCH)

def test_sandbox_kills_command_at_deadline(sandbox: SandboxExecutor) -> None:
    result = sandbox.execute(WORKSPACE, argv=("python", "-c", "while True: pass"), timeout=1)
    assert result.status == "timed_out"
```

- [ ] **Step 2: Run the focused tests and confirm missing implementations**

Run: `uv run pytest tests/agent/test_workspace_patch.py tests/agent/test_workspace_sandbox.py -q`

Expected: FAIL on missing editor and sandbox classes.

- [ ] **Step 3: Implement typed patching and an argv-only sandbox**

Allow only workspace-contained regular files, explicit argv from a command policy, bounded CPU/memory/process/time/output, no host network, read-only dependency caches, and no cluster credentials. Persist each file before/after digest and each sandbox result.

- [ ] **Step 4: Run security and determinism tests**

Run: `uv run pytest tests/agent/test_workspace_patch.py tests/agent/test_workspace_sandbox.py tests/test_path_policy.py -q`

Expected: PASS for syntax/test commands and rejection of shell strings, traversal, symlink escape, oversized diff, and output overflow.

- [ ] **Step 5: Commit edit and sandbox primitives**

```bash
git add src/pilot107/agent/workspace.py src/pilot107/agent/sandbox.py src/pilot107/agent/project_store.py tests/agent/test_workspace_patch.py tests/agent/test_workspace_sandbox.py
git commit -m "feat: validate isolated workspace changes"
```

---

### Task 6: Expose Project, Workspace, and ChangeSet Tools

**Files:**
- Create: `src/pilot107/services/project_agent_service.py`
- Create: `src/pilot107/api/project_agent_routes.py`
- Create: `tests/agent/test_project_agent_service.py`
- Create: `tests/test_project_agent_api.py`
- Create: `apps/web/src/AgentProjectPanel.tsx`
- Create: `apps/web/src/AgentProjectPanel.test.tsx`
- Modify: `src/pilot107/agent/capabilities.py`
- Modify: `src/pilot107/agent/tool_gateway.py`
- Modify: `src/pilot107/api/asgi_app.py`
- Modify: `apps/web/src/AgentPage.tsx`

**Interfaces:**
- Consumes: Tasks 3–5 project/workspace primitives and A1 capability tokens.
- Produces: project APIs and allowlisted tools `project_get`, `workspace_list`, `workspace_read`, `workspace_patch`, `workspace_diff`, and `sandbox_exec`.

- [ ] **Step 1: Write owner, capability, idempotency, and diff-rendering tests**

```python
def test_workspace_patch_requires_turn_bound_capability(gateway: AgentToolGateway) -> None:
    result = gateway.invoke(INVOCATION_WITHOUT_WORKSPACE_SCOPE)
    assert result.error.code == "TOOL.CAPABILITY_DENIED"
```

- [ ] **Step 2: Run service/API/tool tests**

Run: `uv run pytest tests/agent/test_project_agent_service.py tests/test_project_agent_api.py -q`

Expected: FAIL because project routes and tools are not registered.

- [ ] **Step 3: Implement scoped routes, tools, and review UI**

Tool capabilities bind owner, session, Turn, project, workspace, allowed operations, byte budget, command budget, and expiry. Register `experiment_builder` with the minimum project/workspace tool set and keep `platform_coach` read-only. The UI renders Blueprint, changed files, unified diff, sandbox results, risk summary, and ChangeSet state; it provides no publish action until Task 16.

- [ ] **Step 4: Run Python, Web, and A1 regression suites**

Run: `uv run pytest tests/agent tests/test_project_agent_api.py -q && npm --prefix apps/web test -- --run AgentProjectPanel && npm --prefix apps/web run typecheck`

Expected: PASS and A1 read-only tool trajectories remain unchanged.

- [ ] **Step 5: Commit A2 vertical slice**

```bash
git add src/pilot107/services/project_agent_service.py src/pilot107/api/project_agent_routes.py src/pilot107/agent/capabilities.py src/pilot107/agent/tool_gateway.py src/pilot107/api/asgi_app.py apps/web/src/AgentProjectPanel.tsx apps/web/src/AgentProjectPanel.test.tsx apps/web/src/AgentPage.tsx tests/agent/test_project_agent_service.py tests/test_project_agent_api.py
git commit -m "feat: complete isolated agent editing"
```

**A2 Gate:** Demonstrate blank and existing origins creating reviewable multi-file ChangeSets; prove no cluster source mutation, cross-owner access, unrestricted shell, or large-weight copying. Run full static/test/build gates and record D0 evidence before Task 13.

---

### Task 7: Persist Runtime Watches, Cursors, Segments, and Alerts

**Files:**
- Create: `src/pilot107/runtime_watch/__init__.py`
- Create: `src/pilot107/runtime_watch/model.py`
- Create: `src/pilot107/runtime_watch/store.py`
- Create: `src/pilot107/runtime_watch/postgres_store.py`
- Create: `tests/runtime_watch/test_store_contract.py`

**Interfaces:**
- Consumes: Task 1 Runtime Watch schema and existing Run IDs/owners.
- Produces: `RuntimeWatchRecord`, `RuntimeLogCursor`, `RuntimeLogSegment`, `RuntimeAlert`, and `RuntimeWatchStore` with atomic segment commit and monotonic cursor/fencing semantics.

- [ ] **Step 1: Write dual-backend atomic cursor tests**

```python
def test_commit_segment_is_idempotent_and_advances_cursor_once(store: RuntimeWatchStore) -> None:
    first = store.commit_segment(lease=LEASE, segment=SEGMENT, next_cursor=CURSOR_8)
    second = store.commit_segment(lease=LEASE, segment=SEGMENT, next_cursor=CURSOR_8)
    assert first.segment_id == second.segment_id
    assert store.get_cursor("run1", "alice", "stdout").offset == 8
```

- [ ] **Step 2: Run the store contract**

Run: `uv run pytest tests/runtime_watch/test_store_contract.py -q`

Expected: FAIL because the package is absent.

- [ ] **Step 3: Implement stores and content-addressed segment metadata**

Use deterministic `segment_id = sha256(run_id, stream, generation, start_offset, content_sha256)`, one cursor per Run/stream, monotonic fencing tokens, bounded leases, and owner-scoped reads. Segment content is written atomically before the database transaction; unreferenced content is collectible.

- [ ] **Step 4: Run SQLite/PostgreSQL contracts and crash-point tests**

Run: `uv run pytest tests/runtime_watch/test_store_contract.py -q`

Expected: PASS, with PostgreSQL explicitly skipped only when its DSN is absent.

- [ ] **Step 5: Commit Runtime Watch persistence**

```bash
git add src/pilot107/runtime_watch tests/runtime_watch/test_store_contract.py
git commit -m "feat: persist runtime watch cursors"
```

---

### Task 8: Read Incremental Logs with Fair Scheduling and Recovery

**Files:**
- Create: `src/pilot107/runtime_watch/reader.py`
- Create: `src/pilot107/runtime_watch/service.py`
- Create: `tests/runtime_watch/test_reader.py`
- Create: `tests/runtime_watch/test_scheduler.py`
- Modify: `src/pilot107/worker/ssh_evidence.py`
- Modify: `src/pilot107/worker/runtime_worker.py`
- Modify: `src/pilot107/worker/service.py`

**Interfaces:**
- Consumes: Task 7 store and existing `EvidenceTransport.stat/read_bytes_range`.
- Produces: `IncrementalLogReader.read_next` and `RuntimeWatchService.tick`.

- [ ] **Step 1: Write UTF-8 boundary, rotation, lease, and fairness tests**

```python
def test_reader_preserves_split_utf8_character(reader: IncrementalLogReader) -> None:
    first = reader.read_next(RUN, "stdout", max_bytes=2)
    second = reader.read_next(RUN, "stdout", max_bytes=2)
    assert first.text + second.text == "中"
```

The scheduler test must prove 100 active watches cannot let one fast log consume the entire connection byte budget.

- [ ] **Step 2: Run reader and scheduler tests**

Run: `uv run pytest tests/runtime_watch/test_reader.py tests/runtime_watch/test_scheduler.py -q`

Expected: FAIL because reader and scheduler do not exist.

- [ ] **Step 3: Implement generation-aware range reads and adaptive polling**

Use 256 KiB maximum per stream read, persisted UTF-8 decoder remainder, source identity/prefix fingerprint, 5/15-second active/quiet targets, bounded connection concurrency/bytes per tick, and lease renewal before remote reads. API requests must not call this service.

- [ ] **Step 4: Run restart and double-worker failure injection**

Run: `uv run pytest tests/runtime_watch tests/test_collection_outbox.py -q`

Expected: PASS with no duplicate segment sequence and stale-fence commits rejected.

- [ ] **Step 5: Commit incremental Runtime Watch**

```bash
git add src/pilot107/runtime_watch src/pilot107/worker/ssh_evidence.py src/pilot107/worker/runtime_worker.py src/pilot107/worker/service.py tests/runtime_watch
git commit -m "feat: collect incremental run logs"
```

---

### Task 9: Add Runtime Alerts, Terminal Drain, APIs, and UI

**Files:**
- Create: `src/pilot107/runtime_watch/evaluator.py`
- Create: `src/pilot107/api/runtime_watch_routes.py`
- Create: `tests/runtime_watch/test_evaluator.py`
- Create: `tests/test_runtime_watch_api.py`
- Create: `scripts/smoke-runtime-watch-live.sh`
- Create: `apps/web/src/RuntimeWatchPanel.tsx`
- Create: `apps/web/src/RuntimeWatchPanel.test.tsx`
- Modify: `src/pilot107/runtime_watch/service.py`
- Modify: `src/pilot107/api/asgi_app.py`
- Modify: `src/pilot107/worker/runtime_worker.py`
- Modify: `src/pilot107/core/run_store.py`
- Modify: `apps/web/src/RunEvidencePanel.tsx`

**Interfaces:**
- Consumes: Tasks 7–8 segments/cursors and existing terminal Diagnosis/Evidence collection.
- Produces: owner-scoped Watch/log/alert APIs, SSE summary events, deterministic alerts, and terminal drain handoff.

- [ ] **Step 1: Write cross-segment alert and terminal-tail tests**

```python
def test_terminal_drain_commits_last_bytes_before_logs_finalize(service: RuntimeWatchService) -> None:
    service.on_run_terminal(RUN)
    service.tick()
    assert store.get_cursor(RUN.run_id, RUN.owner, "stdout").offset == len(FINAL_LOG)
    assert run_store.collection_task(RUN.run_id, "logs_finalize").state == "pending"
```

- [ ] **Step 2: Run evaluator/API tests**

Run: `uv run pytest tests/runtime_watch/test_evaluator.py tests/test_runtime_watch_api.py -q`

Expected: FAIL on missing evaluator and routes.

- [ ] **Step 3: Implement deterministic alerts and cursor APIs**

Cover missing import, command not found, missing path, CUDA OOM, NCCL, NaN/Inf, InvalidQOS/Account, impossible dependency, and resource-pressure references. Alerts are provisional and side-effect-free. SSE carries summaries only; log content is fetched by opaque owner/run/stream cursor.

- [ ] **Step 4: Run API/Web tests and Docker incremental-log smoke**

Run: `uv run pytest tests/runtime_watch tests/test_runtime_watch_api.py -q && npm --prefix apps/web test -- --run RuntimeWatchPanel && bash scripts/smoke-runtime-watch-live.sh`

Expected: PASS for incremental stdout/stderr, reconnect, terminal tail, Alice/Bob isolation, and no direct API-triggered cluster read.

- [ ] **Step 5: Commit the Runtime Watch vertical slice**

```bash
git add src/pilot107/runtime_watch/evaluator.py src/pilot107/runtime_watch/service.py src/pilot107/api/runtime_watch_routes.py src/pilot107/api/asgi_app.py src/pilot107/worker/runtime_worker.py src/pilot107/core/run_store.py apps/web/src/RuntimeWatchPanel.tsx apps/web/src/RuntimeWatchPanel.test.tsx apps/web/src/RunEvidencePanel.tsx tests/runtime_watch/test_evaluator.py tests/test_runtime_watch_api.py scripts/smoke-runtime-watch-live.sh
git commit -m "feat: complete runtime watch lifecycle"
```

**Runtime Watch Gate:** D1 must show growing stdout/stderr, disconnect replay, Worker restart recovery, terminal drain, alert deduplication, 100-watch fairness fixture, and zero automatic cancel/modify/submit side effects.

---

### Task 10: Persist Typed Platform, Account, Run Samples, and Summaries

**Files:**
- Create: `src/pilot107/observability/__init__.py`
- Create: `src/pilot107/observability/model.py`
- Create: `src/pilot107/observability/store.py`
- Create: `src/pilot107/observability/postgres_store.py`
- Create: `tests/observability/test_store_contract.py`

**Interfaces:**
- Consumes: Task 1 observation schema, PlatformSnapshot IDs, owner/connection/Run IDs.
- Produces: `ObservedMeasure`, `PlatformPulse`, `AccountPulse`, `RunResourceSample`, `RunResourceSummary`, and `ObservabilityStore`.

- [ ] **Step 1: Write measurement semantics and retention tests**

```python
def test_missing_gpu_measure_is_not_zero(store: ObservabilityStore) -> None:
    summary = store.save_summary(SUMMARY_WITH_UNSUPPORTED_GPU)
    assert summary.used.gpu_utilization.availability == "unsupported"
    assert summary.used.gpu_utilization.value is None
```

- [ ] **Step 2: Run the store contract**

Run: `uv run pytest tests/observability/test_store_contract.py -q`

Expected: FAIL because the observability package is absent.

- [ ] **Step 3: Implement typed facts and dual stores**

Persist field-level source operation, captured time, unit, availability, quality, coverage, warning, cycle ID, and fencing token. Keep raw samples for 2 hours, deterministic one-minute aggregates for 24 hours, and immutable terminal summaries with the Run.

- [ ] **Step 4: Run backend contracts and retention boundary tests**

Run: `uv run pytest tests/observability/test_store_contract.py -q`

Expected: PASS without converting missing values to zeros or rewriting an immutable summary.

- [ ] **Step 5: Commit observation persistence**

```bash
git add src/pilot107/observability tests/observability/test_store_contract.py
git commit -m "feat: persist typed resource observations"
```

---

### Task 11: Add Leased Observation Sources and Collection Cycles

**Files:**
- Create: `src/pilot107/observability/adapters.py`
- Create: `src/pilot107/observability/collector.py`
- Create: `tests/observability/test_adapters.py`
- Create: `tests/observability/test_collector.py`
- Create: `scripts/smoke-observability-live.sh`
- Modify: `src/pilot107/adapters/ssh_relay.py`
- Modify: `src/pilot107/core/control_repository.py`
- Modify: `src/pilot107/worker/service.py`
- Modify: `src/pilot107/worker/runtime_worker.py`
- Modify: `simulator/compose/compose.yml`

**Interfaces:**
- Consumes: Task 10 store, `ControlRepository` leases, typed Slurm REST/CLI/metrics reads.
- Produces: `ObservationSourceAdapter` and `ObservabilityCollector.tick(connection_id)`.

- [ ] **Step 1: Write source fallback, lease takeover, and budget tests**

```python
def test_failed_sstat_produces_unavailable_not_zero(collector: ObservabilityCollector) -> None:
    cycle = collector.collect_active_runs(CONNECTION)
    measure = cycle.run_samples[0].used.max_rss
    assert measure.availability == "not_collected"
    assert measure.value is None
```

- [ ] **Step 2: Run adapter and collector tests**

Run: `uv run pytest tests/observability/test_adapters.py tests/observability/test_collector.py -q`

Expected: FAIL because no collector exists.

- [ ] **Step 3: Implement four collection lanes**

Implement capability at 5 minutes, platform/account pulse at 20 seconds, active Run sampling at 30 seconds, and terminal accounting immediately with bounded retry. Batch by connection and account; prioritize terminal accounting; enforce minimum interval, maximum commands/minute, maximum concurrent requests, command deadline, batch size, and failure backoff.

- [ ] **Step 4: Verify one writer per connection and simulator accounting profile**

Run: `uv run pytest tests/observability -q && bash scripts/smoke-observability-live.sh`

Expected: PASS for lease takeover, partial cycles, stale preservation, `sstat` availability semantics, `sacct` terminal convergence, and no browser-driven Slurm calls.

- [ ] **Step 5: Commit the collector**

```bash
git add src/pilot107/observability/adapters.py src/pilot107/observability/collector.py src/pilot107/adapters/ssh_relay.py src/pilot107/core/control_repository.py src/pilot107/worker/service.py src/pilot107/worker/runtime_worker.py simulator/compose/compose.yml tests/observability/test_adapters.py tests/observability/test_collector.py scripts/smoke-observability-live.sh
git commit -m "feat: collect leased resource observations"
```

---

### Task 12: Derive Resource Evaluations and Expose Facts to Users and Agent

**Files:**
- Create: `src/pilot107/observability/evaluator.py`
- Create: `src/pilot107/observability/service.py`
- Create: `src/pilot107/api/observability_routes.py`
- Create: `tests/observability/test_evaluator.py`
- Create: `tests/test_observability_api.py`
- Create: `apps/web/src/RunResourcePanel.tsx`
- Create: `apps/web/src/RunResourcePanel.test.tsx`
- Modify: `src/pilot107/agent/read_tools.py`
- Modify: `src/pilot107/agent/tool_gateway.py`
- Modify: `src/pilot107/api/asgi_app.py`
- Modify: `apps/web/src/ResourceDashboard.tsx`

**Interfaces:**
- Consumes: Tasks 10–11 facts and A1 read-only tools.
- Produces: deterministic evaluations, observability APIs/SSE, UI panels, and tools `platform_observation_get`, `account_observation_get`, and `run_resources_get`.

- [ ] **Step 1: Write rule sufficiency and owner-scope tests**

```python
def test_multitask_maxrss_cannot_trigger_memory_overallocated(evaluator: ResourceEvaluator) -> None:
    evaluations = evaluator.evaluate(MULTITASK_MAXRSS_ONLY_SUMMARY)
    assert "MEMORY_OVERALLOCATED" not in {item.rule_id for item in evaluations}
```

- [ ] **Step 2: Run evaluator/API tests**

Run: `uv run pytest tests/observability/test_evaluator.py tests/test_observability_api.py -q`

Expected: FAIL because evaluator and APIs are absent.

- [ ] **Step 3: Implement evidence-bounded rules and read surfaces**

Implement CPU under 20% only for terminal Runs at least 10 minutes with complete `TotalCPU/CPUTimeRAW`; memory under 30% only with reliable job-level peak; GPU under 20% only at 80% or greater coverage; walltime under 20% as low confidence until three comparable Runs; and queue congestion from pulse trends. Suggested Contract patches remain proposals and never auto-submit.

- [ ] **Step 4: Run full observation, Agent-tool, and Web tests**

Run: `uv run pytest tests/observability tests/test_observability_api.py tests/agent/test_read_tools.py -q && npm --prefix apps/web test -- --run RunResourcePanel && npm --prefix apps/web run typecheck`

Expected: PASS with provenance/freshness/warnings visible and other-owner job details absent.

- [ ] **Step 5: Commit the resource-observation vertical slice**

```bash
git add src/pilot107/observability/evaluator.py src/pilot107/observability/service.py src/pilot107/api/observability_routes.py src/pilot107/agent/read_tools.py src/pilot107/agent/tool_gateway.py src/pilot107/api/asgi_app.py apps/web/src/RunResourcePanel.tsx apps/web/src/RunResourcePanel.test.tsx apps/web/src/ResourceDashboard.tsx tests/observability/test_evaluator.py tests/test_observability_api.py
git commit -m "feat: expose resource observation facts"
```

**Resource Observation Gate:** D1 must prove partial/stale/unsupported semantics, one collector lease, bounded source commands, terminal summary Evidence binding, CPU/memory/GPU/walltime sufficiency rules, and Alice/Bob isolation.

---

### Task 13: Persist AgentTask and AgentResourceEnvelope

**Files:**
- Create: `src/pilot107/agent/tasks.py`
- Create: `src/pilot107/agent/task_store.py`
- Create: `src/pilot107/agent/postgres_task_store.py`
- Create: `tests/agent/test_task_store_contract.py`
- Modify: `src/pilot107/agent/store_factory.py`
- Modify: `src/pilot107/agent/session.py`

**Interfaces:**
- Consumes: Task 1 AgentTask schema, project/workspace IDs, Run IDs, existing AgentSession/Turn IDs.
- Produces: `AgentTaskRecord`, `AgentResourceEnvelope`, and `AgentTaskStore` with lease/fencing/cancel semantics.

- [ ] **Step 1: Write lifecycle, envelope, and stale-writer contracts**

```python
def test_task_cannot_exceed_approved_envelope(store: AgentTaskStore) -> None:
    with pytest.raises(ResourceEnvelopeExceeded):
        store.create_task(owner="alice", request=GPU4_REQUEST, envelope=GPU1_ENVELOPE)
```

- [ ] **Step 2: Run the dual-store contract**

Run: `uv run pytest tests/agent/test_task_store_contract.py -q`

Expected: FAIL because AgentTask types do not exist.

- [ ] **Step 3: Implement immutable requests and monotonic task transitions**

The envelope binds partition/QoS, CPU, memory, GPU type/count, walltime, maximum tasks, maximum submissions, workspace snapshot digest, expiry, and approving actor. Task transitions allow pending→running→terminal/auth_required and idempotent cancellation; stale fencing tokens cannot write terminal results.

- [ ] **Step 4: Run SQLite/PostgreSQL and concurrency contracts**

Run: `uv run pytest tests/agent/test_task_store_contract.py -q`

Expected: PASS with duplicate request keys returning one task and one Run reference.

- [ ] **Step 5: Commit AgentTask persistence**

```bash
git add src/pilot107/agent/tasks.py src/pilot107/agent/task_store.py src/pilot107/agent/postgres_task_store.py src/pilot107/agent/store_factory.py src/pilot107/agent/session.py tests/agent/test_task_store_contract.py
git commit -m "feat: persist bounded agent tasks"
```

---

### Task 14: Dispatch Asynchronous Slurm Validation and Resume Agent Turns

**Files:**
- Create: `src/pilot107/services/agent_task_service.py`
- Create: `tests/agent/test_agent_task_service.py`
- Create: `tests/agent/test_a3_vertical.py`
- Create: `scripts/smoke-pilot-agent-a3-live.sh`
- Modify: `src/pilot107/worker/runtime_worker.py`
- Modify: `src/pilot107/worker/service.py`
- Modify: `src/pilot107/core/run_service.py`
- Modify: `src/pilot107/agent/tool_gateway.py`
- Modify: `src/pilot107/services/agent_session_service.py`
- Modify: `apps/web/src/AgentProjectPanel.tsx`

**Interfaces:**
- Consumes: Task 13 tasks/envelopes, Task 6 workspace, existing Run outbox/Evidence, A1 Turn events.
- Produces: `validation_schedule` tool, durable `agent.task.execute` outbox, task→Run linkage, Evidence injection, and follow-up Turn enqueue.

- [ ] **Step 1: Write Turn-release and single-resume tests**

```python
def test_pending_validation_releases_turn_and_terminal_task_wakes_once(harness: A3Harness) -> None:
    turn, task = harness.schedule_validation()
    assert turn.state == AgentTurnState.COMPLETED
    assert task.state == AgentTaskState.PENDING
    harness.finish_linked_run_and_dispatch_twice(task.task_id)
    assert harness.followup_turn_count(task.task_id) == 1
```

- [ ] **Step 2: Run A3 service/vertical tests**

Run: `uv run pytest tests/agent/test_agent_task_service.py tests/agent/test_a3_vertical.py -q`

Expected: FAIL because validation scheduling is not registered.

- [ ] **Step 3: Implement schedule, dispatch, reconcile, and resume**

Persist the AgentTask before returning its ID; materialize validation from the approved workspace snapshot; create exactly one linked Run through the existing `run.submit` outbox; release Pi immediately; on terminal collection, bind Evidence references and enqueue exactly one follow-up Turn. Authentication loss maps to `auth_required` without losing the task.

- [ ] **Step 4: Run crash barriers and D1 pending/running/terminal smoke**

Run: `uv run pytest tests/agent/test_a3_vertical.py tests/test_submission_outbox.py tests/test_collection_outbox.py -q && bash scripts/smoke-pilot-agent-a3-live.sh`

Expected: PASS for Worker crash before/after Run creation, browser disconnect, long PENDING, cancel, auth pause, and no duplicate submit/resume.

- [ ] **Step 5: Commit A3 vertical slice**

```bash
git add src/pilot107/services/agent_task_service.py src/pilot107/worker/runtime_worker.py src/pilot107/worker/service.py src/pilot107/core/run_service.py src/pilot107/agent/tool_gateway.py src/pilot107/services/agent_session_service.py apps/web/src/AgentProjectPanel.tsx tests/agent/test_agent_task_service.py tests/agent/test_a3_vertical.py scripts/smoke-pilot-agent-a3-live.sh
git commit -m "feat: run asynchronous agent validation"
```

**A3 Gate:** D1 must show PENDING/RUNNING/terminal progress while no Pi Turn remains resident, one linked validation Run, one resume Turn, bounded resources, cancellation, Evidence injection, browser reconnect, and Worker/Agentd restart recovery.

---

### Task 15: Add Artifact-Aware DAG and Array Recovery

**Files:**
- Create: `src/pilot107/core/workflow_manifest.py`
- Create: `tests/test_workflow_manifest.py`
- Create: `tests/test_pipeline_recovery.py`
- Create: `scripts/smoke-experiment-pipeline-live.sh`
- Modify: `src/pilot107/core/run_store.py`
- Modify: `src/pilot107/core/run_service.py`
- Modify: `src/pilot107/core/materializer.py`
- Modify: `src/pilot107/worker/runtime_worker.py`
- Modify: `apps/web/src/types.ts`
- Modify: `apps/web/src/RunEvidencePanel.tsx`

**Interfaces:**
- Consumes: existing `WorkflowPolicy`, Task 12 resource facts, Task 14 AgentTask/Run linkage, array templates, and expected-output Evidence.
- Produces: `WorkflowManifest`, `WorkflowStage`, `ArtifactTruth`, atomic stage/job decisions, dependency-layer peak validation, and missing-array-task recovery.

- [ ] **Step 1: Write DAG resource and artifact-truth tests**

```python
def test_recovery_resubmits_only_missing_array_tasks(service: WorkflowService) -> None:
    manifest = service.reconcile(MANIFEST_WITH_TASKS_0_7_AND_MISSING_8_11)
    retry = service.plan_recovery(manifest.workflow_id, actor="alice")
    assert retry.array_expression == "8-11"
    assert retry.reuses_verified_tasks == tuple(range(8))

def test_dependency_layer_peak_is_checked_before_submit(service: WorkflowService) -> None:
    with pytest.raises(WorkflowResourceLimitExceeded):
        service.validate(DAG_WITH_TWO_GPU4_ARRAYS_IN_SAME_LAYER, GPU4_CEILING)
```

- [ ] **Step 2: Run workflow and recovery tests**

Run: `uv run pytest tests/test_workflow.py tests/test_workflow_manifest.py tests/test_pipeline_recovery.py -q`

Expected: FAIL because the workflow manifest and artifact-aware recovery service do not exist.

- [ ] **Step 3: Implement persistent DAG decisions and fail-closed gates**

Persist stage definitions, dependencies, materialized request digests, job IDs, array task sets, artifact/metadata/COMPLETE truth, reuse decisions, and recovery attempts in one versioned manifest. Aggregate `throttle × resource-per-task` across each dependency layer before submit. A merge/gate cannot advance with missing or invalid task truth; resume/cancel/status all consume the same manifest.

- [ ] **Step 4: Run unit tests and the D1 preflight→array→merge smoke**

Run: `uv run pytest tests/test_workflow.py tests/test_workflow_manifest.py tests/test_pipeline_recovery.py tests/test_scan_array_artifacts.py -q && bash scripts/smoke-experiment-pipeline-live.sh`

Expected: PASS for afterok dependencies, bounded retry, atomic manifest decisions, fail-closed merge, missing-task-only recovery, cancellation, and repeated idempotent resume.

- [ ] **Step 5: Commit artifact-aware workflow recovery**

```bash
git add src/pilot107/core/workflow_manifest.py src/pilot107/core/run_store.py src/pilot107/core/run_service.py src/pilot107/core/materializer.py src/pilot107/worker/runtime_worker.py apps/web/src/types.ts apps/web/src/RunEvidencePanel.tsx tests/test_workflow_manifest.py tests/test_pipeline_recovery.py scripts/smoke-experiment-pipeline-live.sh
git commit -m "feat: recover artifact-aware experiment workflows"
```

**Workflow Gate:** D1 must demonstrate structured preflight→array→fail-closed merge, dependency-layer resource ceilings, durable job decisions, missing-task-only recovery, and identical manifest truth for status, cancel, and resume.

---

### Task 16: Publish Approved ChangeSets with Conflict Detection and Recovery

**Files:**
- Create: `src/pilot107/agent/publisher.py`
- Create: `tests/agent/test_workspace_publisher.py`
- Modify: `src/pilot107/agent/project_store.py`
- Modify: `src/pilot107/services/project_agent_service.py`
- Modify: `src/pilot107/adapters/ssh_relay.py`
- Modify: `src/pilot107/api/project_agent_routes.py`
- Modify: `apps/web/src/AgentProjectPanel.tsx`

**Interfaces:**
- Consumes: approved Task 6 ChangeSet, source snapshot digests, typed file-transfer relay.
- Produces: `WorkspacePublisher.prepare`, `publish`, `reconcile`, and explicit `workspace_conflict` results.

- [ ] **Step 1: Write concurrent-source-change and crash-recovery tests**

```python
def test_publish_detects_changed_remote_source(publisher: WorkspacePublisher) -> None:
    publisher.prepare(CHANGESET)
    REMOTE.write("main.py", b"user changed it")
    result = publisher.publish(CHANGESET.change_set_id, actor="alice")
    assert result.state == "conflicted"
    assert REMOTE.read("main.py") == b"user changed it"
```

- [ ] **Step 2: Run publisher tests**

Run: `uv run pytest tests/agent/test_workspace_publisher.py -q`

Expected: FAIL because the publisher does not exist.

- [ ] **Step 3: Implement prepare/commit/reconcile publication**

Re-read remote metadata/digests, stage files under an owner-approved `.107pilot/publish/<changeset_id>` directory, verify hashes, atomically rename, and persist per-file progress. A retry reconciles completed files and never overwrites a source digest changed outside the ChangeSet. Approval binds the exact ChangeSet digest and actor.

- [ ] **Step 4: Verify crash points, stale approval, and owner isolation**

Run: `uv run pytest tests/agent/test_workspace_publisher.py tests/test_ssh_relay.py tests/test_project_agent_api.py -q`

Expected: PASS with zero partial untracked overwrite and deterministic recovery after each staged/renamed/persisted boundary.

- [ ] **Step 5: Commit safe publication**

```bash
git add src/pilot107/agent/publisher.py src/pilot107/agent/project_store.py src/pilot107/services/project_agent_service.py src/pilot107/adapters/ssh_relay.py src/pilot107/api/project_agent_routes.py apps/web/src/AgentProjectPanel.tsx tests/agent/test_workspace_publisher.py
git commit -m "feat: publish approved workspace changes"
```

---

### Task 17: Finalize Contract, Formal Run, Runtime Watch, and Result Explanation

**Files:**
- Create: `tests/agent/test_a4_vertical.py`
- Create: `scripts/smoke-pilot-agent-a4-live.sh`
- Modify: `src/pilot107/services/project_agent_service.py`
- Modify: `src/pilot107/core/contracts.py`
- Modify: `src/pilot107/core/run_service.py`
- Modify: `src/pilot107/runtime_watch/service.py`
- Modify: `src/pilot107/services/agent_session_service.py`
- Modify: `apps/web/src/AgentProjectPanel.tsx`
- Modify: `apps/web/src/RuntimeWatchPanel.tsx`

**Interfaces:**
- Consumes: published Task 16 ChangeSet, A3 validation Evidence, existing Contract/Run, completed Runtime Watch.
- Produces: one approved Contract, one formal Run, live Watch link, terminal Evidence bundle, and result-explanation follow-up Turn.

- [ ] **Step 1: Write exact-approval and end-to-end lineage tests**

```python
def test_formal_run_binds_approved_changeset_contract_and_validation(harness: A4Harness) -> None:
    formal = harness.approve_and_submit()
    assert formal.contract.parent_contract_id == harness.validation_contract.contract_id
    assert formal.run.parent_run_id == harness.validation_run.run_id
    assert formal.run.lineage_reason == "agent_formal_run"
```

- [ ] **Step 2: Run A4 vertical tests**

Run: `uv run pytest tests/agent/test_a4_vertical.py -q`

Expected: FAIL because formal project finalization is not wired.

- [ ] **Step 3: Implement the approval-bound finalizer**

The approval digest covers ChangeSet, published snapshot, Contract, validation Evidence, and requested formal resources. Re-run capability/preflight at submission time; use the existing Run outbox; start Runtime Watch after job ID; bind terminal Watch/resource/Evidence facts; enqueue one explanation Turn without equating Slurm success to scientific validity.

- [ ] **Step 4: Run D1 blank-project gold path and failure path**

Run: `uv run pytest tests/agent/test_a4_vertical.py -q && bash scripts/smoke-pilot-agent-a4-live.sh`

Expected: PASS for blank project→sandbox→validation→approval→publish→formal Run→Runtime Watch→Evidence→explanation and for source conflict, failed Run, cancel, and browser/Worker restart.

- [ ] **Step 5: Commit A4 vertical slice**

```bash
git add src/pilot107/services/project_agent_service.py src/pilot107/core/contracts.py src/pilot107/core/run_service.py src/pilot107/runtime_watch/service.py src/pilot107/services/agent_session_service.py apps/web/src/AgentProjectPanel.tsx apps/web/src/RuntimeWatchPanel.tsx tests/agent/test_a4_vertical.py scripts/smoke-pilot-agent-a4-live.sh
git commit -m "feat: complete formal agent runs"
```

**A4 Gate:** The first competition demo must be reproducible in D1 with explicit approval, conflict-safe publication, a formal Run, incremental logs, resource facts, terminal Evidence, and an Agent explanation. A successful scheduler state alone cannot satisfy the result gate.

---

### Task 18: Adapt Existing Remediation to the Unified Project Lifecycle

**Files:**
- Create: `tests/agent/test_a5_repair_vertical.py`
- Create: `scripts/smoke-pilot-agent-repair-live.sh`
- Modify: `src/pilot107/services/remediation_service.py`
- Modify: `src/pilot107/services/repair_ticket_service.py`
- Modify: `src/pilot107/services/project_agent_service.py`
- Modify: `src/pilot107/agent/project.py`
- Modify: `apps/web/src/AgentPage.tsx`
- Modify: `apps/web/src/AgentCoeditPanel.tsx`

**Interfaces:**
- Consumes: failed Run, Diagnosis, Runtime Watch alerts, resource summary, existing Remediation budgets/approval, and A2–A4 project lifecycle.
- Produces: `failed_run` Project origin and `run_diagnosis_repair` profile using the same Workspace, ChangeSet, AgentTask, publish, Contract, and Run primitives.

- [ ] **Step 1: Write a failing code-repair vertical test**

```python
def test_failed_run_repair_changes_code_in_workspace_not_source(harness: RepairHarness) -> None:
    project = harness.start_from_failed_run()
    harness.agent_patch_and_validate(project)
    assert harness.cluster_source_digest() == harness.original_source_digest
    assert harness.reviewable_changeset(project).files == ["train.py"]
```

- [ ] **Step 2: Run repair integration tests**

Run: `uv run pytest tests/agent/test_a5_repair_vertical.py tests/test_remediation_service.py -q`

Expected: FAIL because Remediation cannot create a unified failed-run project.

- [ ] **Step 3: Add adapters without deleting the old state machine**

Map Remediation evidence/diagnosis/budget/approval into the unified project profile. Contract-only proposals may retain the current fast path; code changes must use isolated Workspace and ChangeSet. Derived Runs keep existing evaluation semantics and gain Watch/resource references.

- [ ] **Step 4: Run old and new remediation suites plus D1 repair smoke**

Run: `uv run pytest tests/test_remediation_service.py tests/test_repair_ticket_service.py tests/agent/test_a5_repair_vertical.py -q && bash scripts/smoke-pilot-agent-repair-live.sh`

Expected: PASS with budgets, approval, CAS/lease, expected-output evaluation, and old API compatibility intact.

- [ ] **Step 5: Commit unified repair**

```bash
git add src/pilot107/services/remediation_service.py src/pilot107/services/repair_ticket_service.py src/pilot107/services/project_agent_service.py src/pilot107/agent/project.py apps/web/src/AgentPage.tsx apps/web/src/AgentCoeditPanel.tsx tests/agent/test_a5_repair_vertical.py scripts/smoke-pilot-agent-repair-live.sh
git commit -m "feat: unify failed run repair projects"
```

---

### Task 19: Adapt Market Application and Publication to Unified Agent Sessions

**Files:**
- Create: `src/pilot107/agent/market_sessions.py`
- Create: `tests/agent/test_market_application_session.py`
- Create: `tests/agent/test_template_publication_session.py`
- Create: `scripts/smoke-pilot-agent-market-live.sh`
- Modify: `src/pilot107/core/template_market.py`
- Modify: `src/pilot107/core/run_publications.py`
- Modify: `src/pilot107/services/project_agent_service.py`
- Modify: `apps/web/src/MarketPages.tsx`
- Modify: `apps/web/src/TemplateWorkbenchPage.tsx`

**Interfaces:**
- Consumes: existing Market/Template/RunPublication transactions and A2–A4 project lifecycle.
- Produces: strong `MarketApplicationSession` branches and `TemplatePublicationSession` finalizers; direct adopt calls become internal compatibility helpers.

- [ ] **Step 1: Write strong-branch and default-private tests**

```python
def test_successful_run_creates_no_market_record_without_share_manifest(service) -> None:
    service.observe_successful_run(SUCCESSFUL_RUN)
    assert market_store.list_by_source_run(SUCCESSFUL_RUN.run_id) == []
```

Also prove curated and reference-only applications cannot call a shared untyped finalizer, and an equivalent bundle creates verification rather than a duplicate release.

- [ ] **Step 2: Run market Agent tests**

Run: `uv run pytest tests/agent/test_market_application_session.py tests/agent/test_template_publication_session.py -q`

Expected: FAIL because the session types and finalizers are absent.

- [ ] **Step 3: Implement application/publication state machines**

Register `market_application` and `template_publication` profiles. Curated application produces an editable project/ChangeSet/Contract; reference-only publication requires an explicitly shared Contract before adaptation. Publication requires field-level ShareManifest, strict sanitization, bundle digest, structured semantic-family deduplication, isolated reproduction, review, immutable release, verification feedback, versioning, and withdrawal.

- [ ] **Step 4: Run existing Market regressions and two-owner D1 flow**

Run: `uv run pytest tests/test_template_market.py tests/test_run_publications.py tests/agent/test_market_application_session.py tests/agent/test_template_publication_session.py -q && npm --prefix apps/web test -- --run Market && bash scripts/smoke-pilot-agent-market-live.sh`

Expected: PASS for publisher→review→adopter isolation, withdrawn/stale gates, no unapproved sharing, and no duplicate equivalent release.

- [ ] **Step 5: Commit A5 market unification**

```bash
git add src/pilot107/agent/market_sessions.py src/pilot107/core/template_market.py src/pilot107/core/run_publications.py src/pilot107/services/project_agent_service.py apps/web/src/MarketPages.tsx apps/web/src/TemplateWorkbenchPage.tsx tests/agent/test_market_application_session.py tests/agent/test_template_publication_session.py scripts/smoke-pilot-agent-market-live.sh
git commit -m "feat: unify agent market lifecycles"
```

**A5 Gate:** Demonstrate failed-run code repair and both market branches using the same project/workspace/task/publication primitives; preserve default privacy, exact ShareManifest, immutable releases, lineage, environment verification, and withdrawal governance.

---

### Task 20: Complete Runtime Store Selection and PostgreSQL Parity

**Files:**
- Modify: `src/pilot107/agent/store_factory.py`
- Modify: `src/pilot107/core/postgres_domain_schema.py`
- Modify: `src/pilot107/core/postgres_domain_stores.py`
- Modify: `src/pilot107/api/service.py`
- Modify: `src/pilot107/worker/service.py`
- Create: `tests/test_lifecycle_store_selection.py`
- Create: `tests/test_lifecycle_postgres_integration.py`
- Modify: `docs/operations/control_plane_security.md`
- Modify: `simulator/compose/compose.yml`

**Interfaces:**
- Consumes: every new SQLite/PostgreSQL store contract.
- Produces: one explicit database mode selecting all durable domain stores consistently in API and Worker.

- [ ] **Step 1: Write fail-closed mixed-store and migration tests**

```python
def test_postgres_mode_rejects_sqlite_lifecycle_store(config: ServiceConfig) -> None:
    config.database_mode = "postgres"
    config.runtime_watch_sqlite_path = Path("watch.db")
    with pytest.raises(ConfigurationError, match="mixed durable stores"):
        build_service(config)
```

- [ ] **Step 2: Run store-selection tests**

Run: `uv run pytest tests/test_lifecycle_store_selection.py tests/test_lifecycle_postgres_integration.py -q`

Expected: FAIL until every new store is wired into both builders.

- [ ] **Step 3: Wire one runtime database mode and ordered migrations**

API and Worker must resolve the same PostgreSQL DSN/database identity, run checksum-bound advisory-lock migrations, and select Project, AgentTask, Runtime Watch, observation, Run, Remediation, Template, and control repositories coherently. Credentials remain out of manifests, logs, events, and command argv.

- [ ] **Step 4: Run temporary PostgreSQL multi-process recovery tests**

Run: `PILOT107_TEST_POSTGRES_DSN="$TEST_DSN" uv run pytest tests/test_lifecycle_postgres_integration.py -q`

Expected: PASS for migration replay, four concurrent workers, stale fencing, API/Worker restart, and durable event/log cursor replay.

- [ ] **Step 5: Commit production store wiring**

```bash
git add src/pilot107/agent/store_factory.py src/pilot107/core/postgres_domain_schema.py src/pilot107/core/postgres_domain_stores.py src/pilot107/api/service.py src/pilot107/worker/service.py tests/test_lifecycle_store_selection.py tests/test_lifecycle_postgres_integration.py docs/operations/control_plane_security.md simulator/compose/compose.yml
git commit -m "feat: complete lifecycle postgres wiring"
```

---

### Task 21: Execute Full D0/D1 Gates and Prepare S1/R1 Acceptance

**Files:**
- Create: `scripts/accept-agent-lifecycle-source.sh`
- Create: `scripts/accept-agent-lifecycle-runtime.sh`
- Create: `scripts/accept-agent-lifecycle-s1.sh`
- Create: `scripts/accept-agent-lifecycle-r1.sh`
- Create: `docs/phase-3/agent_lifecycle_acceptance_matrix.md`
- Create: `tests/test_agent_lifecycle_acceptance.py`
- Modify: `docs/phase-3/current_status_index.md`
- Modify: `docs/phase-3/automated_execution_plan_20260716.md`
- Modify: `.github/workflows/ci.yml`

**Interfaces:**
- Consumes: all functional work and existing source/runtime acceptance infrastructure.
- Produces: revision-bound, environment-labeled acceptance reports and an explicit campus production decision.

- [ ] **Step 1: Write manifest tests for required acceptance cases**

```python
REQUIRED_D1 = {
    "blank_project_gold_path",
    "failed_run_code_repair",
    "long_pending_turn_release",
    "runtime_log_replay_and_terminal_drain",
    "resource_missingness_and_summary",
    "publish_conflict",
    "worker_agentd_browser_restart",
    "two_owner_isolation",
    "market_application_and_publication",
    "model_unavailable_deterministic_fallback",
    "large_file_metadata_only",
    "artifact_aware_array_recovery",
}
assert REQUIRED_D1 <= set(report["cases"])
```

- [ ] **Step 2: Run manifest tests and confirm missing reports**

Run: `uv run pytest tests/test_agent_lifecycle_acceptance.py -q`

Expected: FAIL until the acceptance scripts emit the required revision-bound reports.

- [ ] **Step 3: Implement four environment-specific acceptance entrypoints**

`source` runs lint/type/unit/schema/build/compose checks; `runtime` creates a clean D1 stack and executes every required case plus 100 idle Sessions, 10 concurrent Turns, 100 active Watch fixtures, and connection command/byte budgets; `s1` verifies deployment/resource ceilings/restart on the 8C/16G VM; `r1` requires explicit target, owner, approved root, and confirmation flags and executes success, exit 42, cancel, auth-expired, Evidence, Watch, and resource-availability cases. The model-unavailable case must keep deterministic Run/Evidence/Watch operations available and place generative project work in an explicit blocked state. R1 must refuse simulator endpoints and must never infer approval.

- [ ] **Step 4: Run D0/D1 and record external gates without overstating them**

Run: `bash scripts/accept-agent-lifecycle-source.sh && bash scripts/accept-agent-lifecycle-runtime.sh`

Expected: both PASS on one Git SHA. S1 and R1 remain `not_run` until their respective infrastructure and authorization are present; this state blocks campus production but does not invalidate D0/D1.

- [ ] **Step 5: Commit the acceptance pack and status update**

```bash
git add scripts/accept-agent-lifecycle-*.sh docs/phase-3/agent_lifecycle_acceptance_matrix.md docs/phase-3/current_status_index.md docs/phase-3/automated_execution_plan_20260716.md .github/workflows/ci.yml tests/test_agent_lifecycle_acceptance.py
git commit -m "test: seal the agent lifecycle candidate"
```

---

## Review and Release Gates

Every boxed gate uses the same sequence:

```text
focused red/green tests
→ full Python/Agentd/Web regression
→ migration and owner-negative tests
→ Docker live behavior where applicable
→ browser workflow where applicable
→ findings-first review
→ P0/P1 zero
→ revision-bound evidence
→ status index update
```

Required commands at each major gate:

```bash
uv run ruff check src tests scripts
uv run mypy src
uv run pytest
npm --prefix services/pilot-agentd test -- --run
npm --prefix apps/web run typecheck
npm --prefix apps/web test -- --run
npm --prefix apps/web run build
bash scripts/check-sim-core.sh
```

Additional gate ownership:

| Gate | Required objective evidence |
|---|---|
| A1 Web | event replay, cancel, reconnect, old Remediation regression |
| A2 | blank/existing multi-file ChangeSet, sandbox limits, source unchanged |
| Runtime Watch | incremental logs, rotation, restart, terminal drain, alerts, fairness |
| Resource observation | lease, `sstat`/`sacct`, missingness, coverage, immutable summary |
| A3 | Turn released while Slurm waits, one Run, one resume, envelope enforced |
| A4 | approval digest, conflict-safe publish, formal Run, Watch/Evidence/explanation |
| A5 | failed-run code repair, strong market branches, privacy/dedup/reproduction |
| PostgreSQL | all store contracts, migration replay, multi-process fencing/recovery |
| S1 | latest SHA deployed, declared resource ceilings, restart and volume recovery |
| R1 | authorized success/failure/cancel/auth-expired plus Watch/Evidence facts |
| Production | S1 and R1 passed, real identity passed, PostgreSQL passed, security/operations approval recorded |

## Explicit Non-Goals for This Plan

- Scientific correctness is not inferred from scheduler success; domain checks and user judgment remain explicit.
- The platform does not become a general remote shell, SSH proxy, package installer, or unrestricted file browser.
- Runtime Watch does not automatically cancel, patch, publish, or submit Runs.
- Resource evaluations do not automatically change Contracts or submit Runs.
- R0 probe artifacts do not satisfy R1 service-integration acceptance.
- Adoption count is not treated as template quality or success probability.

## Final Completion Criteria

The plan is complete only when a single reproducible D1 demonstration performs:

```text
natural-language goal
→ ProjectBlueprint
→ isolated multi-file AgentWorkspace
→ sandbox validation
→ asynchronous Slurm validation with released Turn
→ Evidence-bound resumed Turn
→ reviewable ChangeSet and Contract
→ explicit approval
→ conflict-safe publication
→ formal Run
→ Runtime Watch incremental logs and alerts
→ resource summary
→ terminal Evidence and explanation
→ optional failed-Run repair
→ optional private-by-default market publication/application
```

Campus production additionally requires the PostgreSQL, S1, R1, real-identity, and operations gates in Task 21; D0/D1 completion alone never changes the production decision to GO.
