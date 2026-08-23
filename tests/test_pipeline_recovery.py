from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from pilot107.adapters.slurm import InMemorySlurmBackend
from pilot107.core.resources import ArraySpec, ResourcePlan
from pilot107.core.run_service import RunService, RunSubmitRequest
from pilot107.core.run_store import RunStore
from pilot107.core.states import RunState
from pilot107.core.workflow_manifest import (
    ArtifactTruth,
    WorkflowArtifactGateError,
    WorkflowManifest,
    WorkflowService,
    WorkflowStage,
)


@pytest.fixture
def harness(tmp_path: Path) -> tuple[WorkflowService, InMemorySlurmBackend]:
    store = RunStore(tmp_path / "pilot107.db")
    backend = InMemorySlurmBackend()
    runs = RunService(store=store, backend=backend)
    return WorkflowService(store=store, run_service=runs), backend


def test_recovery_resubmits_only_missing_array_tasks(
    harness: tuple[WorkflowService, InMemorySlurmBackend],
) -> None:
    service, backend = harness
    manifest = service.create(_pipeline())
    manifest = service.resume(manifest.workflow_id, actor="alice")
    manifest = _finish_stage(service, backend, manifest, "preflight")
    manifest = service.resume(manifest.workflow_id, actor="alice")
    manifest = _finish_stage(service, backend, manifest, "array")
    service.record_artifact_truth(
        manifest.workflow_id,
        stage_id="array",
        truth=tuple(_truth(task) for task in range(8)),
        actor="alice",
    )

    reconciled = service.reconcile(manifest.workflow_id, actor="alice")
    retry = service.plan_recovery(reconciled.workflow_id, actor="alice")

    assert retry.array_expression == "8-11"
    assert retry.missing_tasks == (8, 9, 10, 11)
    assert retry.reuses_verified_tasks == tuple(range(8))
    recovered = service.recover(reconciled.workflow_id, actor="alice")
    decision = recovered.stage("array").decisions[-1]
    assert decision.submitted_tasks == (8, 9, 10, 11)
    assert decision.reused_verified_tasks == tuple(range(8))
    assert decision.recovery_attempt == 1
    run = service.store.get_run(decision.run_id)
    assert run.workflow["manifest"] == {
        "workflow_id": "wf-experiment",
        "stage_id": "array",
        "stage_kind": "array",
        "recovery_attempt": 1,
        "submitted_tasks": [8, 9, 10, 11],
        "reused_verified_tasks": list(range(8)),
    }
    repeated = service.resume(recovered.workflow_id, actor="alice")
    assert len(repeated.stage("array").decisions) == 2
    assert repeated.stage("array").decisions[-1].run_id == decision.run_id


def test_merge_is_fail_closed_until_every_task_has_verified_truth(
    harness: tuple[WorkflowService, InMemorySlurmBackend],
) -> None:
    service, backend = harness
    manifest = service.create(_pipeline(task_count=2))
    manifest = service.resume(manifest.workflow_id, actor="alice")
    manifest = _finish_stage(service, backend, manifest, "preflight")
    manifest = service.resume(manifest.workflow_id, actor="alice")
    manifest = _finish_stage(service, backend, manifest, "array")
    service.record_artifact_truth(
        manifest.workflow_id,
        stage_id="array",
        truth=(_truth(0), replace(_truth(1), metadata_sha256=None)),
        actor="alice",
    )

    with pytest.raises(WorkflowArtifactGateError, match="task 1"):
        service.resume(manifest.workflow_id, actor="alice")
    assert service.status(manifest.workflow_id, actor="alice").stage("merge").decisions == ()

    service.record_artifact_truth(
        manifest.workflow_id,
        stage_id="array",
        truth=(_truth(0), _truth(1)),
        actor="alice",
    )
    advanced = service.resume(manifest.workflow_id, actor="alice")
    assert advanced.stage("merge").decisions[0].job_id is not None


def test_cancel_and_repeated_resume_use_the_same_manifest(
    harness: tuple[WorkflowService, InMemorySlurmBackend],
) -> None:
    service, _backend = harness
    created = service.create(_pipeline(task_count=2))
    running = service.resume(created.workflow_id, actor="alice")
    repeated = service.resume(created.workflow_id, actor="alice")

    assert len(repeated.stage("preflight").decisions) == 1
    assert (
        repeated.stage("preflight").decisions[0].run_id
        == running.stage("preflight").decisions[0].run_id
    )
    cancelled = service.cancel(created.workflow_id, actor="alice")
    status = service.status(created.workflow_id, actor="alice")
    resumed = service.resume(created.workflow_id, actor="alice")
    assert cancelled == status == resumed
    assert status.state == "cancelled"
    run_id = status.stage("preflight").decisions[0].run_id
    assert service.store.get_run(run_id).state is RunState.CANCELLED


def _pipeline(*, task_count: int = 12) -> WorkflowManifest:
    tasks = tuple(range(task_count))
    return WorkflowManifest(
        workflow_id="wf-experiment",
        owner="alice",
        stages=(
            WorkflowStage("preflight", "preflight", _request("preflight")),
            WorkflowStage(
                "array",
                "array",
                _request("array", array=f"0-{task_count - 1}"),
                dependencies=("preflight",),
                array_tasks=tasks,
            ),
            WorkflowStage(
                "merge",
                "merge",
                _request("merge"),
                dependencies=("array",),
            ),
        ),
    )


def _request(name: str, *, array: str | None = None) -> RunSubmitRequest:
    return RunSubmitRequest(
        owner="alice",
        workdir=Path("/public/home/alice/experiment"),
        script=f"#!/bin/bash\necho {name}\n",
        job_name=name,
        resource_plan=ResourcePlan(
            partition="debug",
            qos="normal",
            nodes=1,
            ntasks=1,
            cpus_per_task=1,
            memory_value=128,
            memory_unit="M",
            time_limit="00:05:00",
            array=None if array is None else ArraySpec(array, max_concurrency=4),
        ),
    )


def _truth(task: int) -> ArtifactTruth:
    return ArtifactTruth(
        task_index=task,
        artifact_path=f"shards/task_{task}.bin",
        artifact_sha256=f"artifact-{task}",
        metadata_path=f"metadata/task_{task}.json",
        metadata_sha256=f"metadata-{task}",
        complete_marker_path=f"complete/task_{task}.COMPLETE",
        complete=True,
    )


def _finish_stage(
    service: WorkflowService,
    backend: InMemorySlurmBackend,
    manifest: WorkflowManifest,
    stage_id: str,
) -> WorkflowManifest:
    decision = manifest.stage(stage_id).decisions[-1]
    assert decision.job_id is not None
    backend.advance_job(job_id=decision.job_id, raw_state="COMPLETED", exit_code="0:0")
    return service.reconcile(manifest.workflow_id, actor="alice")
