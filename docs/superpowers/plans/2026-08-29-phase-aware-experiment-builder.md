# Phase-aware Experiment Builder Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace model-driven low-level Builder orchestration with a two-tool, phase-aware facade that deterministically persists the build, validates it, and hands one bounded task to Slurm.

**Architecture:** Add a durable Builder submission aggregate and a Python `BuilderWorkflowService` over the existing Project, Workspace, Sandbox, and AgentTask services. Expose only `builder_context_get` and `builder_build_submit` to `experiment_builder`; keep the current granular tools for other Project profiles and behind a CPU-RC rollback flag.

**Tech Stack:** Python 3.13, SQLite/PostgreSQL repositories, TypeScript, `pi-agent-core`, TypeBox, Vitest, pytest, Docker Compose, VM-local Slurm.

## Global Constraints

- Keep `PI_STEP_LIMITS.experiment_builder` at 20 and capability `max_invocations` at 32 until at least 30 representative Builder Turns have been measured.
- Fix the model to USTC-107 `deepseek-v4-flash` for VM acceptance.
- Derive Sandbox argv and Slurm command/resources from the typed Blueprint and approved Session resource envelope; the model cannot submit raw scheduler fields.
- Project and Workspace IDs are injected from Turn bindings and never appear in model-visible argument schemas.
- A successful build schedules validation only; ChangeSet publication and formal Run submission remain explicit user-approved actions.
- Preserve bubblewrap network isolation, digest guards, owner scoping, immutable Evidence, and `vm-slurm` authority.
- Do not modify or delete existing untracked acceptance artifacts.

---

### Task 1: Persist Builder workflow submissions

**Files:**
- Create: `src/pilot107/agent/builder_workflow.py`
- Modify: `src/pilot107/agent/project_store.py`
- Modify: `src/pilot107/agent/postgres_project_store.py`
- Modify: `src/pilot107/core/postgres_domain_schema.py`
- Modify: `src/pilot107/core/postgres_domain_migration.py`
- Create: `tests/agent/test_builder_submission_store_contract.py`
- Modify: `tests/test_postgres_domain_migration.py`

**Interfaces:**
- Produces: `BuilderPhase`, `BuilderSubmissionState`, `BuilderSubmissionRecord`, `BuilderSubmissionConflict`.
- Produces on `ProjectStore`: `create_builder_submission(record)`, `get_builder_submission(submission_id, owner=...)`, `get_builder_submission_by_request_key(owner, request_key)`, and `replace_builder_submission(record, expected_version=...)`.
- Consumes later: Tasks 2 and 3 derive phases and replay receipts from these records.

- [ ] **Step 1: Write the failing repository contract tests**

```python
def test_builder_submission_request_key_replays_only_identical_content(store):
    first = store.create_builder_submission(_submission(request_key="build-1"))
    replay = store.create_builder_submission(_submission(request_key="build-1"))
    assert replay == first
    with pytest.raises(BuilderSubmissionConflict):
        store.create_builder_submission(
            replace(_submission(request_key="build-1"), input_digest="f" * 64)
        )


def test_builder_submission_transition_uses_optimistic_version(store):
    created = store.create_builder_submission(_submission())
    completed = replace(
        created,
        state=BuilderSubmissionState.SANDBOX_FAILED,
        phase=BuilderPhase.SANDBOX_FAILED,
        version=2,
    )
    assert store.replace_builder_submission(completed, expected_version=1) == completed
    with pytest.raises(BuilderSubmissionConflict):
        store.replace_builder_submission(completed, expected_version=1)
```

- [ ] **Step 2: Run the tests and verify RED**

Run: `uv run pytest -q tests/agent/test_builder_submission_store_contract.py tests/test_postgres_domain_migration.py`

Expected: collection fails because `pilot107.agent.builder_workflow` and the new repository methods do not exist.

- [ ] **Step 3: Implement the immutable aggregate and SQLite migration**

