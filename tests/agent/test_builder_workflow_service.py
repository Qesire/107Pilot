from __future__ import annotations

import hashlib
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

import pytest

from pilot107.adapters.slurm import InMemorySlurmBackend
from pilot107.agent.builder_workflow import (
    BuilderPhase,
    BuilderSubmissionRecord,
    BuilderSubmissionState,
)
from pilot107.agent.project import (
    ProjectBlueprint,
    ProjectContractIntent,
    ProjectFile,
    ProjectValidation,
    blueprint_payload,
)
from pilot107.agent.project_store import SQLiteProjectStore
from pilot107.agent.sandbox import SandboxExecutionResult
from pilot107.agent.store import SQLiteAgentSessionStore
from pilot107.agent.task_store import SQLiteAgentTaskStore
from pilot107.agent.tasks import AgentResourceEnvelope
from pilot107.agent.tool_gateway import AgentToolGatewayError
from pilot107.core.control_repository import SQLiteControlRepository
from pilot107.core.run_service import RunService
from pilot107.core.run_store import RunStore
from pilot107.services.agent_session_service import AgentSessionService
from pilot107.services.agent_task_service import AgentTaskService
from pilot107.services.builder_workflow_service import BuilderWorkflowService
from pilot107.services.project_agent_service import ProjectAgentService


class BuilderHarness:
    def __init__(self, tmp_path: Path, *, envelope_digest: str | None = None) -> None:
        self.clock = lambda: datetime(2026, 8, 29, tzinfo=UTC)
        self.database = tmp_path / "builder-context.db"
        self.store = SQLiteProjectStore(self.database, clock=self.clock)
        self.sandbox = ScriptedSandbox(self.store)
        self.projects = ProjectAgentService(
            store=self.store,
            workspace_root=tmp_path / "workspaces",
            sandbox=self.sandbox,  # type: ignore[arg-type]
        )
        created = self.projects.create_project(
            owner="alice",
            origin="blank",
            goal="solve a bounded heat-diffusion experiment",
            request_key="context-project",
        )
        self.project = created.project
        self.workspace = created.workspace
        self.envelope = AgentResourceEnvelope(
            partition="CPU-RC",
            qos="normal",
            cpus=4,
            memory_mib=4096,
            gpu_type=None,
            gpus=0,
            walltime_seconds=600,
            max_tasks=1,
            max_submissions=1,
            workspace_snapshot_digest=(
                envelope_digest or self.workspace.snapshot.digest
            ),
            expires_at="2026-08-30T00:00:00Z",
            approved_by="alice",
        )
        self.control = SQLiteControlRepository(self.database, clock=self.clock)
        self.session_store = SQLiteAgentSessionStore(self.database, clock=self.clock)
        self.task_store = SQLiteAgentTaskStore(self.database, clock=self.clock)
        self.session_service = AgentSessionService(
            store=self.session_store,
            control_repository=self.control,
        )
        self.session, _ = self.session_service.create_session(
            owner="alice",
            request_key="builder-session",
            profile_id="experiment_builder",
            model_profile_id="faux-default",
            source={
                "project_id": self.project.project_id,
                "workspace_id": self.workspace.workspace_id,
                "resource_envelope": asdict(self.envelope),
            },
        )
        self.session_id = self.session.session_id
        self.turn, _ = self.session_service.submit_message(
            session_id=self.session_id,
            owner="alice",
            request_key="builder-turn",
            message="build and validate the experiment",
            expected_state_version=self.session.state_version,
        )
        self.task_service = AgentTaskService(
            store=self.task_store,
            session_store=self.session_store,
            session_service=self.session_service,
            run_service=RunService(
                store=RunStore(self.database),
                backend=InMemorySlurmBackend(),
                control_repository=self.control,
                dispatcher_id="builder-run-worker",
                submission_retry_delay_seconds=0,
                clock=self.clock,
            ),
            control_repository=self.control,
            workspace_resolver=lambda owner, workspace_id, digest: Path(
                self.workspace.local_root
            ),
            worker_id="builder-task-worker",
        )
        self.workflow = BuilderWorkflowService(
            project_service=self.projects,
            store=self.store,
            envelope_resolver=self.resolve_envelope,
            agent_task_service=self.task_service,
            clock=self.clock,
        )

    def resolve_envelope(self, owner: str, session_id: str) -> AgentResourceEnvelope:
        assert owner == "alice"
        if session_id != self.session_id:
            raise KeyError(session_id)
        return self.envelope


