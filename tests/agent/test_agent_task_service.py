from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pilot107.adapters.slurm import InMemorySlurmBackend
from pilot107.agent.project import (
    ProjectBlueprint,
    ProjectContractIntent,
    ProjectValidation,
)
from pilot107.agent.store import SQLiteAgentSessionStore
from pilot107.agent.task_store import SQLiteAgentTaskStore
from pilot107.agent.tasks import AgentResourceEnvelope, AgentTaskRequest, AgentTaskState
from pilot107.agent.tool_gateway import AgentToolGatewayError
from pilot107.core.control_repository import SQLiteControlRepository
from pilot107.core.run_service import RunService
from pilot107.core.run_store import RunStore
from pilot107.core.states import RunState
from pilot107.services.agent_session_service import AgentSessionService
from pilot107.services.agent_task_service import AgentTaskService


class MutableClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 19, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


def _request() -> AgentTaskRequest:
    return AgentTaskRequest(
        partition="debug",
        qos="normal",
        cpus=1,
        memory_mib=1024,
        gpu_type=None,
        gpus=0,
        walltime_seconds=300,
        tasks=1,
        submissions=1,
        workspace_snapshot_digest="a" * 64,
        payload={
            "script": "#!/bin/bash\nprintf 'validated\\n'\n",
            "job_name": "agent-validation",
        },
    )


def _envelope() -> AgentResourceEnvelope:
    return AgentResourceEnvelope(
        partition="debug",
        qos="normal",
        cpus=1,
        memory_mib=1024,
        gpu_type=None,
        gpus=0,
        walltime_seconds=300,
        max_tasks=1,
        max_submissions=1,
        workspace_snapshot_digest="a" * 64,
        expires_at="2026-08-19T01:00:00Z",
        approved_by="alice",
    )


class Harness:
    def __init__(
        self,
        tmp_path: Path,
        *,
        profile_id: str = "experiment_builder",
        provenance_authority: bool = True,
    ) -> None:
        self.clock = MutableClock()
        self.database = tmp_path / "pilot107.db"
        self.workspace = tmp_path / "workspace"
        self.workspace.mkdir()
        (self.workspace / "validate.py").write_text("print('snapshot validation')\n")
        self.control = SQLiteControlRepository(self.database, clock=self.clock)
        self.session_store = SQLiteAgentSessionStore(self.database, clock=self.clock)
        self.task_store = SQLiteAgentTaskStore(self.database, clock=self.clock)
        self.run_store = RunStore(self.database)
        self.backend = InMemorySlurmBackend()
        self.run_service = RunService(
            store=self.run_store,
            backend=self.backend,
            control_repository=self.control,
            dispatcher_id="run-worker",
            submission_retry_delay_seconds=0,
            clock=self.clock,
        )
        self.session_service = AgentSessionService(
            store=self.session_store,
            control_repository=self.control,
        )
        self.service = AgentTaskService(
            store=self.task_store,
            session_store=self.session_store,
            session_service=self.session_service,
            run_service=self.run_service,
            control_repository=self.control,
            workspace_resolver=lambda owner, workspace_id, digest: self.workspace,
            provenance_authority_resolver=(
                (
                    lambda owner, workspace_id, digest: (
                        "workspace-source-1",
                        "snapshot:platform-1",
                    )
                )
                if provenance_authority
                else None
            ),
            worker_id="task-worker",
            lease_seconds=30,
        )
        self.session, _ = self.session_service.create_session(
            owner="alice",
            request_key="session-a3",
            profile_id=profile_id,
            model_profile_id="faux-default",
            source={
                "project_id": "project-1",
                "workspace_id": "workspace-1",
                **(
                    {
                        "run_id": "run-failed",
                        "remediation_session_id": "remsession-repair",
                    }
                    if profile_id == "run_diagnosis_repair"
                    else {}
                ),
            },
        )
        turn, _ = self.session_service.submit_message(
            session_id=self.session.session_id,
            owner="alice",
            request_key="initial-turn",
            message="validate the workspace",
            expected_state_version=self.session.state_version,
        )
        claim = self.session_store.claim_turn(
            turn.turn_id,
            worker_id="turn-worker",
            lease_seconds=30,
        )
        assert claim is not None
        self.session_store.complete_turn(
            turn.turn_id,
            claim=claim,
            final_checkpoint={"summary": "validation scheduled"},
            resource_usage={},
            outcome={"status": "completed"},
        )
        self.turn_id = turn.turn_id

    def schedule(self):
        return self.service.schedule_validation(
            owner="alice",
            session_id=self.session.session_id,
            turn_id=self.turn_id,
            project_id="project-1",
            workspace_id="workspace-1",
            request_key="validation-1",
            request=_request(),
            envelope=_envelope(),
        )