```python
class BuilderPhase(StrEnum):
    DRAFTING = "drafting"
    SANDBOX_FAILED = "sandbox_failed"
    VALIDATION_SCHEDULED = "validation_scheduled"


class BuilderSubmissionState(StrEnum):
    RUNNING = "running"
    SANDBOX_FAILED = "sandbox_failed"
    SCHEDULED = "scheduled"


@dataclass(frozen=True)
class BuilderSubmissionRecord:
    submission_id: str
    owner: str
    session_id: str
    turn_id: str
    project_id: str
    workspace_id: str
    request_key: str
    input_digest: str
    phase: BuilderPhase
    state: BuilderSubmissionState
    version: int
    base_change_set_id: str | None
    change_set_id: str | None
    sandbox_result_id: str | None
    task_id: str | None
    receipt: Mapping[str, object] | None
    created_at: str
    updated_at: str
```

Add SQLite migration `006b.005.agent_builder_submissions` with unique
`(owner, request_key)`, foreign keys to Project and Workspace, closed state and
phase checks, and JSON receipt storage.

- [ ] **Step 4: Implement PostgreSQL parity and schema verification**

Add the same table to `POSTGRES_DOMAIN_SCHEMA`, repository methods using
`INSERT ... ON CONFLICT (owner, request_key) DO NOTHING`, and migration metadata
for `receipt_json`, `created_at`, and `updated_at`.

- [ ] **Step 5: Run focused and parity tests**

Run: `uv run pytest -q tests/agent/test_builder_submission_store_contract.py tests/test_project_store_contract.py tests/test_postgres_domain_migration.py`

Expected: all pass for SQLite; PostgreSQL schema tests confirm the new table and columns.

- [ ] **Step 6: Commit**

```bash
git add src/pilot107/agent/builder_workflow.py src/pilot107/agent/project_store.py src/pilot107/agent/postgres_project_store.py src/pilot107/core/postgres_domain_schema.py src/pilot107/core/postgres_domain_migration.py tests/agent/test_builder_submission_store_contract.py tests/test_postgres_domain_migration.py
git commit -m "feat: persist phase-aware builder submissions"
```

---

### Task 2: Produce compact bound Builder context

**Files:**
- Create: `src/pilot107/services/builder_workflow_service.py`
- Create: `tests/agent/test_builder_workflow_service.py`

**Interfaces:**
- Consumes: `ProjectAgentService`, `ProjectStore`, and `EnvelopeResolver`.
- Produces: `BuilderWorkflowService.context(owner, project_id, workspace_id, session_id) -> AgentReadResult`.
- Produces: `BuilderWorkflowService.build_tool_handlers() -> dict[str, AgentReadHandler]` with `builder_context_get` initially registered.

- [ ] **Step 1: Write the failing compact-context tests**

```python
def test_context_returns_live_manifest_envelope_phase_and_next_action(harness):
    result = harness.workflow.context(
        owner="alice",
        project_id=harness.project.project_id,
        workspace_id=harness.workspace.workspace_id,
        session_id=harness.session.session_id,
    )
    assert result.result["phase"] == "drafting"
    assert result.result["next_action"] == "builder_build_submit"
    assert result.result["project"] == {
        "version": 1,
        "goal": harness.project.goal,
        "blueprint": None,
    }
    assert result.result["resource_envelope"]["partition"] == "CPU-RC"
    assert "local_root" not in json.dumps(result.result)
    assert "owner" not in result.result


def test_context_fails_when_binding_or_snapshot_is_not_approved(harness):
    with pytest.raises(AgentToolGatewayError) as error:
        harness.workflow.context(
            owner="alice",
            project_id="project-other",
            workspace_id=harness.workspace.workspace_id,
            session_id=harness.session.session_id,
        )
    assert error.value.code == "AGENT.BUILDER.BINDING_INVALID"
```

- [ ] **Step 2: Run and verify RED**

Run: `uv run pytest -q tests/agent/test_builder_workflow_service.py -k context`

Expected: import failure for `BuilderWorkflowService`.

- [ ] **Step 3: Implement phase derivation and manifest collection**

Implement a 500-entry live regular-file manifest with `path`, `sha256`, and
`size_bytes`. Derive `drafting`, `sandbox_failed`, or `validation_scheduled` from
the latest durable submission. Return only Project version/goal/Blueprint,
manifest metadata, approved envelope values, `phase`, and `next_action`.

