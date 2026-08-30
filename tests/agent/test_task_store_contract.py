from __future__ import annotations

import json
import os
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pilot107.agent.postgres_task_store import PostgresAgentTaskStore
from pilot107.agent.store_factory import build_agent_task_store
from pilot107.agent.task_store import AgentTaskStore, SQLiteAgentTaskStore
from pilot107.agent.tasks import (
    AgentResourceEnvelope,
    AgentTaskCompletionPolicy,
    AgentTaskConflict,
    AgentTaskGateReceipt,
    AgentTaskGateState,
    AgentTaskRequest,
    AgentTaskResult,
    AgentTaskScheduleReceipt,
    AgentTaskState,
    ResourceEnvelopeExceeded,
    agent_task_payload,
)


def test_schedule_receipt_is_non_terminal_and_declares_completion_policy() -> None:
    receipt = AgentTaskScheduleReceipt(
        task_id="task-1",
        run_id="run-1",
        completion_policy=AgentTaskCompletionPolicy.EVIDENCE_REQUIRED,
        submit_state="admitted",
    )

    assert receipt.is_terminal is False
    assert receipt.completion_policy is AgentTaskCompletionPolicy.EVIDENCE_REQUIRED


def test_live_schedule_receipt_boundary_is_not_legacy() -> None:
    receipt = AgentTaskScheduleReceipt(
        task_id="task-1",
        run_id="run-1",
        completion_policy=AgentTaskCompletionPolicy.EVIDENCE_REQUIRED,
        submit_state="submitted",
        workspace_revision=1,
        workspace_digest="a" * 64,
    )

    assert receipt.workspace_revision == 1
    assert receipt.legacy_boundary is False


def test_capsule_policy_is_explicit_not_inferred_from_vm_mode() -> None:
    assert AgentTaskCompletionPolicy("evidence_required").requires_capsule is False
    assert (
        AgentTaskCompletionPolicy("evidence_and_capsule_required").requires_capsule
        is True
    )


def test_gate_receipt_carries_legacy_workspace_boundary_without_inventing_revision() -> None:
    receipt = AgentTaskGateReceipt(
        run_id="run-1",
        evidence_refs=("evidence-1",),
        evidence_digest="a" * 64,
        integrity_verified_at="2026-08-19T00:05:00Z",
        workspace_revision=None,
        workspace_digest="b" * 64,
        legacy_boundary=True,
        capsule_ref=None,
        capsule_state="not_required",
    )

    assert receipt.workspace_revision is None
    assert receipt.legacy_boundary is True
    assert receipt.capsule_state == "not_required"


def test_gate_state_preserves_legacy_terminal_wire_state() -> None:
    assert AgentTaskGateState.COMPLETED.value == "completed"
    assert AgentTaskState.SUCCEEDED.value == "succeeded"


class MutableClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 19, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, delta: timedelta) -> None:
        self.value += delta


def _envelope(**overrides: object) -> AgentResourceEnvelope:
    values: dict[str, object] = {
        "partition": "debug",
        "qos": "normal",
        "cpus": 4,
        "memory_mib": 8192,
        "gpu_type": "a100",
        "gpus": 1,
        "walltime_seconds": 600,
        "max_tasks": 2,
        "max_submissions": 2,
        "workspace_snapshot_digest": "a" * 64,
        "expires_at": "2026-08-19T01:00:00Z",
        "approved_by": "alice",
    }
    values.update(overrides)
    return AgentResourceEnvelope(**values)  # type: ignore[arg-type]


def _request(**overrides: object) -> AgentTaskRequest:
    values: dict[str, object] = {
        "partition": "debug",
        "qos": "normal",
        "cpus": 2,
        "memory_mib": 4096,
        "gpu_type": "a100",
        "gpus": 1,
        "walltime_seconds": 300,
        "tasks": 1,
        "submissions": 1,
        "workspace_snapshot_digest": "a" * 64,
        "payload": {"argv": ["python3", "validate.py"]},
    }
    values.update(overrides)
    return AgentTaskRequest(**values)  # type: ignore[arg-type]


def _create(store: AgentTaskStore, *, request_key: str = "validate-1"):
    return store.create_task(
        owner="alice",
        session_id="session-1",
        turn_id="turn-1",
        project_id="project-1",
        workspace_id="workspace-1",
        task_kind="slurm_validation",
        request_key=request_key,
        request=_request(),
        envelope=_envelope(),
    )