class ScriptedSandbox:
    def __init__(self, store: SQLiteProjectStore) -> None:
        self.store = store
        self.exit_code = 0
        self.calls = 0

    def execute(
        self,
        workspace,
        *,
        argv: tuple[str, ...],
        timeout: int | float,
        change_set_id: str | None = None,
    ) -> SandboxExecutionResult:
        del timeout
        self.calls += 1
        stdout = "sandbox ok\n" if self.exit_code == 0 else ""
        stderr = "compile failed\n" if self.exit_code else ""
        result = SandboxExecutionResult(
            result_id=f"sandbox-{self.calls}",
            argv=argv,
            status="succeeded" if self.exit_code == 0 else "failed",
            exit_code=self.exit_code,
            stdout=stdout,
            stderr=stderr,
            stdout_sha256=hashlib.sha256(stdout.encode()).hexdigest(),
            stderr_sha256=hashlib.sha256(stderr.encode()).hexdigest(),
            limit_reason=None,
        )
        assert change_set_id is not None
        self.store.append_sandbox_result(
            change_set_id,
            owner=workspace.owner,
            result=result.persistence_record(),
        )
        return result


def _blueprint() -> ProjectBlueprint:
    return ProjectBlueprint(
        goal="validate a heat diffusion experiment",
        entrypoints=("scripts/run_experiment.sh",),
        files=(
            ProjectFile(
                path="main.py",
                purpose="sandbox validation",
                classification="editable",
            ),
            ProjectFile(
                path="scripts/run_experiment.sh",
                purpose="Slurm entrypoint",
                classification="editable",
            ),
        ),
        validations=(
            ProjectValidation(
                validation_id="sandbox-check",
                execution="sandbox",
                argv=("python", "main.py"),
                expected_outputs=(),
            ),
            ProjectValidation(
                validation_id="slurm-validation",
                execution="slurm",
                argv=("bash", "scripts/run_experiment.sh"),
                expected_outputs=("results/summary.json",),
            ),
        ),
        contract_intent=ProjectContractIntent(
            recipe_version_id="recipe_python_cpu@1.0.0",
            resource_hints={
                "partition": "CPU-RC",
                "qos": "normal",
                "cpus_per_task": 4,
                "memory_mib": 2048,
                "gpus": 0,
                "time_limit": "00:05:00",
            },
        ),
        expected_outputs=(),
        dependencies=(),
        open_questions=(),
    )


def _valid_build(harness: BuilderHarness, *, request_key: str = "build-1"):
    return {
        "project_id": harness.project.project_id,
        "workspace_id": harness.workspace.workspace_id,
        "session_id": harness.session_id,
        "turn_id": harness.turn.turn_id,
        "request_key": request_key,
        "expected_project_version": 1,
        "expected_workspace_snapshot_digest": harness.workspace.snapshot.digest,
        "base_change_set_id": None,
        "blueprint": blueprint_payload(_blueprint()),
        "patches": [
            {
                "path": "main.py",
                "expected_source_digest": None,
                "operation": "create",
                "content": "print('sandbox')\n",
            },
            {
                "path": "scripts/run_experiment.sh",
                "expected_source_digest": None,
                "operation": "create",
                "content": "#!/bin/bash\npython main.py\n",
            },
        ],
    }