- [ ] **Step 4: Add closed handler parsing**

`builder_context_get` accepts exactly injected `project_id`, `workspace_id`, and
`session_id`. Reject any additional fields with `AGENT.TOOL.INVALID`. Map binding,
snapshot, and unavailable-envelope failures to stable redacted codes.

- [ ] **Step 5: Run focused tests**

Run: `uv run pytest -q tests/agent/test_builder_workflow_service.py -k context tests/agent/test_project_agent_service.py`

Expected: all pass without exposing host paths or credentials.

- [ ] **Step 6: Commit**

```bash
git add src/pilot107/services/builder_workflow_service.py tests/agent/test_builder_workflow_service.py
git commit -m "feat: provide compact builder workflow context"
```

---

### Task 3: Deterministically apply, Sandbox, and schedule a build

**Files:**
- Modify: `src/pilot107/services/builder_workflow_service.py`
- Modify: `src/pilot107/services/agent_task_service.py`
- Modify: `tests/agent/test_builder_workflow_service.py`
- Modify: `tests/agent/test_agent_task_service.py`

**Interfaces:**
- Produces: `BuilderWorkflowService.submit(owner, arguments) -> AgentReadResult`.
- Produces: `AgentTaskService.schedule_blueprint_validation(...) -> tuple[AgentTaskRecord, bool]`.
- Consumes: the existing `ProjectBlueprint`, atomic patch batch, Sandbox executor, approved `AgentResourceEnvelope`, and Task store.

- [ ] **Step 1: Write the failing Sandbox-failure test**

```python
def test_submit_returns_repair_receipt_and_does_not_schedule_on_sandbox_failure(harness):
    result = harness.submit(_valid_build(), sandbox_exit=1)
    assert result.result["status"] == "repair_required"
    assert result.result["phase"] == "sandbox_failed"
    assert result.result["next_action"] == "builder_build_submit"
    assert result.result["change_set_id"].startswith("changeset-")
    assert result.result["diagnostics"]["exit_code"] == 1
    assert harness.task_store.list_tasks(owner="alice") == []
```

- [ ] **Step 2: Write the failing scheduled-receipt test**

```python
def test_submit_derives_resources_and_schedules_once_after_sandbox_success(harness):
    first = harness.submit(_valid_build(request_key="build-1"), sandbox_exit=0)
    replay = harness.submit(_valid_build(request_key="build-1"), sandbox_exit=0)
    assert first.result == replay.result
    assert first.result["status"] == "scheduled"
    assert first.result["phase"] == "validation_scheduled"
    assert first.result["next_action"] is None
    tasks = harness.task_store.list_tasks(owner="alice")
    assert len(tasks) == 1
    assert tasks[0].request.cpus == 4
    assert tasks[0].request.payload["script"] == "bash scripts/run_experiment.sh"
```

- [ ] **Step 3: Run and verify RED**

Run: `uv run pytest -q tests/agent/test_builder_workflow_service.py -k submit tests/agent/test_agent_task_service.py -k blueprint`

Expected: tests fail because `submit` and `schedule_blueprint_validation` are absent.

- [ ] **Step 4: Implement Blueprint and resource derivation**

Require exactly one Sandbox validation and one Slurm validation. Convert Slurm
argv to a shell-safe script using `shlex.join`. Derive CPU/memory/GPU/walltime
from Blueprint hints with envelope defaults, require partition and QoS to match
the envelope when present, and fix tasks/submissions to one. Call
`AgentResourceEnvelope.assert_allows` before task creation.

- [ ] **Step 5: Implement initial build and repair state transitions**

For the initial build require `base_change_set_id is None` and phase `drafting`.
For repair require the latest failed ChangeSet ID and current source digests.
Persist a running submission before mutation. Save the Blueprint, apply one
atomic patch batch, hash the persisted diff, and execute the Blueprint Sandbox
argv. Persist either the bounded repair receipt or the scheduled receipt.

- [ ] **Step 6: Implement replay and crash recovery**