def test_schedule_dispatches_one_linked_run_and_releases_processing_lease(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    task, created = harness.schedule()
    replay, replay_created = harness.schedule()

    batch = harness.service.dispatch_due(limit=10)
    persisted = harness.task_store.get_task(task.task_id, owner="alice")

    assert created is True
    assert replay_created is False
    assert replay.task_id == task.task_id
    assert batch.checked == 1
    assert batch.succeeded == 1
    assert persisted.state is AgentTaskState.RUNNING
    assert persisted.linked_run_id is not None
    assert persisted.lease_owner is None
    runs, _ = harness.run_store.list_runs_page(owner="alice")
    assert len(runs) == 1
    assert "SLURM_TMPDIR" in runs[0].script
    assert "validate.py" in runs[0].script
    assert "cHJpbnQoJ3NuYXBzaG90IHZhbGlkYXRpb24nKQo=" in runs[0].script
    assert runs[0].resource_plan["memory_unit"] == "M"
    assert harness.run_service.enqueue_submission(
        persisted.linked_run_id
    ).state == "pending"


def test_agent_task_run_persists_provenance_without_inventing_values(tmp_path: Path) -> None:
    harness = Harness(tmp_path, provenance_authority=True)
    request = replace(
        _request(),
        payload={
            **_request().payload,
            "workspace_revision": 7,
            "source_revision": "workspace-source-1",
            "platform_snapshot_ref": "snapshot:platform-1",
        },
    )
    task, _ = harness.service.schedule_validation(
        owner="alice",
        session_id=harness.session.session_id,
        turn_id=harness.turn_id,
        project_id="project-1",
        workspace_id="workspace-1",
        request_key="validation-provenance",
        request=request,
        envelope=_envelope(),
    )

    harness.service.dispatch_due(limit=10)
    persisted = harness.task_store.get_task(task.task_id, owner="alice")
    assert persisted.linked_run_id is not None
    run = harness.run_store.get_run(persisted.linked_run_id)
    assert run.workspace_revision is None
    assert run.workspace_digest == request.workspace_snapshot_digest
    assert run.source_revision == "workspace-source-1"
    assert run.platform_snapshot_ref == "snapshot:platform-1"
    assert run.resource_plan["workspace_snapshot_digest"] == request.workspace_snapshot_digest
    assert run.resource_plan["workspace_revision"] is None
    assert run.resource_plan["source_revision"] == "workspace-source-1"
    assert run.resource_plan["platform_snapshot_ref"] == "snapshot:platform-1"


def test_agent_task_does_not_copy_model_provenance_fields_into_run(tmp_path: Path) -> None:
    harness = Harness(tmp_path, provenance_authority=False)
    request = replace(
        _request(),
        payload={
            **_request().payload,
            "source_revision": "model-spoof",
            "platform_snapshot_ref": "model-spoof",
        },
    )
    task, _ = harness.service.schedule_validation(
        owner="alice",
        session_id=harness.session.session_id,
        turn_id=harness.turn_id,
        project_id="project-1",
        workspace_id="workspace-1",
        request_key="validation-model-provenance",
        request=request,
        envelope=_envelope(),
    )

    batch = harness.service.dispatch_due(limit=10)
    assert batch.succeeded == 0
    assert batch.errors
    persisted = harness.task_store.get_task(task.task_id, owner="alice")
    assert persisted.linked_run_id is None


def test_repair_profile_uses_the_same_bounded_validation_lifecycle(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path, profile_id="run_diagnosis_repair")

    task, created = harness.schedule()

    assert created is True
    assert task.project_id == "project-1"
    assert task.workspace_id == "workspace-1"


def test_terminal_validation_wakes_exactly_one_followup_turn(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    task, _ = harness.schedule()
    harness.service.dispatch_due(limit=10)
    persisted = harness.task_store.get_task(task.task_id, owner="alice")
    assert persisted.linked_run_id is not None
    run_id = persisted.linked_run_id
    harness.run_service.dispatch_due_submissions(limit=10)
    run = harness.run_store.get_run(run_id)
    assert run.job_id is not None
    harness.backend.advance_job(
        job_id=run.job_id,
        raw_state="COMPLETED",
        exit_code="0:0",
    )
    harness.run_service.reconcile_once(run_id)

    first = harness.service.reconcile_active(limit=10)
    ready = harness.service.dispatch_due(limit=10)
    second = harness.service.reconcile_active(limit=10)
    completed = harness.task_store.get_task(task.task_id, owner="alice")
    followups = [
        turn
        for turn in harness.session_store.list_recoverable_turns(limit=10)
        if turn.request_key == f"agent-task:{task.task_id}:ready"
    ]

    assert first.succeeded == 1
    assert ready.succeeded == 1
    assert second.succeeded == 0
    assert completed.state is AgentTaskState.SUCCEEDED
    assert completed.result is not None
    assert completed.result.evidence_refs == (f"run:{run_id}",)
    assert len(followups) == 1


def test_crash_after_run_creation_reuses_the_same_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = Harness(tmp_path)
    task, _ = harness.schedule()
    original_prepare = harness.run_service.prepare
    crashed = False

    def prepare_then_crash(*args, **kwargs):
        nonlocal crashed
        run = original_prepare(*args, **kwargs)
        if not crashed:
            crashed = True
            raise RuntimeError("simulated crash after durable Run creation")
        return run

    monkeypatch.setattr(harness.run_service, "prepare", prepare_then_crash)
    first = harness.service.dispatch_due(limit=1)
    harness.clock.advance(1)
    second = harness.service.dispatch_due(limit=1)
    persisted = harness.task_store.get_task(task.task_id, owner="alice")
    runs, _ = harness.run_store.list_runs_page(owner="alice")

    assert len(first.errors) == 1
    assert second.succeeded == 1
    assert len(runs) == 1
    assert persisted.linked_run_id == runs[0].run_id


def test_validation_tool_uses_server_approved_envelope_and_terminates_turn(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    handler = harness.service.build_tool_handler(
        lambda owner, session_id: _envelope()
    )
    arguments: dict[str, object] = {
        "project_id": "project-1",
        "workspace_id": "workspace-1",
        "session_id": harness.session.session_id,
        "turn_id": harness.turn_id,
        "request_key": "validation-tool",
        "cpus": 1,
        "memory_mib": 1024,
        "gpus": 0,
        "walltime_seconds": 300,
        "tasks": 1,
        "submissions": 1,
        "script": "#!/bin/bash\ntrue\n",
        "job_name": "agent-validation",
    }

    result = handler("alice", arguments)

    assert result.result["state"] == "pending"
    assert result.result["terminate"] is True
    task = harness.task_store.get_task(str(result.result["task_id"]), owner="alice")
    assert task.resource_envelope == _envelope()
    assert task.request.partition == "debug"
    assert task.request.workspace_snapshot_digest == "a" * 64


def test_validation_tool_returns_stable_resource_envelope_error(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    handler = harness.service.build_tool_handler(
        lambda owner, session_id: _envelope()
    )
    arguments: dict[str, object] = {
        "project_id": "project-1",
        "workspace_id": "workspace-1",
        "session_id": harness.session.session_id,
        "turn_id": harness.turn_id,
        "request_key": "validation-too-large",
        "cpus": 2,
        "memory_mib": 1024,
        "gpus": 0,
        "walltime_seconds": 300,
        "tasks": 1,
        "submissions": 1,
        "script": "true\n",
        "job_name": "agent-validation",
    }

    with pytest.raises(AgentToolGatewayError) as error:
        handler("alice", arguments)

    assert error.value.code == "AGENT.TOOL.RESOURCE_ENVELOPE_EXCEEDED"


def test_blueprint_validation_derives_bounded_request_without_model_scheduler_fields(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    blueprint = ProjectBlueprint(
        goal="validate",
        entrypoints=("scripts/run.sh",),
        files=(),
        validations=(
            ProjectValidation(
                validation_id="sandbox",
                execution="sandbox",
                argv=("python", "validate.py"),
                expected_outputs=(),
            ),
            ProjectValidation(
                validation_id="slurm",
                execution="slurm",
                argv=("bash", "scripts/run.sh", "--mode", "accurate result"),
                expected_outputs=("results/summary.json",),
            ),
        ),
        contract_intent=ProjectContractIntent(
            recipe_version_id=None,
            resource_hints={
                "partition": "debug",
                "qos": "normal",
                "cpus_per_task": 1,
                "memory_mib": 512,
                "gpus": 0,
                "time_limit": "00:04:00",
            },
        ),
        expected_outputs=(),
        dependencies=(),
        open_questions=(),
    )

    task, created = harness.service.schedule_blueprint_validation(
        owner="alice",
        session_id=harness.session.session_id,
        turn_id=harness.turn_id,
        project_id="project-1",
        workspace_id="workspace-1",
        request_key="blueprint-validation",
        blueprint=blueprint,
        envelope=_envelope(),
    )

    assert created is True
    assert task.request.cpus == 1
    assert task.request.memory_mib == 512
    assert task.request.walltime_seconds == 240
    assert task.request.tasks == 1
    assert task.request.submissions == 1
    assert task.request.payload == {
        "script": "bash scripts/run.sh --mode 'accurate result'",
        "job_name": "slurm",
        "expected_outputs": ["results/summary.json"],
    }


def test_run_uses_authorized_cluster_workdir_not_local_snapshot(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    cluster_workdir = Path("/public/home/alice")
    harness.service.run_workdir_resolver = lambda owner: cluster_workdir
    harness.schedule()

    harness.service.dispatch_due(limit=10)

    runs, _ = harness.run_store.list_runs_page(owner="alice")
    assert len(runs) == 1
    assert runs[0].workdir == str(cluster_workdir)
    assert str(harness.workspace) not in runs[0].script


def test_snapshot_materialization_rejects_symbolic_links(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    (harness.workspace / "unsafe-link").symlink_to(harness.workspace / "validate.py")
    harness.schedule()

    batch = harness.service.dispatch_due(limit=1)

    assert len(batch.errors) == 1
    runs, _ = harness.run_store.list_runs_page(owner="alice")
    assert runs == []


def test_running_task_cancellation_cancels_run_and_wakes_once(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    task, _ = harness.schedule()
    harness.service.dispatch_due(limit=10)
    harness.run_service.dispatch_due_submissions(limit=10)
    running = harness.task_store.get_task(task.task_id, owner="alice")

    harness.service.request_cancel(
        task.task_id,
        owner="alice",
        expected_version=running.version,
    )
    harness.service.reconcile_active(limit=10)
    harness.service.dispatch_due(limit=10)

    cancelled = harness.task_store.get_task(task.task_id, owner="alice")
    assert cancelled.state is AgentTaskState.CANCELLED
    assert cancelled.linked_run_id is not None
    assert harness.run_store.get_run(cancelled.linked_run_id).state.value == "CANCELLED"
    followups = [
        turn
        for turn in harness.session_store.list_recoverable_turns(limit=10)
        if turn.request_key == f"agent-task:{task.task_id}:ready"
    ]
    assert len(followups) == 1


def test_auth_pause_can_resume_same_linked_run_without_duplicate_submit(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    task, _ = harness.schedule()
    harness.service.dispatch_due(limit=10)
    harness.run_service.dispatch_due_submissions(limit=10)
    running = harness.task_store.get_task(task.task_id, owner="alice")
    assert running.linked_run_id is not None
    harness.run_store.update_state(
        running.linked_run_id,
        RunState.AUTH_REQUIRED,
        event_type="test.auth_required",
    )
    harness.service.reconcile_active(limit=10)
    harness.service.dispatch_due(limit=10)
    paused = harness.task_store.get_task(task.task_id, owner="alice")
    assert paused.state is AgentTaskState.AUTH_REQUIRED
    auth_followups = [
        turn
        for turn in harness.session_store.list_recoverable_turns(limit=10)
        if turn.request_key == f"agent-task:{task.task_id}:auth:{paused.version}"
    ]
    assert len(auth_followups) == 1
    auth_claim = harness.session_store.claim_turn(
        auth_followups[0].turn_id,
        worker_id="turn-worker",
        lease_seconds=30,
    )
    assert auth_claim is not None
    harness.session_store.complete_turn(
        auth_followups[0].turn_id,
        claim=auth_claim,
        final_checkpoint={"summary": "authentication requested"},
        resource_usage={},
        outcome={"status": "completed"},
    )

    resumed = harness.service.resume_after_auth(
        task.task_id,
        owner="alice",
        expected_version=paused.version,
    )
    harness.service.dispatch_due(limit=10)
    rerunning = harness.task_store.get_task(task.task_id, owner="alice")
    assert resumed.state is AgentTaskState.PENDING
    assert rerunning.state is AgentTaskState.RUNNING
    runs, _ = harness.run_store.list_runs_page(owner="alice")
    assert len(runs) == 1
    assert rerunning.linked_run_id == running.linked_run_id
    run = harness.run_store.get_run(running.linked_run_id)
    assert run.job_id is not None
    harness.backend.advance_job(
        job_id=run.job_id,
        raw_state="COMPLETED",
        exit_code="0:0",
    )
    harness.run_service.reconcile_once(run.run_id)
    harness.service.reconcile_active(limit=10)
    harness.service.dispatch_due(limit=10)
    completed = harness.task_store.get_task(task.task_id, owner="alice")
    terminal_followups = [
        turn
        for turn in harness.session_store.list_recoverable_turns(limit=10)
        if turn.request_key == f"agent-task:{task.task_id}:ready"
    ]
    assert completed.state is AgentTaskState.SUCCEEDED
    assert len(terminal_followups) == 1
