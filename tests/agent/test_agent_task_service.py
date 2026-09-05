from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
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
from pilot107.agent.tasks import (
    AgentResourceEnvelope,
    AgentTaskCompletionPolicy,
    AgentTaskGateState,
    AgentTaskRequest,
    AgentTaskState,
    agent_task_schedule_receipt_payload,
)
from pilot107.agent.tool_gateway import AgentToolGatewayError
from pilot107.core.control_repository import SQLiteControlRepository
from pilot107.core.evidence_binding import EvidenceBinder
from pilot107.core.run_service import RunService
from pilot107.core.run_store import RunStore
from pilot107.core.states import CapsuleState, RunState
from pilot107.services.agent_session_service import AgentSessionService
from pilot107.services.agent_task_service import (
    AgentTaskProvenanceError,
    AgentTaskService,
    build_server_provenance_authority,
    build_verified_capsule_authority,
)
from pilot107.worker.capsule import CapsuleError, RawCapsuleService
from pilot107.worker.evidence import EvidenceStore
from pilot107.worker.runtime_worker import RuntimeReconcileWorker


@pytest.fixture(autouse=True)
def _restore_test_tree_permissions(tmp_path: Path) -> Iterator[None]:
    """Keep pytest cleanup safe after production code seals fixture directories."""

    yield
    for path in sorted(tmp_path.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if path.is_symlink():
            continue
        with suppress(FileNotFoundError):
            path.chmod(0o700 if path.is_dir() else 0o600)


class MutableClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 19, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


def test_server_provenance_authority_binds_workspace_and_platform_snapshot(
    tmp_path: Path,
) -> None:
    class Snapshot:
        snapshot_id = "platform-1"

    class Selection:
        record = Snapshot()

    class SnapshotStore:
        def latest_usable(self, *, owner: str):
            assert owner == "alice"
            return Selection()

    seen: list[tuple[str, str, str]] = []
    resolver = build_server_provenance_authority(
        workspace_resolver=lambda owner, workspace_id, digest: (
            seen.append((owner, workspace_id, digest)) or tmp_path
        ),
        platform_snapshot_store=SnapshotStore(),
    )

    assert resolver("alice", "workspace-1", "a" * 64) == (
        f"workspace-snapshot:sha256:{'a' * 64}",
        "snapshot:platform-1",
    )
    assert seen == [("alice", "workspace-1", "a" * 64)]


def test_server_provenance_authority_fails_closed_without_platform_snapshot(
    tmp_path: Path,
) -> None:
    class SnapshotStore:
        def latest_usable(self, *, owner: str):
            return None

    resolver = build_server_provenance_authority(
        workspace_resolver=lambda owner, workspace_id, digest: tmp_path,
        platform_snapshot_store=SnapshotStore(),
    )

    with pytest.raises(AgentTaskProvenanceError) as error:
        resolver("alice", "workspace-1", "a" * 64)
    assert error.value.code == "AGENT.TASK.PROVENANCE_AUTHORITY_UNAVAILABLE"
    assert "权威" in str(error.value)


def test_verified_capsule_authority_returns_manifest_bound_reference() -> None:
    class Capsule:
        valid = True
        capsule_id = "capsule_1"
        manifest_sha256 = "a" * 64

    class CapsuleService:
        def get_raw_capsule(self, run_id: str):
            assert run_id == "run_1"
            return Capsule()

    resolver = build_verified_capsule_authority(CapsuleService())

    assert resolver("run_1") == f"capsule:capsule_1:sha256:{'a' * 64}"


def test_verified_capsule_authority_rejects_malformed_manifest_digest() -> None:
    class Capsule:
        valid = True
        capsule_id = "capsule_1"
        manifest_sha256 = "model-claimed-digest"

    class CapsuleService:
        def get_raw_capsule(self, run_id: str):
            return Capsule()

    resolver = build_verified_capsule_authority(CapsuleService())

    with pytest.raises(ValueError, match="manifest digest"):
        resolver("run_1")


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
        self.evidence_store = EvidenceStore(tmp_path / "evidence")
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
            evidence_binder=EvidenceBinder(
                store=self.run_store,
                evidence_root=self.evidence_store.root,
            ),
            capsule_authority_resolver=lambda run_id: f"capsule:{run_id}",
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

    def restart_task_service(self) -> None:
        previous = self.service
        self.service = AgentTaskService(
            store=self.task_store,
            session_store=self.session_store,
            session_service=self.session_service,
            run_service=self.run_service,
            control_repository=self.control,
            workspace_resolver=previous.workspace_resolver,
            provenance_authority_resolver=previous.provenance_authority_resolver,
            evidence_binder=previous.evidence_binder,
            capsule_authority_resolver=previous.capsule_authority_resolver,
            run_workdir_resolver=previous.run_workdir_resolver,
            worker_id="task-worker-restarted",
            lease_seconds=30,
        )

    def schedule(
        self,
        *,
        completion_policy: AgentTaskCompletionPolicy = (
            AgentTaskCompletionPolicy.EVIDENCE_REQUIRED
        ),
    ):
        return self.service.schedule_validation(
            owner="alice",
            session_id=self.session.session_id,
            turn_id=self.turn_id,
            project_id="project-1",
            workspace_id="workspace-1",
            request_key="validation-1",
            request=_request(),
            envelope=_envelope(),
            completion_policy=completion_policy,
        )

    def finish_run(self, run_id: str, *, exit_code: str = "0:0") -> None:
        self.run_service.dispatch_due_submissions(limit=10)
        run = self.run_store.get_run(run_id)
        assert run.job_id is not None
        self.backend.advance_job(
            job_id=run.job_id,
            raw_state="COMPLETED" if exit_code == "0:0" else "FAILED",
            exit_code=exit_code,
        )
        self.run_service.reconcile_once(run_id)

    def finalize_evidence(self, run_id: str, *, content: str = "validated\n") -> tuple[str, ...]:
        run = self.run_store.get_run(run_id)
        artifact = self.evidence_store.write_text(
            run_id=run_id,
            logical_path="logs/stdout.txt",
            content=content,
            content_type="text/plain",
        )
        artifact_ref = f"evidence://runs/{run_id}/{artifact.logical_path}"
        boundary = {
            "workspace_revision": run.workspace_revision,
            "workspace_digest": run.workspace_digest,
            "source_revision": run.source_revision,
            "platform_snapshot_ref": run.platform_snapshot_ref,
        }
        finalized_at = "2026-08-31T00:00:00+00:00"
        self.run_store.upsert_evidence_objects(
            run_id,
            [
                {
                    "object_id": f"ev-{run_id}-stdout",
                    "category": "logs",
                    "logical_path": artifact.logical_path,
                    "store_path": str(artifact.path),
                    "source_uri": artifact_ref,
                    "sha256": artifact.sha256,
                    "size_bytes": artifact.size_bytes,
                    "mime_type": artifact.content_type,
                    "collection_status": "collected",
                    "mutable_during_run": False,
                    "finalized_at": finalized_at,
                    **boundary,
                }
            ],
        )
        manifest = self.evidence_store.write_json(
            run_id=run_id,
            logical_path="manifest/manifest.json",
            payload={
                "schema": "pilot107.evidence_manifest.v1",
                "run_id": run_id,
                "owner": run.owner,
                "job_id": run.job_id,
                "workspace_revision": run.workspace_revision,
                "workspace_digest": run.workspace_digest,
                "legacy_boundary": True,
                "source_revision": run.source_revision,
                "platform_snapshot_ref": run.platform_snapshot_ref,
                "artifacts": [
                    {
                        "logical_path": artifact.logical_path,
                        "size_bytes": artifact.size_bytes,
                        "sha256": artifact.sha256,
                        "content_type": artifact.content_type,
                        "evidence_ref": artifact_ref,
                    }
                ],
                "warnings": [],
            },
        )
        manifest_ref = f"evidence://runs/{run_id}/{manifest.logical_path}"
        self.run_store.upsert_evidence_objects(
            run_id,
            [
                {
                    "object_id": f"ev-{run_id}-manifest",
                    "category": "manifest",
                    "logical_path": manifest.logical_path,
                    "store_path": str(manifest.path),
                    "source_uri": manifest_ref,
                    "sha256": manifest.sha256,
                    "size_bytes": manifest.size_bytes,
                    "mime_type": manifest.content_type,
                    "collection_status": "collected",
                    "mutable_during_run": False,
                    "finalized_at": finalized_at,
                    **boundary,
                }
            ],
        )
        with self.run_store.connect() as connection:
            connection.execute(
                "UPDATE runs SET collection_state = 'succeeded' WHERE run_id = ?",
                (run_id,),
            )
        return artifact_ref, manifest_ref


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
    assert harness.run_service.enqueue_submission(persisted.linked_run_id).state == "pending"


def test_agent_task_run_persists_provenance_without_inventing_values(tmp_path: Path) -> None:
    harness = Harness(tmp_path, provenance_authority=True)
    request = replace(
        _request(),
        payload={
            **_request().payload,
            "workspace_revision": 7,
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

    batch = harness.service.dispatch_due(limit=1)
    assert batch.succeeded == 1
    assert batch.errors == []
    persisted = harness.task_store.get_task(task.task_id, owner="alice")
    assert persisted.state is AgentTaskState.FAILED
    assert persisted.gate_state is AgentTaskGateState.BLOCKED
    assert persisted.result is not None
    assert persisted.result.error_code == "AGENT.TASK.PROVENANCE_PAYLOAD_FORBIDDEN"
    assert "不可信" in persisted.result.message
    assert persisted.linked_run_id is None


def test_agent_task_without_provenance_authority_creates_no_run(tmp_path: Path) -> None:
    harness = Harness(tmp_path, provenance_authority=False)
    task, _ = harness.schedule()

    batch = harness.service.dispatch_due(limit=1)

    assert batch.succeeded == 1
    assert batch.errors == []
    persisted = harness.task_store.get_task(task.task_id, owner="alice")
    assert persisted.state is AgentTaskState.FAILED
    assert persisted.gate_state is AgentTaskGateState.BLOCKED
    assert persisted.result is not None
    assert persisted.result.error_code == "AGENT.TASK.PROVENANCE_AUTHORITY_UNAVAILABLE"
    assert "权威来源不可用" in persisted.result.message
    assert persisted.linked_run_id is None
    runs, _ = harness.run_store.list_runs_page(owner="alice")
    assert runs == []


def test_agent_task_authority_missing_fields_creates_no_run(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.service.provenance_authority_resolver = lambda owner, workspace_id, digest: (
        "",
        "snapshot:platform-1",
    )
    task, _ = harness.schedule()

    batch = harness.service.dispatch_due(limit=1)

    assert batch.succeeded == 1
    assert batch.errors == []
    persisted = harness.task_store.get_task(task.task_id, owner="alice")
    assert persisted.state is AgentTaskState.FAILED
    assert persisted.gate_state is AgentTaskGateState.BLOCKED
    assert persisted.result is not None
    assert persisted.result.error_code == "AGENT.TASK.PROVENANCE_AUTHORITY_INVALID"
    assert "无效事实" in persisted.result.message
    assert persisted.linked_run_id is None
    runs, _ = harness.run_store.list_runs_page(owner="alice")
    assert runs == []


def test_agent_task_authority_exception_creates_no_run(tmp_path: Path) -> None:
    harness = Harness(tmp_path)

    def fail_authority(owner: str, workspace_id: str, digest: str) -> tuple[str, str]:
        raise RuntimeError("authority unavailable")

    harness.service.provenance_authority_resolver = fail_authority
    task, _ = harness.schedule()

    batch = harness.service.dispatch_due(limit=1)

    assert batch.succeeded == 1
    assert batch.errors == []
    persisted = harness.task_store.get_task(task.task_id, owner="alice")
    assert persisted.state is AgentTaskState.FAILED
    assert persisted.gate_state is AgentTaskGateState.BLOCKED
    assert persisted.result is not None
    assert persisted.result.error_code == "AGENT.TASK.PROVENANCE_AUTHORITY_UNAVAILABLE"
    assert "权威来源不可用" in persisted.result.message
    assert persisted.linked_run_id is None
    runs, _ = harness.run_store.list_runs_page(owner="alice")
    assert runs == []


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
    harness.finish_run(run_id)
    evidence_refs = harness.finalize_evidence(run_id)

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
    assert completed.result.evidence_refs == evidence_refs
    assert len(followups) == 1


def test_schedule_receipt_never_completes_agent_task(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    task, _ = harness.schedule()

    harness.service.dispatch_due(limit=10)
    scheduled = harness.task_store.get_task(task.task_id, owner="alice")
    assert scheduled.state is AgentTaskState.RUNNING
    assert scheduled.schedule_receipt is not None
    assert scheduled.schedule_receipt.is_terminal is False
    with harness.control.connect() as connection:
        ready_count = connection.execute(
            "SELECT COUNT(*) FROM control_outbox WHERE topic = ?",
            ("agent.task.ready.v1",),
        ).fetchone()[0]
    assert ready_count == 0


def test_run_terminal_without_finalized_evidence_stays_awaiting_evidence(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    task, _ = harness.schedule()
    harness.service.dispatch_due(limit=10)
    running = harness.task_store.get_task(task.task_id, owner="alice")
    assert running.linked_run_id is not None
    harness.finish_run(running.linked_run_id)

    batch = harness.service.reconcile_active(limit=10)
    waiting = harness.task_store.get_task(task.task_id, owner="alice")

    assert batch.succeeded == 0
    assert batch.errors == []
    assert waiting.state is AgentTaskState.RUNNING
    assert waiting.gate_state is AgentTaskGateState.AWAITING_EVIDENCE
    with harness.control.connect() as connection:
        ready_count = connection.execute(
            "SELECT COUNT(*) FROM control_outbox WHERE topic = ?",
            ("agent.task.ready.v1",),
        ).fetchone()[0]
    assert ready_count == 0


def test_active_evidence_seal_claim_keeps_task_retryably_awaiting(tmp_path: Path) -> None:
    """Treating claim contention as integrity failure terminally blocks valid work."""

    harness = Harness(tmp_path)
    task, _ = harness.schedule()
    harness.service.dispatch_due(limit=10)
    running = harness.task_store.get_task(task.task_id, owner="alice")
    assert running.linked_run_id is not None
    harness.finish_run(running.linked_run_id)
    harness.finalize_evidence(running.linked_run_id)
    harness.run_store.begin_evidence_seal(
        running.linked_run_id,
        claim_owner="other-live-sealer",
        lease_seconds=300,
    )

    batch = harness.service.reconcile_active(limit=10)
    waiting = harness.task_store.get_task(task.task_id, owner="alice")

    assert batch.succeeded == 0
    assert batch.errors == []
    assert waiting.state is AgentTaskState.RUNNING
    assert waiting.gate_state is AgentTaskGateState.AWAITING_EVIDENCE


def test_evidence_required_task_completes_without_capsule(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    task, _ = harness.schedule()
    harness.service.dispatch_due(limit=10)
    running = harness.task_store.get_task(task.task_id, owner="alice")
    assert running.linked_run_id is not None
    harness.finish_run(running.linked_run_id)
    refs = harness.finalize_evidence(running.linked_run_id)

    assert harness.service.reconcile_active(limit=10).succeeded == 1
    completed = harness.task_store.get_task(task.task_id, owner="alice")

    assert completed.state is AgentTaskState.SUCCEEDED
    assert completed.gate_state is AgentTaskGateState.COMPLETED
    assert completed.gate_receipt is not None
    assert completed.gate_receipt.capsule_state == "not_required"
    assert completed.result is not None
    assert completed.result.evidence_refs == refs


def test_failed_run_with_finalized_evidence_finishes_failed(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    task, _ = harness.schedule()
    harness.service.dispatch_due(limit=10)
    running = harness.task_store.get_task(task.task_id, owner="alice")
    assert running.linked_run_id is not None
    harness.finish_run(running.linked_run_id, exit_code="1:0")
    refs = harness.finalize_evidence(running.linked_run_id)

    assert harness.service.reconcile_active(limit=10).succeeded == 1
    failed = harness.task_store.get_task(task.task_id, owner="alice")

    assert failed.state is AgentTaskState.FAILED
    assert failed.gate_state is AgentTaskGateState.FAILED
    assert failed.result is not None
    assert failed.result.error_code == "VALIDATION.RUN_FAILED"
    assert failed.result.evidence_refs == refs


def test_evidence_and_capsule_task_waits_for_capsule_ready(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    task, _ = harness.schedule(
        completion_policy=AgentTaskCompletionPolicy.EVIDENCE_AND_CAPSULE_REQUIRED
    )
    harness.service.dispatch_due(limit=10)
    running = harness.task_store.get_task(task.task_id, owner="alice")
    assert running.linked_run_id is not None
    harness.finish_run(running.linked_run_id)
    harness.finalize_evidence(running.linked_run_id)

    assert harness.service.reconcile_active(limit=10).succeeded == 0
    waiting = harness.task_store.get_task(task.task_id, owner="alice")
    assert waiting.state is AgentTaskState.RUNNING
    assert waiting.gate_state is AgentTaskGateState.AWAITING_CAPSULE

    harness.run_store.update_capsule_state(
        running.linked_run_id,
        CapsuleState.READY,
        event_type="test.capsule_ready",
    )
    assert harness.service.reconcile_active(limit=10).succeeded == 1
    completed = harness.task_store.get_task(task.task_id, owner="alice")
    assert completed.gate_receipt is not None
    assert completed.gate_receipt.capsule_state == "READY"
    assert completed.gate_receipt.capsule_ref == f"capsule:{running.linked_run_id}"


def test_runtime_worker_builds_required_capsule_after_seal_before_ready_followup(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    capsule_service = RawCapsuleService(
        store=harness.run_store,
        evidence_store=harness.evidence_store,
        capsule_root=tmp_path / "capsules",
    )
    harness.service.capsule_authority_resolver = build_verified_capsule_authority(capsule_service)
    task, _ = harness.schedule(
        completion_policy=AgentTaskCompletionPolicy.EVIDENCE_AND_CAPSULE_REQUIRED
    )
    harness.service.dispatch_due(limit=10)
    running = harness.task_store.get_task(task.task_id, owner="alice")
    assert running.linked_run_id is not None
    harness.finish_run(running.linked_run_id)
    harness.finalize_evidence(running.linked_run_id)
    worker = RuntimeReconcileWorker(
        service=harness.run_service,
        agent_task_service=harness.service,
        capsule_service=capsule_service,
    )

    first = worker.tick()
    terminal = harness.task_store.get_task(task.task_id, owner="alice")
    followups_before_dispatch = [
        turn
        for turn in harness.session_store.list_recoverable_turns(limit=10)
        if turn.request_key == f"agent-task:{task.task_id}:ready"
    ]

    assert first.capsule_builds_succeeded == 1
    assert harness.run_store.get_run(running.linked_run_id).capsule_state is CapsuleState.READY
    assert terminal.state is AgentTaskState.SUCCEEDED
    assert terminal.gate_receipt is not None
    assert terminal.gate_receipt.capsule_state == "READY"
    assert followups_before_dispatch == []
    with harness.control.connect() as connection:
        capsule_messages = connection.execute(
            "SELECT topic, state, attempts, payload_json FROM control_outbox "
            "WHERE topic = 'capsule.build.v1'"
        ).fetchall()
        task_identity = connection.execute(
            "SELECT causation_root_key, gate_operation_key FROM agent_tasks WHERE task_id = ?",
            (task.task_id,),
        ).fetchone()
    assert len(capsule_messages) == 1
    topic, state, attempts, payload_json = tuple(capsule_messages[0])
    assert (topic, state, attempts) == ("capsule.build.v1", "succeeded", 1)
    capsule_identity = json.loads(payload_json)
    assert capsule_identity["causation_root_key"] == task_identity["causation_root_key"]
    assert capsule_identity["operation_key"] != task_identity["gate_operation_key"]

    worker.tick()
    followups = [
        turn
        for turn in harness.session_store.list_recoverable_turns(limit=10)
        if turn.request_key == f"agent-task:{task.task_id}:ready"
    ]
    assert len(followups) == 1


def test_capsule_outbox_ack_crash_recovers_same_artifact_without_duplicate_followup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SimulatedProcessCrash(BaseException):
        pass

    harness = Harness(tmp_path)
    capsule_service = RawCapsuleService(
        store=harness.run_store,
        evidence_store=harness.evidence_store,
        capsule_root=tmp_path / "capsules",
    )
    harness.service.capsule_authority_resolver = build_verified_capsule_authority(capsule_service)
    task, _ = harness.schedule(
        completion_policy=AgentTaskCompletionPolicy.EVIDENCE_AND_CAPSULE_REQUIRED
    )
    harness.service.dispatch_due(limit=10)
    running = harness.task_store.get_task(task.task_id, owner="alice")
    assert running.linked_run_id is not None
    harness.finish_run(running.linked_run_id)
    harness.finalize_evidence(running.linked_run_id)
    worker = RuntimeReconcileWorker(
        service=harness.run_service,
        agent_task_service=harness.service,
        capsule_service=capsule_service,
        task_lease_seconds=30,
    )
    original_acknowledge = harness.control.acknowledge

    def crash_capsule_ack(*, message_id: str, owner: str, fencing_token: int) -> None:
        if message_id.startswith("capsule:"):
            raise SimulatedProcessCrash("crash after Capsule publication")
        original_acknowledge(
            message_id=message_id,
            owner=owner,
            fencing_token=fencing_token,
        )

    monkeypatch.setattr(harness.control, "acknowledge", crash_capsule_ack)
    with pytest.raises(SimulatedProcessCrash):
        worker.tick()
    first = capsule_service.get_raw_capsule(running.linked_run_id)
    first_fence = harness.run_store.get_run(running.linked_run_id).capsule_build_fencing_token

    monkeypatch.setattr(harness.control, "acknowledge", original_acknowledge)
    harness.clock.advance(31)
    worker.tick()
    recovered = capsule_service.get_raw_capsule(running.linked_run_id)
    worker.tick()

    assert recovered.capsule_id == first.capsule_id
    assert recovered.manifest_sha256 == first.manifest_sha256
    assert (
        harness.run_store.get_run(running.linked_run_id).capsule_build_fencing_token > first_fence
    )
    with harness.control.connect() as connection:
        rows = connection.execute(
            "SELECT state, attempts FROM control_outbox WHERE topic = 'capsule.build.v1'"
        ).fetchall()
    assert [tuple(row) for row in rows] == [("succeeded", 2)]
    followups = [
        turn
        for turn in harness.session_store.list_recoverable_turns(limit=10)
        if turn.request_key == f"agent-task:{task.task_id}:ready"
    ]
    assert len(followups) == 1


def test_runtime_worker_lease_takeover_fences_old_builder_before_publish(
    tmp_path: Path,
) -> None:
    reached_publish = threading.Event()
    release_old = threading.Event()
    new_entered_build = threading.Event()
    old_received_lease_assert: list[bool] = []

    class BlockingCapsuleService(RawCapsuleService):
        def build_raw_capsule(self, run_id: str, **kwargs):  # type: ignore[no-untyped-def]
            lease_assert = kwargs.get("lease_assert")
            if threading.current_thread().name == "old-runtime":
                old_received_lease_assert.append(callable(lease_assert))
                lease_assert_calls = 0

                def block_then_assert() -> None:
                    nonlocal lease_assert_calls
                    lease_assert_calls += 1
                    if lease_assert_calls == 1:
                        if not callable(lease_assert):
                            raise CapsuleError("worker did not provide an outbox lease assertion")
                        lease_assert()
                        return
                    reached_publish.set()
                    release_old.wait(timeout=5)
                    if not callable(lease_assert):
                        raise CapsuleError("worker did not provide an outbox lease assertion")
                    lease_assert()

                kwargs["lease_assert"] = block_then_assert
            else:
                new_entered_build.set()
            return super().build_raw_capsule(run_id, **kwargs)

    harness = Harness(tmp_path)
    capsule_service = BlockingCapsuleService(
        store=harness.run_store,
        evidence_store=harness.evidence_store,
        capsule_root=tmp_path / "capsules",
    )
    harness.service.capsule_authority_resolver = build_verified_capsule_authority(capsule_service)
    task, _ = harness.schedule(
        completion_policy=AgentTaskCompletionPolicy.EVIDENCE_AND_CAPSULE_REQUIRED
    )
    harness.service.dispatch_due(limit=10)
    running = harness.task_store.get_task(task.task_id, owner="alice")
    assert running.linked_run_id is not None
    harness.finish_run(running.linked_run_id)
    harness.finalize_evidence(running.linked_run_id)
    workers = (
        RuntimeReconcileWorker(
            service=harness.run_service,
            agent_task_service=harness.service,
            capsule_service=capsule_service,
            worker_id="runtime-old",
            task_lease_seconds=30,
        ),
        RuntimeReconcileWorker(
            service=harness.run_service,
            agent_task_service=harness.service,
            capsule_service=capsule_service,
            worker_id="runtime-new",
            task_lease_seconds=30,
        ),
    )
    old_errors: list[BaseException] = []

    def run_old() -> None:
        try:
            workers[0].tick()
        except BaseException as exc:
            old_errors.append(exc)

    old_thread = threading.Thread(target=run_old, name="old-runtime")
    old_thread.start()
    assert reached_publish.wait(timeout=5)
    harness.clock.advance(31)
    with harness.control.connect() as connection:
        payload = json.loads(
            connection.execute(
                "SELECT payload_json FROM control_outbox WHERE topic = 'capsule.build.v1'"
            ).fetchone()[0]
        )
    new_thread = threading.Thread(target=workers[1].tick, name="new-runtime")
    new_thread.start()
    assert new_entered_build.wait(timeout=5)
    for _ in range(100):
        if harness.run_store.get_run(running.linked_run_id).capsule_build_fencing_token >= 2:
            break
        threading.Event().wait(0.01)
    else:
        raise AssertionError("takeover worker did not fence the stale Capsule builder")
    release_old.set()
    old_thread.join(timeout=5)
    new_thread.join(timeout=5)

    assert not old_thread.is_alive()
    assert not new_thread.is_alive()
    assert old_received_lease_assert == [True]
    completed = harness.run_store.get_run(running.linked_run_id)
    assert completed.capsule_state is CapsuleState.READY
    assert completed.capsule_operation_key == payload["operation_key"]
    assert completed.capsule_build_fencing_token >= 2
    with harness.control.connect() as connection:
        state, attempts = connection.execute(
            "SELECT state, attempts FROM control_outbox WHERE topic = 'capsule.build.v1'"
        ).fetchone()
    assert (state, attempts) == ("succeeded", 2)


def test_runtime_worker_lease_takeover_reuses_publish_but_rejects_old_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published = threading.Event()
    release_old = threading.Event()
    harness = Harness(tmp_path)
    capsule_service = RawCapsuleService(
        store=harness.run_store,
        evidence_store=harness.evidence_store,
        capsule_root=tmp_path / "capsules",
    )
    harness.service.capsule_authority_resolver = build_verified_capsule_authority(capsule_service)
    task, _ = harness.schedule(
        completion_policy=AgentTaskCompletionPolicy.EVIDENCE_AND_CAPSULE_REQUIRED
    )
    harness.service.dispatch_due(limit=10)
    running = harness.task_store.get_task(task.task_id, owner="alice")
    assert running.linked_run_id is not None
    harness.finish_run(running.linked_run_id)
    harness.finalize_evidence(running.linked_run_id)
    original_finish = harness.run_store.finish_capsule_build

    def block_old_ready(run_id: str, *, state: CapsuleState, **kwargs):  # type: ignore[no-untyped-def]
        if state is CapsuleState.READY and threading.current_thread().name == "old-runtime":
            published.set()
            release_old.wait(timeout=5)
        return original_finish(run_id, state=state, **kwargs)

    monkeypatch.setattr(harness.run_store, "finish_capsule_build", block_old_ready)
    workers = (
        RuntimeReconcileWorker(
            service=harness.run_service,
            agent_task_service=harness.service,
            capsule_service=capsule_service,
            worker_id="runtime-old",
            task_lease_seconds=30,
        ),
        RuntimeReconcileWorker(
            service=harness.run_service,
            agent_task_service=harness.service,
            capsule_service=capsule_service,
            worker_id="runtime-new",
            task_lease_seconds=30,
        ),
    )
    old_thread = threading.Thread(target=workers[0].tick, name="old-runtime")
    old_thread.start()
    assert published.wait(timeout=5)
    raw = tmp_path / "capsules" / "runs" / running.linked_run_id / "raw"
    published_manifest = (raw / "manifest.json").read_bytes()
    harness.clock.advance(31)
    with harness.control.connect() as connection:
        payload = json.loads(
            connection.execute(
                "SELECT payload_json FROM control_outbox WHERE topic = 'capsule.build.v1'"
            ).fetchone()[0]
        )
    new_thread = threading.Thread(target=workers[1].tick, name="new-runtime")
    new_thread.start()
    new_thread.join(timeout=5)
    release_old.set()
    old_thread.join(timeout=5)

    assert not old_thread.is_alive()
    assert not new_thread.is_alive()
    assert (raw / "manifest.json").read_bytes() == published_manifest
    completed = harness.run_store.get_run(running.linked_run_id)
    assert completed.capsule_state is CapsuleState.READY
    assert completed.capsule_operation_key == payload["operation_key"]
    assert completed.capsule_build_fencing_token >= 2
    with harness.control.connect() as connection:
        state, attempts = connection.execute(
            "SELECT state, attempts FROM control_outbox WHERE topic = 'capsule.build.v1'"
        ).fetchone()
    assert (state, attempts) == ("succeeded", 2)


def test_required_capsule_retry_exhaustion_leaves_authoritative_task_failure(
    tmp_path: Path,
) -> None:
    class FailingCapsuleService:
        def build_raw_capsule(self, run_id: str, **kwargs):  # type: ignore[no-untyped-def]
            raise CapsuleError("Capsule storage unavailable")

    harness = Harness(tmp_path)
    task, _ = harness.schedule(
        completion_policy=AgentTaskCompletionPolicy.EVIDENCE_AND_CAPSULE_REQUIRED
    )
    harness.service.dispatch_due(limit=10)
    running = harness.task_store.get_task(task.task_id, owner="alice")
    assert running.linked_run_id is not None
    harness.finish_run(running.linked_run_id)
    harness.finalize_evidence(running.linked_run_id)
    worker = RuntimeReconcileWorker(
        service=harness.run_service,
        agent_task_service=harness.service,
        capsule_service=FailingCapsuleService(),  # type: ignore[arg-type]
        collection_max_attempts=2,
    )

    first = worker.tick()
    pending = harness.task_store.get_task(task.task_id, owner="alice")
    assert first.capsule_builds_attempted == 1
    assert pending.state is AgentTaskState.RUNNING
    assert pending.gate_state is AgentTaskGateState.AWAITING_CAPSULE
    assert harness.run_store.get_run(running.linked_run_id).capsule_state is CapsuleState.PENDING

    harness.clock.advance(2)
    second = worker.tick()
    failed = harness.task_store.get_task(task.task_id, owner="alice")

    assert second.capsule_builds_attempted == 1
    assert harness.run_store.get_run(running.linked_run_id).capsule_state is CapsuleState.FAILED
    assert failed.state is AgentTaskState.FAILED
    assert failed.result is not None
    assert failed.result.error_code == "CAPSULE.UNAVAILABLE"
    with harness.control.connect() as connection:
        state, attempts = connection.execute(
            "SELECT state, attempts FROM control_outbox WHERE topic = 'capsule.build.v1'"
        ).fetchone()
    assert (state, attempts) == ("dead_letter", 2)


def test_runtime_worker_collection_exhaustion_finishes_task_without_fake_evidence(
    tmp_path: Path,
) -> None:
    class FailingCollector:
        def collect(self, *, run, task_type: str):  # type: ignore[no-untyped-def]
            raise RuntimeError(f"collector unavailable for {task_type}")

    harness = Harness(tmp_path)
    task, _ = harness.schedule()
    harness.service.dispatch_due(limit=10)
    running = harness.task_store.get_task(task.task_id, owner="alice")
    assert running.linked_run_id is not None
    harness.finish_run(running.linked_run_id)
    worker = RuntimeReconcileWorker(
        service=harness.run_service,
        agent_task_service=harness.service,
        task_handler=FailingCollector(),  # type: ignore[arg-type]
        collection_max_attempts=1,
    )

    result = worker.tick()
    failed = harness.task_store.get_task(task.task_id, owner="alice")

    assert result.tasks_checked == 7
    assert result.tasks_succeeded == 0
    assert len(result.task_errors) == 7
    assert failed.state is AgentTaskState.FAILED
    assert failed.result is not None
    assert failed.result.error_code == "EVIDENCE.UNAVAILABLE"
    assert failed.result.evidence_refs == ()
    assert harness.run_store.get_evidence_seal(running.linked_run_id).state.value == "OPEN"


def test_runtime_worker_optional_capsule_failure_does_not_block_evidence_policy(
    tmp_path: Path,
) -> None:
    class FailingCapsuleService:
        def build_raw_capsule(self, run_id: str, **kwargs):  # type: ignore[no-untyped-def]
            raise CapsuleError("optional Capsule backend unavailable")

    harness = Harness(tmp_path)
    task, _ = harness.schedule()
    harness.service.dispatch_due(limit=10)
    running = harness.task_store.get_task(task.task_id, owner="alice")
    assert running.linked_run_id is not None
    harness.finish_run(running.linked_run_id)
    harness.finalize_evidence(running.linked_run_id)
    worker = RuntimeReconcileWorker(
        service=harness.run_service,
        agent_task_service=harness.service,
        capsule_service=FailingCapsuleService(),  # type: ignore[arg-type]
    )

    worker.tick()
    completed = harness.task_store.get_task(task.task_id, owner="alice")

    assert completed.state is AgentTaskState.SUCCEEDED
    assert completed.gate_receipt is not None
    assert completed.gate_receipt.capsule_state == "not_required"
    assert harness.run_store.get_run(running.linked_run_id).capsule_state is CapsuleState.PENDING


def test_two_runtime_workers_publish_one_capsule_and_one_terminal_task(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    capsule_service = RawCapsuleService(
        store=harness.run_store,
        evidence_store=harness.evidence_store,
        capsule_root=tmp_path / "capsules",
    )
    harness.service.capsule_authority_resolver = build_verified_capsule_authority(capsule_service)
    task, _ = harness.schedule(
        completion_policy=AgentTaskCompletionPolicy.EVIDENCE_AND_CAPSULE_REQUIRED
    )
    harness.service.dispatch_due(limit=10)
    running = harness.task_store.get_task(task.task_id, owner="alice")
    assert running.linked_run_id is not None
    harness.finish_run(running.linked_run_id)
    harness.finalize_evidence(running.linked_run_id)
    first_service = harness.service
    harness.restart_task_service()
    second_service = harness.service
    workers = (
        RuntimeReconcileWorker(
            service=harness.run_service,
            agent_task_service=first_service,
            capsule_service=capsule_service,
            worker_id="runtime-a",
        ),
        RuntimeReconcileWorker(
            service=harness.run_service,
            agent_task_service=second_service,
            capsule_service=capsule_service,
            worker_id="runtime-b",
        ),
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda worker: worker.tick(), workers))

    completed = harness.task_store.get_task(task.task_id, owner="alice")
    assert sum(result.capsule_builds_succeeded for result in results) == 1
    assert completed.state is AgentTaskState.SUCCEEDED
    with harness.control.connect() as connection:
        rows = connection.execute(
            "SELECT state, attempts FROM control_outbox WHERE topic = 'capsule.build.v1'"
        ).fetchall()
    assert [tuple(row) for row in rows] == [("succeeded", 1)]


def test_capsule_authority_unavailable_blocks_required_task(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.service.capsule_authority_resolver = None
    task, _ = harness.schedule(
        completion_policy=AgentTaskCompletionPolicy.EVIDENCE_AND_CAPSULE_REQUIRED
    )
    harness.service.dispatch_due(limit=10)
    running = harness.task_store.get_task(task.task_id, owner="alice")
    assert running.linked_run_id is not None
    harness.finish_run(running.linked_run_id)
    harness.finalize_evidence(running.linked_run_id)

    assert harness.service.reconcile_active(limit=10).succeeded == 1
    blocked = harness.task_store.get_task(task.task_id, owner="alice")
    assert blocked.state is AgentTaskState.FAILED
    assert blocked.gate_state is AgentTaskGateState.BLOCKED
    assert blocked.result is not None
    assert blocked.result.error_code == "CAPSULE.AUTHORITY_UNAVAILABLE"


def test_failed_capsule_build_finishes_required_task_as_unavailable(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    task, _ = harness.schedule(
        completion_policy=AgentTaskCompletionPolicy.EVIDENCE_AND_CAPSULE_REQUIRED
    )
    harness.service.dispatch_due(limit=10)
    running = harness.task_store.get_task(task.task_id, owner="alice")
    assert running.linked_run_id is not None
    harness.finish_run(running.linked_run_id)
    harness.finalize_evidence(running.linked_run_id)
    harness.run_store.update_capsule_state(
        running.linked_run_id,
        CapsuleState.FAILED,
        event_type="test.capsule_failed",
    )

    assert harness.service.reconcile_active(limit=10).succeeded == 1
    failed = harness.task_store.get_task(task.task_id, owner="alice")
    assert failed.state is AgentTaskState.FAILED
    assert failed.gate_state is AgentTaskGateState.FAILED
    assert failed.result is not None
    assert failed.result.error_code == "CAPSULE.UNAVAILABLE"


def test_capsule_authority_malformed_reference_blocks_completion(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.service.capsule_authority_resolver = lambda run_id: ""
    task, _ = harness.schedule(
        completion_policy=AgentTaskCompletionPolicy.EVIDENCE_AND_CAPSULE_REQUIRED
    )
    harness.service.dispatch_due(limit=10)
    running = harness.task_store.get_task(task.task_id, owner="alice")
    assert running.linked_run_id is not None
    harness.finish_run(running.linked_run_id)
    harness.finalize_evidence(running.linked_run_id)
    harness.run_store.update_capsule_state(
        running.linked_run_id,
        CapsuleState.READY,
        event_type="test.capsule_ready",
    )

    assert harness.service.reconcile_active(limit=10).succeeded == 1
    blocked = harness.task_store.get_task(task.task_id, owner="alice")
    assert blocked.state is AgentTaskState.FAILED
    assert blocked.gate_state is AgentTaskGateState.BLOCKED
    assert blocked.result is not None
    assert blocked.result.error_code == "CAPSULE.INTEGRITY_FAILED"


def test_integrity_failure_blocks_task(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    task, _ = harness.schedule()
    harness.service.dispatch_due(limit=10)
    running = harness.task_store.get_task(task.task_id, owner="alice")
    assert running.linked_run_id is not None
    harness.finish_run(running.linked_run_id)
    harness.finalize_evidence(running.linked_run_id)
    evidence_path = harness.evidence_store.run_root(running.linked_run_id) / "logs/stdout.txt"
    evidence_path.write_text("tampered\n", encoding="utf-8")

    assert harness.service.reconcile_active(limit=10).succeeded == 1
    blocked = harness.task_store.get_task(task.task_id, owner="alice")
    assert blocked.state is AgentTaskState.FAILED
    assert blocked.gate_state is AgentTaskGateState.BLOCKED
    assert blocked.result is not None
    assert blocked.result.error_code == "EVIDENCE.INTEGRITY_FAILED"


def test_terminal_seal_rejects_evidence_tamper_after_first_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = Harness(tmp_path)
    task, _ = harness.schedule()
    harness.service.dispatch_due(limit=10)
    running = harness.task_store.get_task(task.task_id, owner="alice")
    assert running.linked_run_id is not None
    harness.finish_run(running.linked_run_id)
    harness.finalize_evidence(running.linked_run_id)
    evidence_path = harness.evidence_store.run_root(running.linked_run_id) / "logs/stdout.txt"
    assert harness.service.evidence_binder is not None
    original_verify = harness.service.evidence_binder.verify_terminal_gate
    calls = 0
    mutation_error: OSError | None = None

    def verify_then_tamper(*args, **kwargs):
        nonlocal calls, mutation_error
        calls += 1
        receipt = original_verify(*args, **kwargs)
        if calls == 1:
            try:
                evidence_path.write_text("tampered after first gate\n", encoding="utf-8")
            except OSError as exc:
                mutation_error = exc
        return receipt

    monkeypatch.setattr(
        harness.service.evidence_binder,
        "verify_terminal_gate",
        verify_then_tamper,
    )

    harness.service.reconcile_active(limit=10)
    completed = harness.task_store.get_task(task.task_id, owner="alice")

    assert calls >= 1
    assert isinstance(mutation_error, PermissionError)
    assert completed.state is AgentTaskState.SUCCEEDED
    assert completed.gate_state is AgentTaskGateState.COMPLETED


def test_terminal_run_provenance_trigger_rejects_change_after_first_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = Harness(tmp_path)
    task, _ = harness.schedule()
    harness.service.dispatch_due(limit=10)
    running = harness.task_store.get_task(task.task_id, owner="alice")
    assert running.linked_run_id is not None
    harness.finish_run(running.linked_run_id)
    harness.finalize_evidence(running.linked_run_id)
    assert harness.service.evidence_binder is not None
    original_verify = harness.service.evidence_binder.verify_terminal_gate
    calls = 0
    update_rejected = False

    def verify_then_mutate_run(*args, **kwargs):
        nonlocal calls, update_rejected
        calls += 1
        receipt = original_verify(*args, **kwargs)
        if calls == 1:
            try:
                with harness.run_store.connect() as connection:
                    connection.execute(
                        "UPDATE runs SET source_revision = ? WHERE run_id = ?",
                        ("workspace-source-tampered", running.linked_run_id),
                    )
            except sqlite3.IntegrityError:
                update_rejected = True
        return receipt

    monkeypatch.setattr(
        harness.service.evidence_binder,
        "verify_terminal_gate",
        verify_then_mutate_run,
    )

    harness.service.reconcile_active(limit=10)
    completed = harness.task_store.get_task(task.task_id, owner="alice")

    assert calls >= 1
    assert update_rejected is True
    assert completed.state is AgentTaskState.SUCCEEDED
    assert completed.gate_state is AgentTaskGateState.COMPLETED


def test_missing_evidence_authority_blocks_task(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    task, _ = harness.schedule()
    harness.service.dispatch_due(limit=10)
    running = harness.task_store.get_task(task.task_id, owner="alice")
    assert running.linked_run_id is not None
    harness.finish_run(running.linked_run_id)
    harness.finalize_evidence(running.linked_run_id)
    harness.service.evidence_binder = None

    assert harness.service.reconcile_active(limit=10).succeeded == 1
    blocked = harness.task_store.get_task(task.task_id, owner="alice")
    assert blocked.state is AgentTaskState.FAILED
    assert blocked.gate_state is AgentTaskGateState.BLOCKED
    assert blocked.result is not None
    assert blocked.result.error_code == "EVIDENCE.AUTHORITY_UNAVAILABLE"


def test_evidence_authority_runtime_failure_terminates_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = Harness(tmp_path)
    task, _ = harness.schedule()
    harness.service.dispatch_due(limit=10)
    running = harness.task_store.get_task(task.task_id, owner="alice")
    assert running.linked_run_id is not None
    harness.finish_run(running.linked_run_id)
    harness.finalize_evidence(running.linked_run_id)
    assert harness.service.evidence_binder is not None

    def authority_failure(*args, **kwargs):
        raise RuntimeError("evidence database unavailable")

    monkeypatch.setattr(
        harness.service.evidence_binder,
        "verify_terminal_gate",
        authority_failure,
    )

    assert harness.service.reconcile_active(limit=10).succeeded == 1
    blocked = harness.task_store.get_task(task.task_id, owner="alice")
    assert blocked.state is AgentTaskState.FAILED
    assert blocked.gate_state is AgentTaskGateState.BLOCKED
    assert blocked.result is not None
    assert blocked.result.error_code == "EVIDENCE.AUTHORITY_UNAVAILABLE"
    assert "权威校验" in blocked.result.message


def test_collection_exhaustion_finishes_as_evidence_unavailable(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    task, _ = harness.schedule()
    harness.service.dispatch_due(limit=10)
    running = harness.task_store.get_task(task.task_id, owner="alice")
    assert running.linked_run_id is not None
    harness.finish_run(running.linked_run_id)
    with harness.run_store.connect() as connection:
        connection.execute(
            "UPDATE runs SET collection_state = 'failed' WHERE run_id = ?",
            (running.linked_run_id,),
        )

    assert harness.service.reconcile_active(limit=10).succeeded == 1
    failed = harness.task_store.get_task(task.task_id, owner="alice")
    assert failed.state is AgentTaskState.FAILED
    assert failed.gate_state is AgentTaskGateState.FAILED
    assert failed.result is not None
    assert failed.result.error_code == "EVIDENCE.UNAVAILABLE"


def test_failed_run_with_collection_exhaustion_reports_evidence_unavailable(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    task, _ = harness.schedule()
    harness.service.dispatch_due(limit=10)
    running = harness.task_store.get_task(task.task_id, owner="alice")
    assert running.linked_run_id is not None
    harness.finish_run(running.linked_run_id, exit_code="1:0")
    with harness.run_store.connect() as connection:
        connection.execute(
            "UPDATE runs SET collection_state = 'failed' WHERE run_id = ?",
            (running.linked_run_id,),
        )

    assert harness.service.reconcile_active(limit=10).succeeded == 1
    failed = harness.task_store.get_task(task.task_id, owner="alice")
    assert failed.state is AgentTaskState.FAILED
    assert failed.result is not None
    assert failed.result.error_code == "EVIDENCE.UNAVAILABLE"


def test_orphaned_run_without_collection_tasks_terminates_instead_of_polling(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    task, _ = harness.schedule()
    harness.service.dispatch_due(limit=10)
    running = harness.task_store.get_task(task.task_id, owner="alice")
    assert running.linked_run_id is not None
    harness.run_store.update_state(
        running.linked_run_id,
        RunState.ORPHANED,
        event_type="test.orphaned",
    )

    assert harness.service.reconcile_active(limit=10).succeeded == 1
    failed = harness.task_store.get_task(task.task_id, owner="alice")
    assert failed.state is AgentTaskState.FAILED
    assert failed.gate_state is AgentTaskGateState.ORPHANED
    assert failed.result is not None
    assert failed.result.error_code == "VALIDATION.RUN_FAILED"


def test_ready_followup_is_idempotent_after_finalizer_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = Harness(tmp_path)
    task, _ = harness.schedule()
    harness.service.dispatch_due(limit=10)
    running = harness.task_store.get_task(task.task_id, owner="alice")
    assert running.linked_run_id is not None
    harness.finish_run(running.linked_run_id)
    harness.finalize_evidence(running.linked_run_id)
    original_finalize = harness.task_store.finalize_task
    crashed = False

    def finalize_then_crash(*args, **kwargs):
        nonlocal crashed
        completed = original_finalize(*args, **kwargs)
        if not crashed:
            crashed = True
            raise RuntimeError("simulated crash after task finalizer")
        return completed

    monkeypatch.setattr(harness.task_store, "finalize_task", finalize_then_crash)
    first = harness.service.reconcile_active(limit=10)
    ready = harness.service.dispatch_due(limit=10)
    replay = harness.service.dispatch_due(limit=10)
    followups = [
        turn
        for turn in harness.session_store.list_recoverable_turns(limit=10)
        if turn.request_key == f"agent-task:{task.task_id}:ready"
    ]

    assert len(first.errors) == 1
    assert ready.succeeded == 1
    assert replay.checked == 0
    assert len(followups) == 1


def test_ready_enqueue_failure_is_recovered_after_terminal_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = Harness(tmp_path)
    task, _ = harness.schedule()
    harness.service.dispatch_due(limit=10)
    running = harness.task_store.get_task(task.task_id, owner="alice")
    assert running.linked_run_id is not None
    harness.finish_run(running.linked_run_id)
    harness.finalize_evidence(running.linked_run_id)
    original_enqueue = harness.control.enqueue
    failed_once = False

    def fail_first_ready_enqueue(*args, **kwargs):
        nonlocal failed_once
        if kwargs.get("topic") == "agent.task.ready.v1" and not failed_once:
            failed_once = True
            raise RuntimeError("ready outbox unavailable")
        return original_enqueue(*args, **kwargs)

    monkeypatch.setattr(harness.control, "enqueue", fail_first_ready_enqueue)
    first = harness.service.reconcile_active(limit=10)
    terminal = harness.task_store.get_task(task.task_id, owner="alice")
    assert terminal.state is AgentTaskState.SUCCEEDED
    assert len(first.errors) == 1

    monkeypatch.setattr(harness.control, "enqueue", original_enqueue)
    harness.restart_task_service()
    harness.service.reconcile_active(limit=10)
    harness.service.dispatch_due(limit=10)
    harness.service.reconcile_active(limit=10)
    harness.service.dispatch_due(limit=10)
    followups = [
        turn
        for turn in harness.session_store.list_recoverable_turns(limit=10)
        if turn.request_key == f"agent-task:{task.task_id}:ready"
    ]

    assert len(followups) == 1


def test_ready_intent_is_not_visible_before_terminal_finalizer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = Harness(tmp_path)
    task, _ = harness.schedule()
    harness.service.dispatch_due(limit=10)
    running = harness.task_store.get_task(task.task_id, owner="alice")
    assert running.linked_run_id is not None
    harness.finish_run(running.linked_run_id)
    harness.finalize_evidence(running.linked_run_id)
    original_finalize = harness.task_store.finalize_task

    def crash_before_finalize(*args, **kwargs):
        raise RuntimeError("simulated crash before task finalizer")

    monkeypatch.setattr(harness.task_store, "finalize_task", crash_before_finalize)
    failed = harness.service.reconcile_active(limit=10)

    with harness.control.connect() as connection:
        ready_count = connection.execute(
            "SELECT COUNT(*) FROM control_outbox WHERE topic = ?",
            ("agent.task.ready.v1",),
        ).fetchone()[0]
    assert len(failed.errors) == 1
    assert ready_count == 0

    monkeypatch.setattr(harness.task_store, "finalize_task", original_finalize)
    harness.clock.advance(31)
    replay = harness.service.reconcile_active(limit=10)
    ready = harness.service.dispatch_due(limit=10)
    followups = [
        turn
        for turn in harness.session_store.list_recoverable_turns(limit=10)
        if turn.request_key == f"agent-task:{task.task_id}:ready"
    ]

    assert replay.succeeded == 1
    assert ready.succeeded == 1
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


def test_execute_ack_crash_replays_identical_schedule_receipt_without_resubmit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = Harness(tmp_path)
    task, _ = harness.schedule()
    original_acknowledge = harness.service._acknowledge
    crashed = False

    def crash_before_ack(message):
        nonlocal crashed
        if message.topic == "agent.task.execute.v1" and not crashed:
            crashed = True
            raise RuntimeError("crash before execute ack")
        return original_acknowledge(message)

    monkeypatch.setattr(harness.service, "_acknowledge", crash_before_ack)
    first = harness.service.dispatch_due(limit=1)
    scheduled = harness.task_store.get_task(task.task_id, owner="alice")
    assert scheduled.schedule_receipt is not None
    first_receipt = scheduled.schedule_receipt
    first_receipt_bytes = json.dumps(
        agent_task_schedule_receipt_payload(first_receipt),
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    assert len(first.errors) == 1

    monkeypatch.setattr(harness.service, "_acknowledge", original_acknowledge)
    harness.clock.advance(1)
    replay = harness.service.dispatch_due(limit=1)
    persisted = harness.task_store.get_task(task.task_id, owner="alice")
    runs, _ = harness.run_store.list_runs_page(owner="alice")

    assert replay.succeeded == 1
    assert replay.errors == []
    assert persisted.schedule_receipt == first_receipt
    assert persisted.schedule_receipt is not None
    assert (
        json.dumps(
            agent_task_schedule_receipt_payload(persisted.schedule_receipt),
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        == first_receipt_bytes
    )
    assert len(runs) == 1
    assert len(harness.run_service.dispatch_due_submissions(limit=10).succeeded) == 1
    assert harness.backend._next_job_id == 1001


def test_validation_tool_uses_server_approved_envelope_and_terminates_turn(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    handler = harness.service.build_tool_handler(lambda owner, session_id: _envelope())
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
    handler = harness.service.build_tool_handler(lambda owner, session_id: _envelope())
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
    harness.finalize_evidence(run.run_id)
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