Canonicalize Blueprint plus patches to `input_digest`. Replaying the same
`request_key + input_digest` returns the receipt. A different digest raises
`AGENT.BUILDER.IDEMPOTENCY_CONFLICT`. If a running record already has a
ChangeSet/Sandbox/Task reference, resume from the first missing durable reference
instead of applying patches or scheduling again.

- [ ] **Step 7: Implement semantic no-progress limits**

Allow one context call, one initial submission, and at most three repairs per
Turn. Reject stale base ChangeSets, identical content, or post-schedule calls with
`AGENT.BUILDER.NO_PROGRESS` and `retryable=false`.

- [ ] **Step 8: Run focused service tests**

Run: `uv run pytest -q tests/agent/test_builder_workflow_service.py tests/agent/test_agent_task_service.py`

Expected: all pass; failure paths create no duplicate AgentTask.

- [ ] **Step 9: Commit**

```bash
git add src/pilot107/services/builder_workflow_service.py src/pilot107/services/agent_task_service.py tests/agent/test_builder_workflow_service.py tests/agent/test_agent_task_service.py
git commit -m "feat: orchestrate bounded builder validation"
```

---

### Task 4: Expose the two-tool facade to Pi

**Files:**
- Modify: `src/pilot107/agent/protocol.py`
- Modify: `src/pilot107/agent/capabilities.py`
- Modify: `src/pilot107/agent/tool_gateway.py`
- Modify: `src/pilot107/worker/agent_turn_worker.py`
- Modify: `services/pilot-agentd/src/protocol.ts`
- Modify: `services/pilot-agentd/src/project-tools.ts`
- Modify: `services/pilot-agentd/src/tasks.ts`
- Modify: `services/pilot-agentd/src/config.ts`
- Modify: `services/pilot-agentd/src/main.ts`
- Modify: `services/pilot-agentd/tests/config.test.ts`
- Modify: `services/pilot-agentd/tests/project-tools.test.ts`
- Modify: `services/pilot-agentd/tests/tasks.test.ts`
- Modify: `services/pilot-agentd/tests/turn-executor.test.ts`
- Modify: `tests/agent/test_protocol.py`
- Modify: `tests/agent/test_capabilities.py`
- Modify: `tests/agent/test_tool_gateway.py`

**Interfaces:**
- Produces: model-visible `BUILDER_WORKFLOW_TOOL_NAMES = ["builder_context_get", "builder_build_submit"]`.
- Preserves: legacy `A2_PROJECT_TOOL_NAMES` for non-Builder profiles and rollback mode.
- Consumes: the Python handlers wired in Task 5.

- [ ] **Step 1: Write failing TypeScript catalog and schema tests**

```typescript
expect(task.tools.map((tool) => tool.name)).toEqual([
  "builder_context_get",
  "builder_build_submit",
]);
expect(Value.Check(submit.parameters, {
  request_key: "build-1",
  expected_project_version: 1,
  expected_workspace_snapshot_digest: "a".repeat(64),
  base_change_set_id: null,
  blueprint: BLUEPRINT,
  patches: PATCHES,
})).toBe(true);
expect(Value.Check(submit.parameters, { ...valid, cpus: 4 })).toBe(false);
```

- [ ] **Step 2: Write failing Python authorization tests**

Assert Builder claims contain exactly the facade tools when the feature flag is
enabled, bound IDs are injected, raw IDs/resources are rejected, and other
profiles retain their current catalogs.

- [ ] **Step 3: Run and verify RED**

Run: `npm --prefix services/pilot-agentd test -- tests/project-tools.test.ts tests/tasks.test.ts tests/turn-executor.test.ts`

Run: `uv run pytest -q tests/agent/test_protocol.py tests/agent/test_capabilities.py tests/agent/test_tool_gateway.py`

Expected: catalog and schema assertions fail against the eight legacy tools.

- [ ] **Step 4: Implement protocol and capability catalogs**

Add both facade names to the wire protocol. Parse the same rollout flag in
pilot-agentd config and pass it through `createAgentdExecutor` to task
preparation. Issue only facade capabilities for `experiment_builder` when
enabled; keep legacy names for the other three Project profiles. Map both facade
tools to the existing `read/write/validate` operation set and inject Project,
Workspace, Session, and Turn bindings server-side.

