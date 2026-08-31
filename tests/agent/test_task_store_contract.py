from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pilot107.agent.postgres_task_store import PostgresAgentTaskStore
from pilot107.agent.store_factory import build_agent_task_store
from pilot107.agent.task_store import AgentTaskStore, SQLiteAgentTaskStore, _task_from_row
from pilot107.agent.tasks import (
    AgentResourceEnvelope,
    AgentTaskCompletionPolicy,
    AgentTaskConflict,
    AgentTaskGateReceipt,
    AgentTaskGateState,
    AgentTaskRecord,
    AgentTaskRequest,
    AgentTaskResult,
    AgentTaskScheduleReceipt,
    AgentTaskState,
    ResourceEnvelopeExceeded,
    agent_task_gate_receipt_payload,
    agent_task_payload,
    agent_task_schedule_receipt_payload,
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
        task_id="task-1",
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

    assert receipt.workspace_revision is None
    assert receipt.legacy_boundary is True
    assert receipt.capsule_state == "not_required"


def test_gate_state_preserves_legacy_terminal_wire_state() -> None:
    assert AgentTaskGateState.COMPLETED.value == "completed"
    assert AgentTaskState.SUCCEEDED.value == "succeeded"


def test_gate_receipt_rejects_unfinalized_or_unverified_facts() -> None:
    with pytest.raises(ValueError, match="evidence_state"):
        AgentTaskGateReceipt(
            task_id="task-1",
            run_id="run-1",
            evidence_refs=("evidence-1",),
            evidence_digest="a" * 64,
            integrity_verified_at="2026-08-19T00:05:00Z",
            workspace_revision=None,
            workspace_digest="b" * 64,
            legacy_boundary=True,
            capsule_ref=None,
            capsule_state="not_required",
            run_terminal_state="completed",
            evidence_state="collected",
        )
    with pytest.raises(ValueError, match="integrity_state"):
        AgentTaskGateReceipt(
            task_id="task-1",
            run_id="run-1",
            evidence_refs=("evidence-1",),
            evidence_digest="a" * 64,
            integrity_verified_at="2026-08-19T00:05:00Z",
            workspace_revision=None,
            workspace_digest="b" * 64,
            legacy_boundary=True,
            capsule_ref=None,
            capsule_state="not_required",
            run_terminal_state="completed",
            integrity_state="pending",
        )


@pytest.mark.parametrize(
    ("capsule_ref", "capsule_state"),
    [(None, "READY"), ("capsule-1", "not_required")],
)
def test_gate_receipt_requires_capsule_state_and_ref_to_agree(
    capsule_ref: str | None, capsule_state: str
) -> None:
    with pytest.raises(ValueError, match="capsule"):
        AgentTaskGateReceipt(
            task_id="task-1",
            run_id="run-1",
            evidence_refs=("evidence-1",),
            evidence_digest="a" * 64,
            integrity_verified_at="2026-08-19T00:05:00Z",
            workspace_revision=None,
            workspace_digest="b" * 64,
            legacy_boundary=True,
            capsule_ref=capsule_ref,
            capsule_state=capsule_state,
            run_terminal_state="completed",
        )


def test_gate_receipt_requires_task_and_terminal_state() -> None:
    with pytest.raises(TypeError):
        AgentTaskGateReceipt(
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


@pytest.mark.parametrize("run_terminal_state", ["completed", "failed", "cancelled", "orphaned"])
def test_gate_receipt_accepts_only_wire_run_terminal_states(
    run_terminal_state: str,
) -> None:
    receipt = AgentTaskGateReceipt(
        task_id="task-1",
        run_id="run-1",
        run_terminal_state=run_terminal_state,
        evidence_refs=("evidence-1",),
        evidence_digest="a" * 64,
        integrity_verified_at="2026-08-19T00:05:00Z",
        workspace_revision=None,
        workspace_digest="b" * 64,
        legacy_boundary=True,
        capsule_ref=None,
        capsule_state="not_required",
    )
    assert receipt.run_terminal_state == run_terminal_state


@pytest.mark.parametrize("run_terminal_state", ["running", "bogus"])
def test_gate_receipt_rejects_non_terminal_run_states(run_terminal_state: str) -> None:
    with pytest.raises(ValueError, match="run_terminal_state"):
        AgentTaskGateReceipt(
            task_id="task-1",
            run_id="run-1",
            run_terminal_state=run_terminal_state,
            evidence_refs=("evidence-1",),
            evidence_digest="a" * 64,
            integrity_verified_at="2026-08-19T00:05:00Z",
            workspace_revision=None,
            workspace_digest="b" * 64,
            legacy_boundary=True,
            capsule_ref=None,
            capsule_state="not_required",
        )


def test_task_receipt_bindings_follow_linked_run_and_admitted_boundary(
    tmp_path: Path,
) -> None:
    task, _ = _create(
        SQLiteAgentTaskStore(tmp_path / "tasks.db", clock=MutableClock())
    )
    admitted = AgentTaskScheduleReceipt(
        task_id=task.task_id,
        run_id="run-1",
        completion_policy=task.completion_policy,
        submit_state="admitted",
    )
    assert replace(task, schedule_receipt=admitted).schedule_receipt == admitted
    with pytest.raises(ValueError, match="schedule receipt task_id"):
        replace(task, schedule_receipt=replace(admitted, task_id="task-other"))
    with pytest.raises(ValueError, match="linked Run"):
        replace(task, linked_run_id="run-2", schedule_receipt=admitted)
    with pytest.raises(ValueError, match="admitted"):
        replace(task, schedule_receipt=replace(admitted, submit_state="submitted"))


def test_task_gate_receipt_requires_task_and_linked_run(tmp_path: Path) -> None:
    task, _ = _create(
        SQLiteAgentTaskStore(tmp_path / "tasks.db", clock=MutableClock())
    )
    gate = AgentTaskGateReceipt(
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
    with pytest.raises(ValueError, match="linked Run"):
        replace(task, gate_receipt=gate)
    with pytest.raises(ValueError, match="gate receipt task_id"):
        replace(task, linked_run_id="run-1", gate_receipt=replace(gate, task_id="task-other"))
    with pytest.raises(ValueError, match="gate receipt Run"):
        replace(task, linked_run_id="run-2", gate_receipt=gate)


def _terminal_gate(task: AgentTaskRecord, *, capsule: bool = False) -> AgentTaskGateReceipt:
    task_id = task.task_id
    return AgentTaskGateReceipt(
        task_id=task_id,
        run_id="run-1",
        run_terminal_state="completed",
        evidence_refs=("evidence-1",),
        evidence_digest="a" * 64,
        integrity_verified_at="2026-08-19T00:05:00Z",
        workspace_revision=None,
        workspace_digest="b" * 64,
        legacy_boundary=True,
        capsule_ref="capsule-1" if capsule else None,
        capsule_state="READY" if capsule else "not_required",
    )


def _schedule_receipt(task: AgentTaskRecord, **overrides: object) -> AgentTaskScheduleReceipt:
    request_digest = hashlib.sha256(
        json.dumps(
            {
                "partition": task.request.partition,
                "qos": task.request.qos,
                "cpus": task.request.cpus,
                "memory_mib": task.request.memory_mib,
                "gpu_type": task.request.gpu_type,
                "gpus": task.request.gpus,
                "walltime_seconds": task.request.walltime_seconds,
                "tasks": task.request.tasks,
                "submissions": task.request.submissions,
                "workspace_snapshot_digest": task.request.workspace_snapshot_digest,
                "payload": task.request.payload,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    values: dict[str, object] = {
        "task_id": task.task_id,
        "run_id": "run-1",
        "completion_policy": task.completion_policy,
        "submit_state": "admitted",
        "receipt_id": "receipt-1",
        "owner": task.owner,
        "session_id": task.session_id,
        "originating_turn_id": task.turn_id,
        "request_digest": request_digest,
        "idempotency_key": task.request_key,
        "resource_envelope_id": hashlib.sha256(
            json.dumps(
                {
                    "partition": task.resource_envelope.partition,
                    "qos": task.resource_envelope.qos,
                    "cpus": task.resource_envelope.cpus,
                    "memory_mib": task.resource_envelope.memory_mib,
                    "gpu_type": task.resource_envelope.gpu_type,
                    "gpus": task.resource_envelope.gpus,
                    "walltime_seconds": task.resource_envelope.walltime_seconds,
                    "max_tasks": task.resource_envelope.max_tasks,
                    "max_submissions": task.resource_envelope.max_submissions,
                    "workspace_snapshot_digest": task.resource_envelope.workspace_snapshot_digest,
                    "expires_at": task.resource_envelope.expires_at,
                    "approved_by": task.resource_envelope.approved_by,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest(),
        "workspace_digest": task.request.workspace_snapshot_digest,
        "workspace_revision": None,
        "legacy_boundary": True,
        "created_at": "2026-08-19T00:00:00Z",
    }
    values.update(overrides)
    return AgentTaskScheduleReceipt(**values)  # type: ignore[arg-type]


def test_store_rejects_success_without_terminal_gate(tmp_path: Path) -> None:
    store = SQLiteAgentTaskStore(tmp_path / "tasks.db", clock=MutableClock())
    task, _ = _create(store)
    lease = store.claim_task(task.task_id, owner="alice", worker_id="worker-a", lease_seconds=30)
    assert lease is not None
    linked = store.link_run(task.task_id, lease=lease, run_id="run-1")
    lease = replace(lease, version=linked.version)

    with pytest.raises(AgentTaskConflict, match="gate"):
        store.finalize_task(
            task.task_id,
            lease=lease,
            gate_receipt=None,
            result=AgentTaskResult.succeeded(("evidence-1",)),
        )


def test_new_task_is_not_marked_legacy_gate_unverified(tmp_path: Path) -> None:
    store = SQLiteAgentTaskStore(tmp_path / "tasks.db", clock=MutableClock())
    task, _ = _create(store)

    assert task.legacy_gate_unverified is False


def test_complete_task_rejects_success_without_terminal_gate(tmp_path: Path) -> None:
    store = SQLiteAgentTaskStore(tmp_path / "tasks.db", clock=MutableClock())
    task, _ = _create(store)
    lease = store.claim_task(task.task_id, owner="alice", worker_id="worker-a", lease_seconds=30)
    assert lease is not None

    with pytest.raises(AgentTaskConflict, match="gate"):
        store.complete_task(
            task.task_id,
            lease=lease,
            result=AgentTaskResult.succeeded(("evidence-1",)),
        )


def test_advance_gate_validates_candidate_before_opening_sql_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SQLiteAgentTaskStore(tmp_path / "tasks.db", clock=MutableClock())
    task, _ = _create(store)
    lease = store.claim_task(task.task_id, owner="alice", worker_id="worker-a", lease_seconds=30)
    assert lease is not None

    def fail_if_sql_is_opened() -> object:
        raise AssertionError("invalid gate candidate must be rejected before SQL")

    monkeypatch.setattr(store, "connect", fail_if_sql_is_opened)
    with pytest.raises(AgentTaskConflict, match="finalize"):
        store.advance_gate(
            task.task_id,
            lease=lease,
            gate_state="completed",  # type: ignore[arg-type]
        )


def test_stale_fence_cannot_finalize_gate(tmp_path: Path) -> None:
    clock = MutableClock()
    store = SQLiteAgentTaskStore(tmp_path / "tasks.db", clock=clock)
    task, _ = _create(store)
    first = store.claim_task(task.task_id, owner="alice", worker_id="worker-a", lease_seconds=1)
    assert first is not None
    clock.advance(timedelta(seconds=2))
    second = store.claim_task(task.task_id, owner="alice", worker_id="worker-b", lease_seconds=30)
    assert second is not None

    with pytest.raises(AgentTaskConflict):
        store.finalize_task(
            task.task_id,
            lease=first,
            gate_receipt=_terminal_gate(task),
            result=AgentTaskResult.succeeded(("evidence-1",)),
        )


def test_advance_gate_persists_receipt_and_uses_task_cas(tmp_path: Path) -> None:
    store = SQLiteAgentTaskStore(tmp_path / "tasks.db", clock=MutableClock())
    task, _ = _create(store)
    lease = store.claim_task(task.task_id, owner="alice", worker_id="worker-a", lease_seconds=30)
    assert lease is not None

    advanced = store.advance_gate(
        task.task_id,
        lease=lease,
        gate_state=AgentTaskGateState.AWAITING_EVIDENCE,
        receipt=None,
    )

    assert advanced.gate_state is AgentTaskGateState.AWAITING_EVIDENCE
    assert advanced.version == lease.version + 1
    assert (
        store.get_task(task.task_id, owner="alice").gate_state
        is AgentTaskGateState.AWAITING_EVIDENCE
    )
    with pytest.raises(AgentTaskConflict):
        store.advance_gate(
            task.task_id,
            lease=lease,
            gate_state=AgentTaskGateState.AWAITING_INTEGRITY,
            receipt=None,
        )


def test_gate_receipt_is_immutable_and_same_replay_is_idempotent(tmp_path: Path) -> None:
    store = SQLiteAgentTaskStore(tmp_path / "tasks.db", clock=MutableClock())
    task, _ = _create(store)
    lease = store.claim_task(task.task_id, owner="alice", worker_id="worker-a", lease_seconds=30)
    assert lease is not None
    linked = store.link_run(task.task_id, lease=lease, run_id="run-1")
    lease = replace(lease, version=linked.version)
    receipt = _terminal_gate(task)
    first = store.advance_gate(
        task.task_id,
        lease=lease,
        gate_state=AgentTaskGateState.AWAITING_INTEGRITY,
        receipt=receipt,
    )
    replay_lease = replace(lease, version=first.version)
    replay = store.advance_gate(
        task.task_id,
        lease=replay_lease,
        gate_state=AgentTaskGateState.AWAITING_INTEGRITY,
        receipt=receipt,
    )
    assert replay == first
    changed = replace(receipt, evidence_digest="c" * 64)
    replay_lease = replace(replay_lease, version=replay.version)
    with pytest.raises(AgentTaskConflict, match="identity"):
        store.advance_gate(
            task.task_id,
            lease=replay_lease,
            gate_state=AgentTaskGateState.AWAITING_INTEGRITY,
            receipt=changed,
        )


def test_finalize_rejects_evidence_refs_that_differ_from_gate(tmp_path: Path) -> None:
    store = SQLiteAgentTaskStore(tmp_path / "tasks.db", clock=MutableClock())
    task, _ = _create(store)
    lease = store.claim_task(task.task_id, owner="alice", worker_id="worker-a", lease_seconds=30)
    assert lease is not None
    linked = store.link_run(task.task_id, lease=lease, run_id="run-1")
    lease = replace(lease, version=linked.version)
    with pytest.raises(AgentTaskConflict, match="Evidence"):
        store.finalize_task(
            task.task_id,
            lease=lease,
            gate_receipt=_terminal_gate(task),
            result=AgentTaskResult.succeeded(("evidence-other",)),
        )


def test_advance_gate_is_monotonic_and_never_writes_completed(tmp_path: Path) -> None:
    store = SQLiteAgentTaskStore(tmp_path / "tasks.db", clock=MutableClock())
    task, _ = _create(store)
    lease = store.claim_task(task.task_id, owner="alice", worker_id="worker-a", lease_seconds=30)
    assert lease is not None
    awaiting = store.advance_gate(
        task.task_id,
        lease=lease,
        gate_state=AgentTaskGateState.AWAITING_EVIDENCE,
    )
    with pytest.raises(AgentTaskConflict, match="monotonic"):
        store.advance_gate(
            task.task_id,
            lease=replace(lease, version=awaiting.version),
            gate_state=AgentTaskGateState.CREATED,
        )
    with pytest.raises(AgentTaskConflict, match="finalize"):
        store.advance_gate(
            task.task_id,
            lease=replace(lease, version=awaiting.version),
            gate_state=AgentTaskGateState.COMPLETED,
        )


def test_terminal_finalize_replay_checks_lease_before_receipt(tmp_path: Path) -> None:
    store = SQLiteAgentTaskStore(tmp_path / "tasks.db", clock=MutableClock())
    task, _ = _create(store)
    lease = store.claim_task(task.task_id, owner="alice", worker_id="worker-a", lease_seconds=30)
    assert lease is not None
    linked = store.link_run(task.task_id, lease=lease, run_id="run-1")
    lease = replace(lease, version=linked.version)
    store.finalize_task(
        task.task_id,
        lease=lease,
        gate_receipt=_terminal_gate(task),
        result=AgentTaskResult.succeeded(("evidence-1",)),
    )
    replay = store.finalize_task(
        task.task_id,
        lease=lease,
        gate_receipt=_terminal_gate(task),
        result=AgentTaskResult.succeeded(("evidence-1",)),
    )
    assert replay.state is AgentTaskState.SUCCEEDED


def test_claim_and_renew_update_heartbeat_at(tmp_path: Path) -> None:
    clock = MutableClock()
    store = SQLiteAgentTaskStore(tmp_path / "tasks.db", clock=clock)
    task, _ = _create(store)
    lease = store.claim_task(task.task_id, owner="alice", worker_id="worker-a", lease_seconds=30)
    assert lease is not None
    with store.connect() as connection:
        claimed_heartbeat = connection.execute(
            "SELECT heartbeat_at FROM agent_tasks WHERE task_id = ?", (task.task_id,)
        ).fetchone()[0]
    assert claimed_heartbeat == clock.value.isoformat().replace("+00:00", "Z")
    clock.advance(timedelta(seconds=5))
    renewed = store.renew_task(lease, lease_seconds=30)
    with store.connect() as connection:
        renewed_heartbeat = connection.execute(
            "SELECT heartbeat_at FROM agent_tasks WHERE task_id = ?", (task.task_id,)
        ).fetchone()[0]
    assert renewed_heartbeat == clock.value.isoformat().replace("+00:00", "Z")
    assert renewed.expires_at != lease.expires_at


def test_finalize_requires_policy_compatible_ready_capsule(tmp_path: Path) -> None:
    store = SQLiteAgentTaskStore(tmp_path / "tasks.db", clock=MutableClock())
    task, _ = _create(store)
    lease = store.claim_task(task.task_id, owner="alice", worker_id="worker-a", lease_seconds=30)
    assert lease is not None
    linked = store.link_run(task.task_id, lease=lease, run_id="run-1")
    lease = replace(lease, version=linked.version)
    capsule_task = replace(
        task,
        completion_policy=AgentTaskCompletionPolicy.EVIDENCE_AND_CAPSULE_REQUIRED,
    )
    # The policy is persisted as part of the gate transition, not by mutating a record.
    store.advance_gate(
        task.task_id,
        lease=lease,
        gate_state=AgentTaskGateState.AWAITING_CAPSULE,
        receipt=None,
        completion_policy=capsule_task.completion_policy,
    )
    refreshed = store.get_task(task.task_id, owner="alice")
    renewed = store.renew_task(
        replace(lease, version=refreshed.version), lease_seconds=30
    )
    with pytest.raises(AgentTaskConflict, match="Capsule"):
        store.finalize_task(
            task.task_id,
            lease=renewed,
            gate_receipt=_terminal_gate(task),
            result=AgentTaskResult.succeeded(("evidence-1",)),
        )


def test_finalize_persists_verified_gate_and_releases_lease(tmp_path: Path) -> None:
    store = SQLiteAgentTaskStore(tmp_path / "tasks.db", clock=MutableClock())
    task, _ = _create(store)
    lease = store.claim_task(task.task_id, owner="alice", worker_id="worker-a", lease_seconds=30)
    assert lease is not None
    linked = store.link_run(task.task_id, lease=lease, run_id="run-1")
    lease = replace(lease, version=linked.version)
    completed = store.finalize_task(
        task.task_id,
        lease=lease,
        gate_receipt=_terminal_gate(task),
        result=AgentTaskResult.succeeded(("evidence-1",)),
    )

    assert completed.state is AgentTaskState.SUCCEEDED
    assert completed.gate_state is AgentTaskGateState.COMPLETED
    assert completed.gate_receipt is not None
    assert completed.legacy_gate_unverified is False
    assert completed.lease_owner is None


def test_terminal_finalize_replay_allows_exact_immutable_identity_without_lease(
    tmp_path: Path,
) -> None:
    store = SQLiteAgentTaskStore(tmp_path / "tasks.db", clock=MutableClock())
    task, _ = _create(store)
    lease = store.claim_task(task.task_id, owner="alice", worker_id="worker-a", lease_seconds=30)
    assert lease is not None
    linked = store.link_run(task.task_id, lease=lease, run_id="run-1")
    lease = replace(lease, version=linked.version)
    gate = _terminal_gate(task)
    store.finalize_task(
        task.task_id,
        lease=lease,
        gate_receipt=gate,
        result=AgentTaskResult.succeeded(("evidence-1",)),
    )

    replay = store.finalize_task(
        task.task_id,
        lease=lease,
        gate_receipt=gate,
        result=AgentTaskResult.succeeded(("evidence-1",)),
    )

    assert replay.state is AgentTaskState.SUCCEEDED


def test_terminal_finalize_replay_rejects_missing_or_different_stage_identity(
    tmp_path: Path,
) -> None:
    store = SQLiteAgentTaskStore(tmp_path / "tasks.db", clock=MutableClock())
    task, _ = _create(store)
    lease = store.claim_task(task.task_id, owner="alice", worker_id="worker-a", lease_seconds=30)
    assert lease is not None
    linked = store.link_run(task.task_id, lease=lease, run_id="run-1")
    lease = replace(lease, version=linked.version)
    gate = _terminal_gate(task)
    store.finalize_task(
        task.task_id,
        lease=lease,
        gate_receipt=gate,
        result=AgentTaskResult.succeeded(("evidence-1",)),
    )
    with pytest.raises(AgentTaskConflict, match="identity"):
        store.finalize_task(
            task.task_id,
            lease=lease,
            gate_receipt=None,
            result=AgentTaskResult.succeeded(("evidence-1",)),
        )
    with pytest.raises(AgentTaskConflict, match="identity"):
        store.finalize_task(
            task.task_id,
            lease=lease,
            gate_receipt=replace(gate, evidence_digest="c" * 64),
            result=AgentTaskResult.succeeded(("evidence-1",)),
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("task_id", "task-other"),
        ("run_id", "run-other"),
        ("owner", "bob"),
        ("session_id", "session-2"),
        ("originating_turn_id", "turn-2"),
        ("completion_policy", AgentTaskCompletionPolicy.EVIDENCE_AND_CAPSULE_REQUIRED),
        ("request_digest", "c" * 64),
        ("idempotency_key", "validate-2"),
        ("resource_envelope_id", "d" * 64),
        ("workspace_digest", "e" * 64),
    ],
)
def test_schedule_receipt_identity_must_match_task(
    tmp_path: Path, field: str, value: object
) -> None:
    store = SQLiteAgentTaskStore(tmp_path / "tasks.db", clock=MutableClock())
    task, _ = _create(store)
    lease = store.claim_task(task.task_id, owner="alice", worker_id="worker-a", lease_seconds=30)
    assert lease is not None
    linked = store.link_run(task.task_id, lease=lease, run_id="run-1")
    lease = replace(lease, version=linked.version)
    with pytest.raises(AgentTaskConflict, match="identity"):
        store.advance_gate(
            task.task_id,
            lease=lease,
            gate_state=AgentTaskGateState.ADMITTED,
            receipt=_schedule_receipt(task, **{field: value}),
        )


def test_valid_schedule_receipt_is_persisted_with_stage_identity(tmp_path: Path) -> None:
    store = SQLiteAgentTaskStore(tmp_path / "tasks.db", clock=MutableClock())
    task, _ = _create(store)
    lease = store.claim_task(task.task_id, owner="alice", worker_id="worker-a", lease_seconds=30)
    assert lease is not None
    linked = store.link_run(task.task_id, lease=lease, run_id="run-1")
    advanced = store.advance_gate(
        task.task_id,
        lease=replace(lease, version=linked.version),
        gate_state=AgentTaskGateState.ADMITTED,
        receipt=_schedule_receipt(task),
    )

    assert advanced.schedule_receipt is not None
    assert advanced.schedule_receipt.idempotency_key == task.request_key


def test_schedule_and_gate_share_root_but_use_distinct_stage_keys(tmp_path: Path) -> None:
    store = SQLiteAgentTaskStore(tmp_path / "tasks.db", clock=MutableClock())
    task, _ = _create(store)
    lease = store.claim_task(task.task_id, owner="alice", worker_id="worker-a", lease_seconds=30)
    assert lease is not None
    linked = store.link_run(task.task_id, lease=lease, run_id="run-1")
    schedule = store.advance_gate(
        task.task_id,
        lease=replace(lease, version=linked.version),
        gate_state=AgentTaskGateState.ADMITTED,
        receipt=_schedule_receipt(task),
        causation_root_key="cause-1",
        stage_operation_key="schedule-1",
    )
    gate = _terminal_gate(task)
    advanced = store.advance_gate(
        task.task_id,
        lease=replace(lease, version=schedule.version),
        gate_state=AgentTaskGateState.AWAITING_INTEGRITY,
        receipt=gate,
        causation_root_key="cause-1",
        stage_operation_key="gate-1",
    )
    completed = store.finalize_task(
        task.task_id,
        lease=replace(lease, version=advanced.version),
        gate_receipt=gate,
        result=AgentTaskResult.succeeded(("evidence-1",)),
        causation_root_key="cause-1",
        stage_operation_key="gate-1",
    )

    assert completed.state is AgentTaskState.SUCCEEDED


def test_same_stage_key_and_receipt_replay_is_idempotent(tmp_path: Path) -> None:
    store = SQLiteAgentTaskStore(tmp_path / "tasks.db", clock=MutableClock())
    task, _ = _create(store)
    lease = store.claim_task(task.task_id, owner="alice", worker_id="worker-a", lease_seconds=30)
    assert lease is not None
    linked = store.link_run(task.task_id, lease=lease, run_id="run-1")
    schedule = _schedule_receipt(task)
    first = store.advance_gate(
        task.task_id,
        lease=replace(lease, version=linked.version),
        gate_state=AgentTaskGateState.ADMITTED,
        receipt=schedule,
        causation_root_key="cause-1",
        stage_operation_key="schedule-1",
    )
    replay = store.advance_gate(
        task.task_id,
        lease=replace(lease, version=first.version),
        gate_state=AgentTaskGateState.ADMITTED,
        receipt=schedule,
        causation_root_key="cause-1",
        stage_operation_key="schedule-1",
    )

    assert replay == first


@pytest.mark.parametrize(
    ("root", "stage_key", "receipt_change"),
    [
        ("cause-2", "schedule-1", {}),
        ("cause-1", "schedule-2", {}),
        ("cause-1", "schedule-1", {"request_digest": "c" * 64}),
    ],
)
def test_schedule_stage_rejects_root_key_or_receipt_conflict(
    tmp_path: Path, root: str, stage_key: str, receipt_change: dict[str, object]
) -> None:
    store = SQLiteAgentTaskStore(tmp_path / "tasks.db", clock=MutableClock())
    task, _ = _create(store)
    lease = store.claim_task(task.task_id, owner="alice", worker_id="worker-a", lease_seconds=30)
    assert lease is not None
    linked = store.link_run(task.task_id, lease=lease, run_id="run-1")
    schedule = _schedule_receipt(task)
    first = store.advance_gate(
        task.task_id,
        lease=replace(lease, version=linked.version),
        gate_state=AgentTaskGateState.ADMITTED,
        receipt=schedule,
        causation_root_key="cause-1",
        stage_operation_key="schedule-1",
    )
    with pytest.raises(AgentTaskConflict, match="identity"):
        store.advance_gate(
            task.task_id,
            lease=replace(lease, version=first.version),
            gate_state=AgentTaskGateState.ADMITTED,
            receipt=replace(schedule, **receipt_change),
            causation_root_key=root,
            stage_operation_key=stage_key,
        )


def test_terminal_finalize_replay_requires_matching_gate_stage_key(tmp_path: Path) -> None:
    store = SQLiteAgentTaskStore(tmp_path / "tasks.db", clock=MutableClock())
    task, _ = _create(store)
    lease = store.claim_task(task.task_id, owner="alice", worker_id="worker-a", lease_seconds=30)
    assert lease is not None
    linked = store.link_run(task.task_id, lease=lease, run_id="run-1")
    gate = _terminal_gate(task)
    completed = store.finalize_task(
        task.task_id,
        lease=replace(lease, version=linked.version),
        gate_receipt=gate,
        result=AgentTaskResult.succeeded(("evidence-1",)),
        causation_root_key="cause-1",
        stage_operation_key="gate-1",
    )
    assert completed.state is AgentTaskState.SUCCEEDED
    with pytest.raises(AgentTaskConflict, match="identity"):
        store.finalize_task(
            task.task_id,
            lease=lease,
            gate_receipt=gate,
            result=AgentTaskResult.succeeded(("evidence-1",)),
            causation_root_key="cause-1",
            stage_operation_key="gate-2",
        )


def test_advance_gate_rejects_policy_incompatible_receipt_before_update(tmp_path: Path) -> None:
    store = SQLiteAgentTaskStore(tmp_path / "tasks.db", clock=MutableClock())
    task, _ = _create(store)
    lease = store.claim_task(task.task_id, owner="alice", worker_id="worker-a", lease_seconds=30)
    assert lease is not None
    linked = store.link_run(task.task_id, lease=lease, run_id="run-1")
    lease = replace(lease, version=linked.version)
    waiting = store.advance_gate(
        task.task_id,
        lease=lease,
        gate_state=AgentTaskGateState.AWAITING_CAPSULE,
        completion_policy=AgentTaskCompletionPolicy.EVIDENCE_AND_CAPSULE_REQUIRED,
    )
    with pytest.raises(AgentTaskConflict, match="Capsule"):
        store.advance_gate(
            task.task_id,
            lease=replace(lease, version=waiting.version),
            gate_state=AgentTaskGateState.AWAITING_CAPSULE,
            receipt=_terminal_gate(task),
        )
    assert store.get_task(task.task_id, owner="alice").gate_receipt is None


def test_input_required_is_not_a_progress_gate_target(tmp_path: Path) -> None:
    store = SQLiteAgentTaskStore(tmp_path / "tasks.db", clock=MutableClock())
    task, _ = _create(store)
    lease = store.claim_task(task.task_id, owner="alice", worker_id="worker-a", lease_seconds=30)
    assert lease is not None
    with pytest.raises(AgentTaskConflict, match="INPUT_REQUIRED"):
        store.advance_gate(
            task.task_id,
            lease=lease,
            gate_state=AgentTaskGateState.INPUT_REQUIRED,
        )


def test_capsule_required_policy_requires_ready_capsule_on_gate_receipt(
    tmp_path: Path,
) -> None:
    task, _ = _create(
        SQLiteAgentTaskStore(tmp_path / "tasks.db", clock=MutableClock())
    )
    not_required = AgentTaskGateReceipt(
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
    with pytest.raises(ValueError, match="Capsule"):
        replace(
            task,
            linked_run_id="run-1",
            completion_policy=AgentTaskCompletionPolicy.EVIDENCE_AND_CAPSULE_REQUIRED,
            gate_receipt=not_required,
        )

    ready = replace(not_required, capsule_ref="capsule-1", capsule_state="READY")
    completed = replace(
        task,
        linked_run_id="run-1",
        completion_policy=AgentTaskCompletionPolicy.EVIDENCE_AND_CAPSULE_REQUIRED,
        gate_receipt=ready,
    )
    assert completed.gate_receipt == ready


def test_evidence_policy_allows_ready_capsule_as_an_extra_product(tmp_path: Path) -> None:
    task, _ = _create(
        SQLiteAgentTaskStore(tmp_path / "tasks.db", clock=MutableClock())
    )
    ready = AgentTaskGateReceipt(
        task_id=task.task_id,
        run_id="run-1",
        run_terminal_state="completed",
        evidence_refs=("evidence-1",),
        evidence_digest="a" * 64,
        integrity_verified_at="2026-08-19T00:05:00Z",
        workspace_revision=None,
        workspace_digest="b" * 64,
        legacy_boundary=True,
        capsule_ref="capsule-1",
        capsule_state="READY",
    )

    completed = replace(task, linked_run_id="run-1", gate_receipt=ready)
    assert completed.gate_receipt == ready


def test_new_gate_columns_with_null_value_remain_legacy_unverified(
    tmp_path: Path,
) -> None:
    store = SQLiteAgentTaskStore(tmp_path / "tasks.db", clock=MutableClock())
    task, _ = _create(store)
    with store.connect() as connection:
        row = dict(
            connection.execute(
                "SELECT * FROM agent_tasks WHERE task_id = ?", (task.task_id,)
            ).fetchone()
        )
    row.update(completion_policy=None, gate_state=None, legacy_gate_unverified=0)
    assert _task_from_row(row).legacy_gate_unverified is True


@pytest.mark.parametrize("receipt_type", [AgentTaskScheduleReceipt, AgentTaskGateReceipt])
def test_legacy_boundary_is_required_when_revision_is_missing(receipt_type: type) -> None:
    values: dict[str, object] = {
        "task_id": "task-1",
        "run_id": "run-1",
        "workspace_revision": None,
        "workspace_digest": "a" * 64,
        "legacy_boundary": False,
    }
    if receipt_type is AgentTaskScheduleReceipt:
        values.update(
            {
                "completion_policy": AgentTaskCompletionPolicy.EVIDENCE_REQUIRED,
                "submit_state": "admitted",
            }
        )
    else:
        values.update(
            {
                "evidence_refs": ("evidence-1",),
                "evidence_digest": "b" * 64,
                "integrity_verified_at": "2026-08-19T00:05:00Z",
                "capsule_ref": None,
                "capsule_state": "not_required",
                "run_terminal_state": "completed",
            }
        )
    with pytest.raises(ValueError, match="legacy workspace boundary"):
        receipt_type(**values)


@pytest.mark.parametrize("receipt_type", [AgentTaskScheduleReceipt, AgentTaskGateReceipt])
def test_live_boundary_is_required_when_revision_is_present(receipt_type: type) -> None:
    values: dict[str, object] = {
        "task_id": "task-1",
        "run_id": "run-1",
        "workspace_revision": 1,
        "workspace_digest": "a" * 64,
        "legacy_boundary": True,
    }
    if receipt_type is AgentTaskScheduleReceipt:
        values.update(
            {
                "completion_policy": AgentTaskCompletionPolicy.EVIDENCE_REQUIRED,
                "submit_state": "admitted",
            }
        )
    else:
        values.update(
            {
                "evidence_refs": ("evidence-1",),
                "evidence_digest": "b" * 64,
                "integrity_verified_at": "2026-08-19T00:05:00Z",
                "capsule_ref": None,
                "capsule_state": "not_required",
                "run_terminal_state": "completed",
            }
        )
    with pytest.raises(ValueError, match="legacy workspace boundary"):
        receipt_type(**values)


def test_task_policy_must_match_schedule_receipt_policy(tmp_path: Path) -> None:
    task, _ = _create(
        SQLiteAgentTaskStore(tmp_path / "tasks.db", clock=MutableClock())
    )
    receipt = AgentTaskScheduleReceipt(
        task_id=task.task_id,
        run_id="run-1",
        completion_policy=AgentTaskCompletionPolicy.EVIDENCE_AND_CAPSULE_REQUIRED,
        submit_state="admitted",
    )

    with pytest.raises(ValueError, match="completion_policy"):
        replace(task, schedule_receipt=receipt)


def test_receipt_serializers_emit_schema_valid_full_receipts(tmp_path: Path) -> None:
    schedule = AgentTaskScheduleReceipt(
        task_id="task-1",
        run_id="run-1",
        completion_policy=AgentTaskCompletionPolicy.EVIDENCE_AND_CAPSULE_REQUIRED,
        submit_state="submitted",
        receipt_id="receipt-1",
        owner="alice",
        session_id="session-1",
        originating_turn_id="turn-1",
        request_digest="a" * 64,
        idempotency_key="validate-1",
        slurm_job_id="123",
        resource_envelope_id="envelope-1",
        workspace_revision=1,
        workspace_digest="b" * 64,
        created_at="2026-08-19T00:00:00Z",
        legacy_boundary=False,
    )
    gate = AgentTaskGateReceipt(
        task_id="task-1",
        run_id="run-1",
        run_terminal_state="completed",
        evidence_refs=("evidence-1",),
        evidence_digest="c" * 64,
        integrity_verified_at="2026-08-19T00:05:00Z",
        workspace_revision=1,
        workspace_digest="b" * 64,
        legacy_boundary=False,
        capsule_ref="capsule-1",
        capsule_state="READY",
        terminal_at="2026-08-19T00:05:00Z",
    )
    task, _ = _create(
        SQLiteAgentTaskStore(tmp_path / "tasks.db", clock=MutableClock())
    )
    payload = agent_task_payload(task)
    payload.update(
        {
            "completion_policy": schedule.completion_policy.value,
            "gate_state": "completed",
            "schedule_receipt": agent_task_schedule_receipt_payload(schedule),
            "gate_receipt": agent_task_gate_receipt_payload(gate),
            "legacy_gate_unverified": False,
        }
    )
    payload = json.loads(json.dumps(payload))
    from jsonschema import Draft202012Validator

    schema_path = (
        Path(__file__).resolve().parents[2]
        / "schemas"
        / "agent"
        / "v2"
        / "agent-task.schema.json"
    )
    Draft202012Validator(json.loads(schema_path.read_text())).validate(payload)
    assert payload["schedule_receipt"]["workspace_revision"] == 1
    assert payload["gate_receipt"]["capsule_ref"] == "capsule-1"


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
    assert task.legacy_gate_unverified is False

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

    gate = _terminal_gate(task)
    advanced = store.advance_gate(
        task.task_id,
        lease=second,
        gate_state=AgentTaskGateState.AWAITING_INTEGRITY,
        receipt=gate,
    )
    finalized_lease = replace(second, version=advanced.version)
    completed = store.complete_task(
        task.task_id,
        lease=finalized_lease,
        result=AgentTaskResult.succeeded(("evidence-1",)),
    )
    assert completed.state is AgentTaskState.SUCCEEDED
    assert completed.legacy_gate_unverified is False
    assert completed.result is not None
    assert completed.result.evidence_refs == ("evidence-1",)
    replay = store.complete_task(
        task.task_id,
        lease=finalized_lease,
        result=AgentTaskResult.succeeded(("evidence-1",)),
    )
    assert replay == completed

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
