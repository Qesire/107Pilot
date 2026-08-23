from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from pilot107.adapters.slurm import InMemorySlurmBackend
from pilot107.agent.session import AgentSessionState, AgentTurnState
from pilot107.agent.store import SQLiteAgentSessionStore
from pilot107.agent.task_store import SQLiteAgentTaskStore
from pilot107.agent.tasks import AgentResourceEnvelope, AgentTaskRequest, AgentTaskState
from pilot107.core.control_repository import SQLiteControlRepository
from pilot107.core.run_service import RunService
from pilot107.core.run_store import RunStore
from pilot107.services.agent_session_service import AgentSessionService
from pilot107.services.agent_task_service import AgentTaskService
from pilot107.worker.runtime_worker import RuntimeReconcileWorker


def test_pending_validation_releases_turn_and_terminal_task_wakes_once(
    tmp_path: Path,
) -> None:
    database = tmp_path / "pilot107.db"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    def clock() -> datetime:
        return datetime(2026, 8, 19, tzinfo=UTC)
    control = SQLiteControlRepository(database, clock=clock)
    session_store = SQLiteAgentSessionStore(database, clock=clock)
    task_store = SQLiteAgentTaskStore(database, clock=clock)
    run_store = RunStore(database)
    backend = InMemorySlurmBackend()
    run_service = RunService(
        store=run_store,
        backend=backend,
        control_repository=control,
        dispatcher_id="run-worker",
        submission_retry_delay_seconds=0,
        clock=clock,
    )
    session_service = AgentSessionService(
        store=session_store,
        control_repository=control,
    )
    task_service = AgentTaskService(
        store=task_store,
        session_store=session_store,
        session_service=session_service,
        run_service=run_service,
        control_repository=control,
        workspace_resolver=lambda owner, workspace_id, digest: workspace,
        worker_id="task-worker",
        lease_seconds=30,
    )
    worker = RuntimeReconcileWorker(
        service=run_service,
        agent_task_service=task_service,
        worker_id="runtime-worker",
    )
    session, _ = session_service.create_session(
        owner="alice",
        request_key="a3-session",
        profile_id="experiment_builder",
        model_profile_id="faux-default",
        source={"project_id": "project-1", "workspace_id": "workspace-1"},
    )
    turn, _ = session_service.submit_message(
        session_id=session.session_id,
        owner="alice",
        request_key="a3-turn",
        message="validate",
        expected_state_version=session.state_version,
    )
    turn_lease = session_store.claim_turn(
        turn.turn_id,
        worker_id="turn-worker",
        lease_seconds=30,
    )
    assert turn_lease is not None
    completed_turn = session_store.complete_turn(
        turn.turn_id,
        claim=turn_lease,
        final_checkpoint={"summary": "scheduled"},
        resource_usage={},
        outcome={"status": "completed"},
    )
    task, _ = task_service.schedule_validation(
        owner="alice",
        session_id=session.session_id,
        turn_id=turn.turn_id,
        project_id="project-1",
        workspace_id="workspace-1",
        request_key="a3-validation",
        request=AgentTaskRequest(
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
            payload={"script": "#!/bin/bash\ntrue\n", "job_name": "a3-validation"},
        ),
        envelope=AgentResourceEnvelope(
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
        ),
    )

    first_tick = worker.tick()
    pending = task_store.get_task(task.task_id, owner="alice")
    run = run_store.get_run(pending.linked_run_id or "missing")

    assert completed_turn.state is AgentTurnState.COMPLETED
    assert session_store.get_session(
        session.session_id, owner="alice"
    ).state is AgentSessionState.IDLE
    assert pending.state is AgentTaskState.RUNNING
    assert pending.lease_owner is None
    assert first_tick.agent_tasks_succeeded >= 1
    assert run.job_id is not None

    backend.advance_job(job_id=run.job_id, raw_state="COMPLETED", exit_code="0:0")
    second_tick = worker.tick()
    completed_task = task_store.get_task(task.task_id, owner="alice")
    followups = [
        item
        for item in session_store.list_recoverable_turns(limit=10)
        if item.request_key == f"agent-task:{task.task_id}:ready"
    ]

    assert second_tick.agent_tasks_succeeded >= 1
    assert completed_task.state is AgentTaskState.SUCCEEDED
    assert len(followups) == 1