- [ ] **Step 5: Implement TypeBox tool schemas and terminal behavior**

`builder_context_get` uses an empty closed schema. `builder_build_submit` uses the
existing Blueprint and WorkspacePatch schemas plus version, snapshot digest,
request key, and nullable base ChangeSet. Its tool implementation terminates
only when the returned structured result has `status === "scheduled"`; a
`repair_required` result continues the Pi loop.

- [ ] **Step 6: Replace the Builder prompt with phase instructions**

Require `builder_context_get` exactly once, then `builder_build_submit`. On
`repair_required`, patch only diagnosed files and resubmit against the returned
ChangeSet. Forbid narration, repeated context reads, raw scheduler construction,
diff probing, and post-schedule actions.

- [ ] **Step 7: Add real Pi loop integration tests**

Faux responses must prove a successful context + submit path uses two tool calls,
a one-repair path uses three, `scheduled` immediately stops Pi, and the existing
20-step emergency limit still fails a pathological loop.

- [ ] **Step 8: Run TypeScript and Python tests**

Run: `npm --prefix services/pilot-agentd test`

Run: `uv run pytest -q tests/agent/test_protocol.py tests/agent/test_capabilities.py tests/agent/test_tool_gateway.py`

Expected: all pass.

- [ ] **Step 9: Commit**

```bash
git add src/pilot107/agent/protocol.py src/pilot107/agent/capabilities.py src/pilot107/agent/tool_gateway.py src/pilot107/worker/agent_turn_worker.py services/pilot-agentd/src/protocol.ts services/pilot-agentd/src/project-tools.ts services/pilot-agentd/src/tasks.ts services/pilot-agentd/src/config.ts services/pilot-agentd/src/main.ts services/pilot-agentd/tests/config.test.ts services/pilot-agentd/tests/project-tools.test.ts services/pilot-agentd/tests/tasks.test.ts services/pilot-agentd/tests/turn-executor.test.ts tests/agent/test_protocol.py tests/agent/test_capabilities.py tests/agent/test_tool_gateway.py
git commit -m "feat: expose phase-aware builder tools"
```

---

### Task 5: Wire rollout configuration and API handlers

**Files:**
- Modify: `src/pilot107/api/service.py`
- Modify: `simulator/compose/compose.cpu-rc.yml`
- Modify: `simulator/compose/.env.cpu-rc.example`
- Modify: `tests/test_api_service.py`
- Modify: `tests/test_agentd_compose.py`
- Modify: `tests/test_cpu_rc_profile.py`
- Create: `tests/agent/test_agent_tool_gateway_api.py`

**Interfaces:**
- Produces: `PILOT107_PHASE_AWARE_BUILDER` boolean configuration, default false outside CPU-RC and true in `.env.cpu-rc.example`.
- Produces: API Tool Gateway handlers for both facade tools.

- [ ] **Step 1: Write failing configuration and API tests**

```python
def test_cpu_rc_enables_phase_aware_builder():
    env = Path("simulator/compose/.env.cpu-rc.example").read_text()
    assert "PILOT107_PHASE_AWARE_BUILDER=1" in env


def test_builder_context_route_uses_bound_scope(api_client, builder_turn):
    response = api_client.invoke_tool(
        builder_turn,
        tool_name="builder_context_get",
        arguments={},
    )
    assert response.status == 200
    assert response.payload["result"]["phase"] == "drafting"
```

- [ ] **Step 2: Run and verify RED**

Run: `uv run pytest -q tests/test_api_service.py tests/test_agentd_compose.py tests/test_cpu_rc_profile.py tests/agent/test_agent_tool_gateway_api.py`

Expected: missing config field, env setting, and handlers.

- [ ] **Step 3: Implement config parsing and service wiring**

Construct `BuilderWorkflowService` from the existing `ProjectAgentService`,
`AgentTaskService`, Project store, and Session envelope resolver. Register facade
handlers only when enabled. Pass the same flag to worker capability issuance and
pilot-agentd tool preparation.