def _repair_build(
    harness: BuilderHarness,
    failed_result,
    *,
    request_key: str,
    content: str,
):
    target = Path(harness.workspace.local_root, "main.py")
    return {
        "project_id": harness.project.project_id,
        "workspace_id": harness.workspace.workspace_id,
        "session_id": harness.session_id,
        "turn_id": harness.turn.turn_id,
        "request_key": request_key,
        "expected_project_version": harness.store.get_project(
            harness.project.project_id, owner="alice"
        ).version,
        "expected_workspace_snapshot_digest": harness.workspace.snapshot.digest,
        "base_change_set_id": failed_result.result["change_set_id"],
        "blueprint": blueprint_payload(_blueprint()),
        "patches": [
            {
                "path": "main.py",
                "expected_source_digest": hashlib.sha256(target.read_bytes()).hexdigest(),
                "operation": "modify",
                "content": content,
            }
        ],
    }


def test_context_returns_live_manifest_envelope_phase_and_next_action(
    tmp_path: Path,
) -> None:
    harness = BuilderHarness(tmp_path)
    source = Path(harness.workspace.local_root, "src", "heat2d.c")
    source.parent.mkdir()
    source.write_text("int main(void) { return 0; }\n")

    result = harness.workflow.context(
        owner="alice",
        project_id=harness.project.project_id,
        workspace_id=harness.workspace.workspace_id,
        session_id=harness.session_id,
    )

    assert result.result["phase"] == "drafting"
    assert result.result["next_action"] == "builder_build_submit"
    assert result.result["project"] == {
        "version": 1,
        "goal": harness.project.goal,
        "blueprint": None,
    }
    assert result.result["resource_envelope"] == {
        "partition": "CPU-RC",
        "qos": "normal",
        "cpus": 4,
        "memory_mib": 4096,
        "gpu_type": None,
        "gpus": 0,
        "walltime_seconds": 600,
        "max_tasks": 1,
        "max_submissions": 1,
        "workspace_snapshot_digest": harness.workspace.snapshot.digest,
        "expires_at": "2026-08-30T00:00:00Z",
    }
    assert result.result["manifest"] == {
        "items": [
            {
                "path": "src/heat2d.c",
                "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "size_bytes": source.stat().st_size,
            }
        ],
        "truncated": False,
    }
    serialized = repr(result.result)
    assert "local_root" not in serialized
    assert "approved_by" not in serialized
    assert "owner" not in result.result


def test_context_derives_failed_phase_from_latest_durable_submission(
    tmp_path: Path,
) -> None:
    harness = BuilderHarness(tmp_path)
    harness.store.create_builder_submission(
        BuilderSubmissionRecord(
            submission_id="builder-submission-failed",
            owner="alice",
            session_id=harness.session_id,
            turn_id="turn-builder",
            project_id=harness.project.project_id,
            workspace_id=harness.workspace.workspace_id,
            request_key="build-failed",
            input_digest="a" * 64,
            phase=BuilderPhase.SANDBOX_FAILED,
            state=BuilderSubmissionState.SANDBOX_FAILED,
            version=1,
            base_change_set_id=None,
            change_set_id="changeset-failed",
            sandbox_result_id="sandbox-failed",
            task_id=None,
            receipt={"status": "repair_required", "exit_code": 1},
            created_at="2026-08-29T00:00:00Z",
            updated_at="2026-08-29T00:01:00Z",
        )
    )

    result = harness.workflow.context(
        owner="alice",
        project_id=harness.project.project_id,
        workspace_id=harness.workspace.workspace_id,
        session_id=harness.session_id,
    )

    assert result.result["phase"] == "sandbox_failed"
    assert result.result["next_action"] == "builder_build_submit"
    assert result.result["last_submission"]["change_set_id"] == "changeset-failed"
    assert result.result["last_submission"]["receipt"]["status"] == "repair_required"


