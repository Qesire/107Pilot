"""D1 smoke for durable asynchronous Agent validation on Docker Slurm."""

from __future__ import annotations

import json
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pilot107.adapters.slurm import (
    DockerComposeExecutor,
    DockerComposeTarget,
    DockerSimulatorCommandBackend,
)
from pilot107.agent.session import AgentSessionState, AgentTurnState
from pilot107.agent.store import SQLiteAgentSessionStore
from pilot107.agent.task_store import SQLiteAgentTaskStore
from pilot107.agent.tasks import AgentResourceEnvelope, AgentTaskRequest, AgentTaskState
from pilot107.core.control_repository import SQLiteControlRepository
from pilot107.core.run_service import RunService
from pilot107.core.run_store import RunStore
from pilot107.core.states import RunState
from pilot107.services.agent_session_service import AgentSessionService
from pilot107.services.agent_task_service import AgentTaskService
from pilot107.worker.runtime_worker import RuntimeReconcileWorker


def _request(*, digest: str, sleep_seconds: int, name: str) -> AgentTaskRequest:
    return AgentTaskRequest(
        partition="Students",
        qos="qos_stu_medium_2gpu",
        cpus=1,
        memory_mib=128,
        gpu_type=None,
        gpus=0,
        walltime_seconds=60,
        tasks=1,
        submissions=1,
        workspace_snapshot_digest=digest,
        payload={
            "script": (
                "#!/bin/bash\n"
                "test \"$(python3 marker.py)\" = snapshot-ok\n"
                f"sleep {sleep_seconds}\n"
                "printf 'validation-complete\\n'\n"
            ),
            "job_name": name,
        },
    )