- [ ] **Step 4: Verify disabled-mode rollback**

Add a test with the flag false that registers the existing legacy Builder
catalog and can read an already-created legacy Project without schema changes or
data conversion.

- [ ] **Step 5: Run focused deployment tests**

Run: `uv run pytest -q tests/test_api_service.py tests/test_agentd_compose.py tests/test_cpu_rc_profile.py tests/agent/test_agent_tool_gateway_api.py`

Expected: all pass in both enabled and disabled configurations.

- [ ] **Step 6: Commit**

```bash
git add src/pilot107/api/service.py simulator/compose/compose.cpu-rc.yml simulator/compose/.env.cpu-rc.example tests/test_api_service.py tests/test_agentd_compose.py tests/test_cpu_rc_profile.py tests/agent/test_agent_tool_gateway_api.py
git commit -m "feat: wire phase-aware builder rollout"
```

---

### Task 6: Add Builder efficiency metrics

**Files:**
- Modify: `services/pilot-agentd/src/turn-executor.ts`
- Modify: `services/pilot-agentd/src/protocol.ts`
- Modify: `src/pilot107/agent/protocol.py`
- Modify: `src/pilot107/api/metrics.py`
- Modify: `services/pilot-agentd/tests/turn-executor.test.ts`
- Modify: `tests/agent/test_protocol.py`
- Modify: `tests/test_asgi_app.py`

**Interfaces:**
- Produces terminal metadata: `pi_steps`, `provider_calls`, `tool_invocations`, `build_submissions`, `repair_submissions`, `no_progress_rejections`, and `terminal_phase`.
- Produces Prometheus counters/histograms labeled only by profile, tool, outcome, and phase; never owner, Project, Session, or path.

- [ ] **Step 1: Write failing terminal-metadata tests**

```typescript
expect(terminal(events)).toMatchObject({
  payload: {
    provider_calls: 2,
    pi_steps: 2,
    tool_invocations: 2,
    build_submissions: 1,
    repair_submissions: 0,
    terminal_phase: "validation_scheduled",
  },
});
```

- [ ] **Step 2: Run and verify RED**

Run: `npm --prefix services/pilot-agentd test -- tests/turn-executor.test.ts`

Run: `uv run pytest -q tests/agent/test_protocol.py tests/test_asgi_app.py -k metrics`

Expected: the new terminal fields and metrics do not exist.

- [ ] **Step 3: Accumulate and validate bounded metadata**

Count Pi `turn_start` events and completed tool results, classify initial versus
repair submissions from `base_change_set_id`, and record no-progress failures by
machine code. Extend both protocol consumers with non-negative safe integers and
a closed optional terminal phase enum.

- [ ] **Step 4: Export low-cardinality metrics**

Add totals for Builder submission outcome and no-progress rejection plus a Pi
step histogram. Do not attach user-derived labels.

- [ ] **Step 5: Run focused metrics tests and commit**

Run: `npm --prefix services/pilot-agentd test -- tests/turn-executor.test.ts`

Run: `uv run pytest -q tests/agent/test_protocol.py tests/test_asgi_app.py -k metrics`

```bash
git add services/pilot-agentd/src/turn-executor.ts services/pilot-agentd/src/protocol.ts src/pilot107/agent/protocol.py src/pilot107/api/metrics.py services/pilot-agentd/tests/turn-executor.test.ts tests/agent/test_protocol.py tests/test_asgi_app.py
git commit -m "feat: measure builder workflow efficiency"
```

---

### Task 7: Update scientific acceptance and documentation

**Files:**
- Modify: `scripts/smoke-vm-heat-diffusion-agent.py`
- Modify: `tests/agent/test_vm_heat_diffusion_smoke_contract.py`
- Create: `docs/operations/phase-aware-builder.md`
- Modify: `docs/phase-0/competition_deployment_plan.md`

**Interfaces:**
- Consumes: the two-tool Builder event contract and existing heat-output auditor.
- Produces: a smoke report containing Pi/tool counts, Builder submission receipts, AgentTask/Run IDs, scientific audit, Evidence refs, and Capsule ref.

- [ ] **Step 1: Extend the already-failing scientific smoke contract tests**