def exercise_agent_task_store_contract(
    store: AgentTaskStore,
    *,
    advance_clock: Callable[[timedelta], None],
) -> None:
    task, created = _create(store)
    assert created is True
    assert task.state is AgentTaskState.PENDING
    assert task.version == 0

    replay, replay_created = _create(store)
    assert replay_created is False
    assert replay.task_id == task.task_id
    assert replay.request == task.request

    with pytest.raises(AgentTaskConflict):
        store.create_task(
            owner="alice",
            session_id="session-1",
            turn_id="turn-1",
            project_id="project-1",
            workspace_id="workspace-1",
            task_kind="slurm_validation",
            request_key="validate-1",
            request=_request(cpus=3),
            envelope=_envelope(),
        )
    with pytest.raises(KeyError):
        store.get_task(task.task_id, owner="bob")

    first = store.claim_task(
        task.task_id, owner="alice", worker_id="worker-a", lease_seconds=1
    )
    assert first is not None
    assert first.fencing_token == 1
    assert store.claim_task(
        task.task_id, owner="alice", worker_id="worker-b", lease_seconds=30
    ) is None

    linked = store.link_run(task.task_id, lease=first, run_id="run-1")
    assert linked.linked_run_id == "run-1"
    assert store.link_run(task.task_id, lease=first, run_id="run-1") == linked
    with pytest.raises(AgentTaskConflict):
        store.link_run(task.task_id, lease=first, run_id="run-2")

    released = store.release_task(first)
    assert released.state is AgentTaskState.RUNNING
    assert released.lease_owner is None
    assert released.lease_expires_at is None
    second = store.claim_task(
        task.task_id, owner="alice", worker_id="worker-b", lease_seconds=30
    )
    assert second is not None
    assert second.fencing_token == 2
    with pytest.raises(AgentTaskConflict):
        store.complete_task(
            task.task_id,
            lease=first,
            result=AgentTaskResult.succeeded(("evidence-1",)),
        )

    completed = store.complete_task(
        task.task_id,
        lease=second,
        result=AgentTaskResult.succeeded(("evidence-1",)),
    )
    assert completed.state is AgentTaskState.SUCCEEDED
    assert completed.result is not None
    assert completed.result.evidence_refs == ("evidence-1",)
    assert store.complete_task(
        task.task_id,
        lease=second,
        result=AgentTaskResult.succeeded(("evidence-1",)),
    ) == completed

    cancellable, _ = _create(store, request_key="validate-cancel")
    cancelled = store.request_cancel(
        cancellable.task_id, owner="alice", expected_version=cancellable.version
    )
    assert cancelled.state is AgentTaskState.CANCELLED
    assert cancelled.cancel_requested is True
    assert store.request_cancel(
        cancellable.task_id, owner="alice", expected_version=cancelled.version
    ) == cancelled

    auth_task, _ = _create(store, request_key="validate-auth")
    auth_lease = store.claim_task(
        auth_task.task_id,
        owner="alice",
        worker_id="worker-auth",
        lease_seconds=30,
    )
    assert auth_lease is not None
    paused = store.complete_task(
        auth_task.task_id,
        lease=auth_lease,
        result=AgentTaskResult(
            status="auth_required",
            evidence_refs=(),
            error_code="AUTH.EXPIRED",
            message="re-authentication is required",
        ),
    )
    assert paused.state is AgentTaskState.AUTH_REQUIRED
    resumed = store.resume_after_auth(
        auth_task.task_id,
        owner="alice",
        expected_version=paused.version,
    )
    assert resumed.state is AgentTaskState.PENDING
    assert resumed.result is None

    running_task, _ = _create(store, request_key="validate-running-cancel")
    running_lease = store.claim_task(
        running_task.task_id,
        owner="alice",
        worker_id="worker-cancel",
        lease_seconds=30,
    )
    assert running_lease is not None
    cancelling = store.request_cancel(
        running_task.task_id,
        owner="alice",
        expected_version=running_lease.version,
    )
    assert cancelling.state is AgentTaskState.RUNNING
    assert cancelling.cancel_requested is True
    acknowledged = store.complete_task(
        running_task.task_id,
        lease=running_lease,
        result=AgentTaskResult.cancelled("worker acknowledged cancellation"),
    )
    assert acknowledged.state is AgentTaskState.CANCELLED


def test_task_cannot_exceed_approved_envelope(tmp_path: Path) -> None:
    store = SQLiteAgentTaskStore(tmp_path / "tasks.db")

    with pytest.raises(ResourceEnvelopeExceeded):
        store.create_task(
            owner="alice",
            session_id="session-1",
            turn_id="turn-1",
            project_id="project-1",
            workspace_id="workspace-1",
            task_kind="slurm_validation",
            request_key="gpu4",
            request=_request(gpus=4),
            envelope=_envelope(gpus=1),
        )


def test_expired_or_cross_owner_envelope_is_rejected(tmp_path: Path) -> None:
    clock = MutableClock()
    store = SQLiteAgentTaskStore(tmp_path / "tasks.db", clock=clock)

    with pytest.raises(ResourceEnvelopeExceeded):
        store.create_task(
            owner="alice",
            session_id="session-1",
            turn_id="turn-1",
            project_id="project-1",
            workspace_id="workspace-1",
            task_kind="slurm_validation",
            request_key="expired",
            request=_request(),
            envelope=_envelope(expires_at="2026-08-18T23:59:59Z"),
        )
    with pytest.raises(ResourceEnvelopeExceeded):
        store.create_task(
            owner="bob",
            session_id="session-1",
            turn_id="turn-1",
            project_id="project-1",
            workspace_id="workspace-1",
            task_kind="slurm_validation",
            request_key="wrong-approver",
            request=_request(),
            envelope=_envelope(),
        )