def test_context_fails_when_binding_or_snapshot_is_not_approved(tmp_path: Path) -> None:
    harness = BuilderHarness(tmp_path)
    other = harness.projects.create_project(
        owner="alice",
        origin="blank",
        goal="other",
        request_key="other-project",
    )

    with pytest.raises(AgentToolGatewayError) as binding_error:
        harness.workflow.context(
            owner="alice",
            project_id=other.project.project_id,
            workspace_id=harness.workspace.workspace_id,
            session_id=harness.session_id,
        )
    assert binding_error.value.code == "AGENT.BUILDER.BINDING_INVALID"

    mismatched = BuilderHarness(tmp_path / "mismatch", envelope_digest="f" * 64)
    with pytest.raises(AgentToolGatewayError) as snapshot_error:
        mismatched.workflow.context(
            owner="alice",
            project_id=mismatched.project.project_id,
            workspace_id=mismatched.workspace.workspace_id,
            session_id=mismatched.session_id,
        )
    assert snapshot_error.value.code == "AGENT.BUILDER.SNAPSHOT_INVALID"


def test_context_handler_accepts_only_injected_scope(tmp_path: Path) -> None:
    harness = BuilderHarness(tmp_path)
    handler = harness.workflow.build_tool_handlers()["builder_context_get"]
    arguments = {
        "project_id": harness.project.project_id,
        "workspace_id": harness.workspace.workspace_id,
        "session_id": harness.session_id,
    }

    assert handler("alice", arguments).result["phase"] == "drafting"
    with pytest.raises(AgentToolGatewayError) as error:
        handler("alice", {**arguments, "owner": "alice"})
    assert error.value.code == "AGENT.TOOL.INVALID"


def test_submit_returns_repair_receipt_and_does_not_schedule_on_sandbox_failure(
    tmp_path: Path,
) -> None:
    harness = BuilderHarness(tmp_path)
    harness.sandbox.exit_code = 1

    result = harness.workflow.submit("alice", _valid_build(harness))

    assert result.result["status"] == "repair_required"
    assert result.result["phase"] == "sandbox_failed"
    assert result.result["next_action"] == "builder_build_submit"
    assert str(result.result["change_set_id"]).startswith("changeset-")
    assert result.result["diagnostics"]["exit_code"] == 1
    assert result.result["diagnostics"]["stderr"] == "compile failed\n"
    assert harness.task_store.list_tasks(
        owner="alice", session_id=harness.session_id
    ) == []


def test_submit_derives_resources_and_schedules_once_after_sandbox_success(
    tmp_path: Path,
) -> None:
    harness = BuilderHarness(tmp_path)
    arguments = _valid_build(harness)

    first = harness.workflow.submit("alice", arguments)
    replay = harness.workflow.submit("alice", arguments)

    assert first.result == replay.result
    assert first.result["status"] == "scheduled"
    assert first.result["phase"] == "validation_scheduled"
    assert first.result["next_action"] is None
    tasks = harness.task_store.list_tasks(owner="alice", session_id=harness.session_id)
    assert len(tasks) == 1
    assert tasks[0].request.cpus == 4
    assert tasks[0].request.memory_mib == 2048
    assert tasks[0].request.walltime_seconds == 300
    assert tasks[0].request.payload["script"] == "bash scripts/run_experiment.sh"
    assert harness.sandbox.calls == 1


def test_failed_submission_can_be_repaired_once_then_schedules(tmp_path: Path) -> None:
    harness = BuilderHarness(tmp_path)
    harness.sandbox.exit_code = 1
    failed = harness.workflow.submit("alice", _valid_build(harness))
    harness.sandbox.exit_code = 0

    repaired = harness.workflow.submit(
        "alice",
        _repair_build(
            harness,
            failed,
            request_key="build-repair-1",
            content="print('repaired')\n",
        ),
    )

    assert repaired.result["status"] == "scheduled"
    assert repaired.result["phase"] == "validation_scheduled"
    assert harness.sandbox.calls == 2
    assert len(
        harness.task_store.list_tasks(owner="alice", session_id=harness.session_id)
    ) == 1


