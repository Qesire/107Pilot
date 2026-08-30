from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest

from pilot107.agent.postgres_task_store import PostgresAgentTaskStore
from pilot107.agent.tasks import (
    AgentResourceEnvelope,
    AgentTaskConflict,
    AgentTaskGateReceipt,
    AgentTaskRecord,
    AgentTaskRequest,
    AgentTaskResult,
)


class MutableClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 19, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value


def _create(store: PostgresAgentTaskStore, *, request_key: str) -> tuple[object, bool]:
    envelope = AgentResourceEnvelope(
        partition="debug",
        qos="normal",
        cpus=4,
        memory_mib=8192,
        gpu_type="a100",
        gpus=1,
        walltime_seconds=600,
        max_tasks=2,
        max_submissions=2,
        workspace_snapshot_digest="a" * 64,
        expires_at="2026-08-19T01:00:00Z",
        approved_by="alice",
    )
    request = AgentTaskRequest(
        partition="debug",
        qos="normal",
        cpus=2,
        memory_mib=4096,
        gpu_type="a100",
        gpus=1,
        walltime_seconds=300,
        tasks=1,
        submissions=1,
        workspace_snapshot_digest="a" * 64,
        payload={"argv": ["python3", "validate.py"]},
    )
    return store.create_task(
        owner="alice",
        session_id="session-1",
        turn_id="turn-1",
        project_id="project-1",
        workspace_id="workspace-1",
        task_kind="slurm_validation",
        request_key=request_key,
        request=request,
        envelope=envelope,
    )


def _terminal_gate(task: AgentTaskRecord) -> AgentTaskGateReceipt:
    return AgentTaskGateReceipt(
        task_id=task.task_id,
        run_id="run-1",
        run_terminal_state="completed",
        evidence_refs=("evidence-1",),
        evidence_digest="a" * 64,
        integrity_verified_at="2026-08-19T00:05:00Z",
        workspace_revision=None,
        workspace_digest="b" * 64,
        legacy_boundary=True,
        capsule_ref=None,
        capsule_state="not_required",
    )


@pytest.mark.skipif(
    not os.environ.get("PILOT107_TEST_POSTGRES_DSN")
    or os.environ.get("PILOT107_TEST_POSTGRES_ALLOW_RESET") != "1",
    reason="set a dedicated PostgreSQL DSN and explicit reset opt-in",
)
def test_postgres_store_rejects_success_without_terminal_gate() -> None:
    clock = MutableClock()
    store = PostgresAgentTaskStore(os.environ["PILOT107_TEST_POSTGRES_DSN"], clock=clock)
    with store.connect() as connection:
        connection.execute("TRUNCATE agent_tasks CASCADE")
    task, _ = _create(store, request_key="postgres-gate")
    lease = store.claim_task(task.task_id, owner="alice", worker_id="worker-a", lease_seconds=30)
    assert lease is not None

    with pytest.raises(AgentTaskConflict, match="gate"):
        store.finalize_task(
            task.task_id,
            lease=lease,
            gate_receipt=None,
            result=AgentTaskResult.succeeded(("evidence-1",)),
        )


@pytest.mark.skipif(
    not os.environ.get("PILOT107_TEST_POSTGRES_DSN")
    or os.environ.get("PILOT107_TEST_POSTGRES_ALLOW_RESET") != "1",
    reason="set a dedicated PostgreSQL DSN and explicit reset opt-in",
)
def test_postgres_store_accepts_verified_gate_with_current_fence() -> None:
    clock = MutableClock()
    store = PostgresAgentTaskStore(os.environ["PILOT107_TEST_POSTGRES_DSN"], clock=clock)
    with store.connect() as connection:
        connection.execute("TRUNCATE agent_tasks CASCADE")
    task, _ = _create(store, request_key="postgres-gate-success")
    lease = store.claim_task(task.task_id, owner="alice", worker_id="worker-a", lease_seconds=30)
    assert lease is not None
    store.link_run(task.task_id, lease=lease, run_id="run-1")
    refreshed = store.get_task(task.task_id, owner="alice")
    lease = lease.__class__(
        task_id=lease.task_id,
        owner=lease.owner,
        worker_id=lease.worker_id,
        version=refreshed.version,
        fencing_token=lease.fencing_token,
        expires_at=lease.expires_at,
    )
    result = store.finalize_task(
        task.task_id,
        lease=lease,
        gate_receipt=_terminal_gate(refreshed),
        result=AgentTaskResult.succeeded(("evidence-1",)),
    )
    assert result.state.value == "succeeded"