def test_pending_task_cannot_start_after_envelope_expires(tmp_path: Path) -> None:
    clock = MutableClock()
    store = SQLiteAgentTaskStore(tmp_path / "tasks.db", clock=clock)
    task, _ = store.create_task(
        owner="alice",
        session_id="session-1",
        turn_id="turn-1",
        project_id="project-1",
        workspace_id="workspace-1",
        task_kind="slurm_validation",
        request_key="expires-before-claim",
        request=_request(),
        envelope=_envelope(expires_at="2026-08-19T00:00:01Z"),
    )
    clock.advance(timedelta(seconds=2))

    replay, created = store.create_task(
        owner="alice",
        session_id="session-1",
        turn_id="turn-1",
        project_id="project-1",
        workspace_id="workspace-1",
        task_kind="slurm_validation",
        request_key="expires-before-claim",
        request=_request(),
        envelope=_envelope(expires_at="2026-08-19T00:00:01Z"),
    )

    lease = store.claim_task(
        task.task_id,
        owner="alice",
        worker_id="worker-late",
        lease_seconds=30,
    )

    assert created is False
    assert replay.task_id == task.task_id
    assert lease is None
    assert store.get_task(task.task_id, owner="alice").state is AgentTaskState.PENDING


def test_sqlite_agent_task_store_satisfies_contract(tmp_path: Path) -> None:
    clock = MutableClock()
    exercise_agent_task_store_contract(
        SQLiteAgentTaskStore(tmp_path / "tasks.db", clock=clock),
        advance_clock=clock.advance,
    )


def test_sqlite_agent_task_store_survives_reopen(tmp_path: Path) -> None:
    database = tmp_path / "tasks.db"
    clock = MutableClock()
    task, _ = _create(SQLiteAgentTaskStore(database, clock=clock))

    reopened = SQLiteAgentTaskStore(database, clock=clock).get_task(
        task.task_id, owner="alice"
    )

    assert reopened == task


def test_factory_selects_agent_task_store(tmp_path: Path) -> None:
    store = build_agent_task_store(sqlite_path=tmp_path / "tasks.db", postgres_dsn=None)

    assert isinstance(store, SQLiteAgentTaskStore)


def test_factory_selects_postgres_agent_task_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = object()
    monkeypatch.setattr(
        "pilot107.agent.store_factory.PostgresAgentTaskStore",
        lambda dsn: sentinel,
    )

    store = build_agent_task_store(
        sqlite_path=Path("unused.db"),
        postgres_dsn="postgresql://agent-task-store-test",
    )

    assert store is sentinel


def test_agent_task_record_serializes_to_the_frozen_wire_schema(tmp_path: Path) -> None:
    clock = MutableClock()
    task, _ = _create(SQLiteAgentTaskStore(tmp_path / "tasks.db", clock=clock))

    payload = agent_task_payload(task)

    from jsonschema import Draft202012Validator

    schema_path = (
        Path(__file__).resolve().parents[2]
        / "schemas"
        / "agent"
        / "v2"
        / "agent-task.schema.json"
    )
    Draft202012Validator(json.loads(schema_path.read_text())).validate(payload)
    assert "request" not in payload
    assert payload["resource_envelope"]["approved_by"] == "alice"


def test_concurrent_create_and_claim_produce_one_task_and_one_lease(
    tmp_path: Path,
) -> None:
    database = tmp_path / "tasks.db"
    clock = MutableClock()
    first_store = SQLiteAgentTaskStore(database, clock=clock)
    second_store = SQLiteAgentTaskStore(database, clock=clock)

    with ThreadPoolExecutor(max_workers=2) as executor:
        creations = list(executor.map(_create, (first_store, second_store)))

    assert len({task.task_id for task, _ in creations}) == 1
    assert sum(created for _, created in creations) == 1
    task_id = creations[0][0].task_id

    def claim(worker_and_store: tuple[str, SQLiteAgentTaskStore]):
        worker, store = worker_and_store
        return store.claim_task(
            task_id,
            owner="alice",
            worker_id=worker,
            lease_seconds=30,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        claims = list(
            executor.map(
                claim,
                (("worker-a", first_store), ("worker-b", second_store)),
            )
        )

    assert sum(lease is not None for lease in claims) == 1


@pytest.mark.skipif(
    not os.environ.get("PILOT107_TEST_POSTGRES_DSN")
    or os.environ.get("PILOT107_TEST_POSTGRES_ALLOW_RESET") != "1",
    reason="set a dedicated PostgreSQL DSN and explicit reset opt-in",
)
def test_postgres_agent_task_store_satisfies_contract() -> None:
    dsn = os.environ["PILOT107_TEST_POSTGRES_DSN"]
    clock = MutableClock()
    store = PostgresAgentTaskStore(dsn, clock=clock)
    with store.connect() as connection:
        connection.execute("TRUNCATE agent_tasks CASCADE")

    exercise_agent_task_store_contract(store, advance_clock=clock.advance)