def _envelope(*, digest: str) -> AgentResourceEnvelope:
    return AgentResourceEnvelope(
        partition="Students",
        qos="qos_stu_medium_2gpu",
        cpus=1,
        memory_mib=128,
        gpu_type=None,
        gpus=0,
        walltime_seconds=60,
        max_tasks=1,
        max_submissions=1,
        workspace_snapshot_digest=digest,
        expires_at=(datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
        approved_by="alice",
    )


def _completed_turn(
    session_service: AgentSessionService,
    session_store: SQLiteAgentSessionStore,
    *,
    key: str,
) -> tuple[str, str]:
    session, _ = session_service.create_session(
        owner="alice",
        request_key=f"{key}-session",
        profile_id="experiment_builder",
        model_profile_id="faux-default",
        source={"project_id": "project-1", "workspace_id": "workspace-1"},
    )
    turn, _ = session_service.submit_message(
        session_id=session.session_id,
        owner="alice",
        request_key=f"{key}-turn",
        message="run bounded validation",
        expected_state_version=session.state_version,
    )
    lease = session_store.claim_turn(turn.turn_id, worker_id="agentd-d1", lease_seconds=30)
    if lease is None:
        raise RuntimeError("D1 Turn was not claimable")
    completed = session_store.complete_turn(
        turn.turn_id,
        claim=lease,
        final_checkpoint={"summary": "validation scheduled"},
        resource_usage={},
        outcome={"status": "completed"},
    )
    if completed.state is not AgentTurnState.COMPLETED:
        raise RuntimeError("D1 Turn did not release before Slurm validation")
    return session.session_id, turn.turn_id


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    compose_dir = root / "simulator" / "compose"
    executor = DockerComposeExecutor(
        DockerComposeTarget(
            compose_file=compose_dir / "compose.yml",
            env_file=compose_dir / ".env.example",
            workdir=compose_dir,
        )
    )
    backend = DockerSimulatorCommandBackend(
        executor=executor,
        allowed_roots=["/public/home/alice"],
        timeout_seconds=20,
    )
    with tempfile.TemporaryDirectory(prefix="pilot107-a3-d1-") as temporary:
        temporary_root = Path(temporary)
        database = temporary_root / "pilot107.db"
        workspace = temporary_root / "workspace"
        workspace.mkdir()
        (workspace / "marker.py").write_text("print('snapshot-ok')\n", encoding="utf-8")
        digest = "d" * 64

        control = SQLiteControlRepository(database)
        run_store = RunStore(database)
        session_store = SQLiteAgentSessionStore(database)
        task_store = SQLiteAgentTaskStore(database)
        session_service = AgentSessionService(
            store=session_store,
            control_repository=control,
        )

        def build_runtime() -> tuple[AgentTaskService, RuntimeReconcileWorker]:
            run_service = RunService(
                store=RunStore(database),
                backend=backend,
                control_repository=SQLiteControlRepository(database),
                dispatcher_id="a3-d1-run-worker",
                submission_retry_delay_seconds=0,
            )
            task_service = AgentTaskService(
                store=SQLiteAgentTaskStore(database),
                session_store=SQLiteAgentSessionStore(database),
                session_service=AgentSessionService(
                    store=SQLiteAgentSessionStore(database),
                    control_repository=SQLiteControlRepository(database),
                ),
                run_service=run_service,
                control_repository=SQLiteControlRepository(database),
                workspace_resolver=lambda owner, workspace_id, snapshot: workspace,
                run_workdir_resolver=lambda owner: Path("/public/home/alice"),
                worker_id="a3-d1-task-worker",
                lease_seconds=30,
            )
            return task_service, RuntimeReconcileWorker(
                service=run_service,
                agent_task_service=task_service,
                worker_id="a3-d1-runtime-worker",
            )

        task_service, worker = build_runtime()
        session_id, turn_id = _completed_turn(
            session_service,
            session_store,
            key="primary",
        )
        task, _ = task_service.schedule_validation(
            owner="alice",
            session_id=session_id,
            turn_id=turn_id,
            project_id="project-1",
            workspace_id="workspace-1",
            request_key="primary-validation",
            request=_request(digest=digest, sleep_seconds=5, name="a3-d1-primary"),
            envelope=_envelope(digest=digest),
        )
        task_service.dispatch_due(limit=10)
        linked = task_store.get_task(task.task_id, owner="alice")
        if linked.linked_run_id is None or linked.lease_owner is not None:
            raise RuntimeError("D1 AgentTask did not durably link and release its lease")
        if session_store.get_session(session_id, owner="alice").state is not AgentSessionState.IDLE:
            raise RuntimeError("D1 Session retained a resident Turn while Slurm was pending")
        if session_store.list_recoverable_turns(limit=10):
            raise RuntimeError("D1 retained recoverable Pi work while Slurm was pending")

        # Browser/process reconnect: all visible state must survive fresh store instances.
        reconnected_task = SQLiteAgentTaskStore(database).get_task(task.task_id, owner="alice")
        reconnected_turn = SQLiteAgentSessionStore(database).get_turn(turn_id, owner="alice")
        if reconnected_task.linked_run_id != linked.linked_run_id:
            raise RuntimeError("D1 reconnect lost AgentTask to Run linkage")
        if reconnected_turn.state is not AgentTurnState.COMPLETED:
            raise RuntimeError("D1 reconnect lost released Turn state")

        states: list[str] = []
        first_tick = worker.tick()
        primary_run = run_store.get_run(linked.linked_run_id)
        states.append(primary_run.state.value)
        if primary_run.job_id is None:
            raise RuntimeError(
                "D1 validation Run was not submitted to Docker Slurm: "
                f"state={primary_run.state.value} "
                f"submission_errors={first_tick.submission_errors} "
                f"agent_task_errors={first_tick.agent_task_errors}"
            )
        if (
            primary_run.resource_plan.get("cpus_per_task") != 1
            or primary_run.resource_plan.get("memory_value") != 128
            or primary_run.resource_plan.get("gpus_total") != 0
        ):
            raise RuntimeError("D1 validation Run escaped its approved resource envelope")

        # Restart the application worker while the real Slurm job is active.
        task_service, worker = build_runtime()
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            result = worker.tick()
            primary_run = run_store.get_run(linked.linked_run_id)
            states.append(primary_run.state.value)
            current_task = SQLiteAgentTaskStore(database).get_task(task.task_id, owner="alice")
            if current_task.state in {
                AgentTaskState.SUCCEEDED,
                AgentTaskState.FAILED,
                AgentTaskState.CANCELLED,
            }:
                break
            if result.agent_task_errors:
                raise RuntimeError(f"D1 AgentTask Worker error: {result.agent_task_errors}")
            time.sleep(0.5)
        else:
            raise RuntimeError(f"D1 primary validation timed out: {states}")
        if current_task.state is not AgentTaskState.SUCCEEDED:
            raise RuntimeError(f"D1 primary validation failed: {current_task.state}")
        if RunState.RUNNING.value not in states:
            raise RuntimeError(f"D1 never observed the live RUNNING state: {states}")
        if current_task.result is None or current_task.result.evidence_refs != (
            f"run:{linked.linked_run_id}",
        ):
            raise RuntimeError("D1 terminal Run Evidence was not injected into AgentTask")
        primary_followups = [
            item
            for item in SQLiteAgentSessionStore(database).list_recoverable_turns(limit=20)
            if item.request_key == f"agent-task:{task.task_id}:ready"
        ]
        if len(primary_followups) != 1:
            raise RuntimeError("D1 terminal validation did not enqueue exactly one resume Turn")
        if len(run_store.list_runs_page(owner="alice")[0]) != 1:
            raise RuntimeError("D1 primary validation created duplicate Runs")

        cancel_session_id, cancel_turn_id = _completed_turn(
            session_service,
            session_store,
            key="cancel",
        )
        cancel_task, _ = task_service.schedule_validation(
            owner="alice",
            session_id=cancel_session_id,
            turn_id=cancel_turn_id,
            project_id="project-1",
            workspace_id="workspace-1",
            request_key="cancel-validation",
            request=_request(digest=digest, sleep_seconds=30, name="a3-d1-cancel"),
            envelope=_envelope(digest=digest),
        )
        worker.tick()
        cancel_running = SQLiteAgentTaskStore(database).get_task(
            cancel_task.task_id, owner="alice"
        )
        if cancel_running.linked_run_id is None:
            raise RuntimeError("D1 cancellation fixture did not create a linked Run")
        task_service.request_cancel(
            cancel_task.task_id,
            owner="alice",
            expected_version=cancel_running.version,
        )
        cancellation_deadline = time.monotonic() + 30
        while time.monotonic() < cancellation_deadline:
            worker.tick()
            cancelled = SQLiteAgentTaskStore(database).get_task(
                cancel_task.task_id, owner="alice"
            )
            if cancelled.state is AgentTaskState.CANCELLED:
                break
            time.sleep(0.25)
        else:
            raise RuntimeError("D1 cancellation did not reach terminal AgentTask state")
        cancelled_run = run_store.get_run(cancel_running.linked_run_id)
        if cancelled_run.state is not RunState.CANCELLED:
            raise RuntimeError("D1 AgentTask cancellation did not propagate to Slurm")
        cancel_followups = [
            item
            for item in SQLiteAgentSessionStore(database).list_recoverable_turns(limit=20)
            if item.request_key == f"agent-task:{cancel_task.task_id}:ready"
        ]
        if len(cancel_followups) != 1:
            raise RuntimeError("D1 cancellation did not enqueue exactly one resume Turn")

        report = {
            "schema": "pilot107.agent-a3-live-smoke/v1",
            "primary_task_id": task.task_id,
            "primary_run_id": linked.linked_run_id,
            "primary_job_id": primary_run.job_id,
            "states_observed": list(dict.fromkeys(states)),
            "turn_released_while_active": True,
            "browser_reconnect_recovered": True,
            "worker_restart_recovered": True,
            "primary_run_count": 1,
            "primary_resume_turn_count": 1,
            "evidence_refs": list(current_task.result.evidence_refs),
            "bounded_resources": {"cpus": 1, "memory_mib": 128, "gpus": 0},
            "cancel_task_state": cancelled.state.value,
            "cancel_run_state": cancelled_run.state.value,
            "cancel_resume_turn_count": 1,
        }
        print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