Require the exact `src/heat2d.c` and `scripts/summarize.py` contract already
added locally, then assert the smoke rejects legacy low-level Builder calls and
requires `builder_context_get`, `builder_build_submit`, `pi_steps <= 8`, one
AgentTask, and one validation Slurm Run.

- [ ] **Step 2: Run and verify RED**

Run: `uv run pytest -q tests/agent/test_vm_heat_diffusion_smoke_contract.py`

Expected: new facade and efficiency assertions fail against the current smoke.

- [ ] **Step 3: Update the smoke event audit**

Keep the natural-language Project goal and external API entry. Inspect persisted
events for the facade tools, verify no low-level Builder tool was model-called,
and include measured counts in the final JSON report. Preserve the convergence,
distinct `srun`, output, Evidence, Capsule, publication, and formal Run checks.

- [ ] **Step 4: Document rollout and rollback**

Document the feature flag, two-tool contract, semantic repair limit, 20/32
emergency ceilings, immutable release switch, health probes, and rollback to the
legacy catalog.

- [ ] **Step 5: Run focused tests and commit**

Run: `uv run pytest -q tests/agent/test_vm_heat_diffusion_smoke_contract.py`

```bash
git add scripts/smoke-vm-heat-diffusion-agent.py tests/agent/test_vm_heat_diffusion_smoke_contract.py docs/operations/phase-aware-builder.md docs/phase-0/competition_deployment_plan.md
git commit -m "test: enforce phase-aware scientific builder flow"
```

---

### Task 8: Full verification, immutable deployment, and live acceptance

**Files:**
- Generated: `artifacts/acceptance/source-<commit>-<timestamp>/`
- Generated: `artifacts/acceptance/vm-demo/`
- No source edits unless a verification failure starts a new TDD cycle.

**Interfaces:**
- Consumes: all prior tasks.
- Produces: source acceptance report, release manifest, image-binding report, VM scientific smoke report, and browser QA evidence.

- [ ] **Step 1: Run focused local suites**

Run: `uv run pytest -q tests/agent tests/test_agent_turn_worker.py tests/test_api_service.py tests/test_cpu_rc_profile.py`

Run: `npm --prefix services/pilot-agentd test`

Run: `npm test -- --run`

Expected: all pass.

- [ ] **Step 2: Run the complete source acceptance gate**

Run: `bash scripts/accept-source-release.sh`

Expected: Python, mypy, web, Playwright, image, supply-chain, and source checks all pass; any known npm audit finding is reported explicitly.

- [ ] **Step 3: Build and verify immutable CPU-RC images**

Run: `bash scripts/export-cpu-rc-bundle.sh`

Expected: the script rebuilds images for the exact accepted Git revision,
verifies required files and image metadata, then writes an immutable release
archive plus manifest under `artifacts/acceptance/`.

- [ ] **Step 4: Deploy through the existing non-interactive SSH broker**

Upload the immutable bundle to `114.214.241.31` over SSH port 8000, create a new
release directory, reuse only approved secrets/certificates/data, switch systemd,
and retain the previous release for rollback. Never place the SSH password in a
file, command argument, report, or source tree.

- [ ] **Step 5: Run VM authority and scientific acceptance**

Verify all containers match the release manifest, health endpoints return 200,
the platform snapshot authority is `vm-slurm`, and the heat-diffusion smoke
completes with `deepseek-v4-flash`, one AgentTask, one Slurm validation, passing
convergence, Evidence, Capsule, publication, and formal Run.

- [ ] **Step 6: Run live browser QA through `pilot-browser`**

Verify the deployed Agent page shows the DeepSeek profile, phase-aware tool
events without blank reply cards, the completed scientific lineage, and Files
manual path/search behavior. Follow repository `AGENTS.md`; do not invoke
`agent-browser` directly.

- [ ] **Step 7: Record verification evidence and final commit if required**

Do not commit generated acceptance artifacts unless repository policy already
tracks the exact target directory. Report release path, commit, image digests,
Agent Session/Task/Run IDs, Slurm Job ID, scientific audit, known vulnerabilities,
and rollback path.