def test_repair_rejects_stale_base_identical_content_and_post_schedule_calls(
    tmp_path: Path,
) -> None:
    stale = BuilderHarness(tmp_path / "stale")
    stale.sandbox.exit_code = 1
    failed = stale.workflow.submit("alice", _valid_build(stale))
    stale_arguments = _repair_build(
        stale,
        failed,
        request_key="stale-repair",
        content="print('repaired')\n",
    )
    stale_arguments["base_change_set_id"] = "changeset-stale"
    with pytest.raises(AgentToolGatewayError) as stale_error:
        stale.workflow.submit("alice", stale_arguments)
    assert stale_error.value.code == "AGENT.BUILDER.NO_PROGRESS"

    identical = BuilderHarness(tmp_path / "identical")
    identical.sandbox.exit_code = 1
    failed = identical.workflow.submit("alice", _valid_build(identical))
    with pytest.raises(AgentToolGatewayError) as identical_error:
        identical.workflow.submit(
            "alice",
            _repair_build(
                identical,
                failed,
                request_key="identical-repair",
                content="print('sandbox')\n",
            ),
        )
    assert identical_error.value.code == "AGENT.BUILDER.NO_PROGRESS"

    scheduled = BuilderHarness(tmp_path / "scheduled")
    completed = scheduled.workflow.submit("alice", _valid_build(scheduled))
    with pytest.raises(AgentToolGatewayError) as scheduled_error:
        scheduled.workflow.submit(
            "alice",
            _repair_build(
                scheduled,
                completed,
                request_key="post-schedule",
                content="print('late')\n",
            ),
        )
    assert scheduled_error.value.code == "AGENT.BUILDER.NO_PROGRESS"


def test_builder_allows_at_most_three_repairs_per_turn(tmp_path: Path) -> None:
    harness = BuilderHarness(tmp_path)
    harness.sandbox.exit_code = 1
    failed = harness.workflow.submit("alice", _valid_build(harness))
    for index in range(1, 4):
        failed = harness.workflow.submit(
            "alice",
            _repair_build(
                harness,
                failed,
                request_key=f"repair-{index}",
                content=f"print('repair {index}')\n",
            ),
        )
        assert failed.result["status"] == "repair_required"

    with pytest.raises(AgentToolGatewayError) as error:
        harness.workflow.submit(
            "alice",
            _repair_build(
                harness,
                failed,
                request_key="repair-4",
                content="print('repair 4')\n",
            ),
        )
    assert error.value.code == "AGENT.BUILDER.NO_PROGRESS"
    assert harness.sandbox.calls == 4


def test_replay_recovers_after_task_creation_without_duplicate_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = BuilderHarness(tmp_path)
    original_replace = harness.store.replace_builder_submission
    crashed = False

    def crash_before_scheduled_receipt(record, *, expected_version):
        nonlocal crashed
        if record.state is BuilderSubmissionState.SCHEDULED and not crashed:
            crashed = True
            raise RuntimeError("simulated crash before scheduled receipt")
        return original_replace(record, expected_version=expected_version)

    monkeypatch.setattr(
        harness.store,
        "replace_builder_submission",
        crash_before_scheduled_receipt,
    )
    arguments = _valid_build(harness)
    with pytest.raises(RuntimeError, match="simulated crash"):
        harness.workflow.submit("alice", arguments)

    replay = harness.workflow.submit("alice", arguments)

    assert replay.result["status"] == "scheduled"
    assert harness.sandbox.calls == 1
    assert len(
        harness.task_store.list_tasks(owner="alice", session_id=harness.session_id)
    ) == 1


def test_same_request_key_with_different_content_is_rejected(tmp_path: Path) -> None:
    harness = BuilderHarness(tmp_path)
    arguments = _valid_build(harness)
    harness.workflow.submit("alice", arguments)
    changed = _valid_build(harness)
    changed["patches"][0]["content"] = "print('different')\n"

    with pytest.raises(AgentToolGatewayError) as error:
        harness.workflow.submit("alice", changed)

    assert error.value.code == "AGENT.BUILDER.IDEMPOTENCY_CONFLICT"
