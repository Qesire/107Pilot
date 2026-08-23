from __future__ import annotations

from pathlib import Path

import pytest

from pilot107.core.resources import ArraySpec, ResourcePlan
from pilot107.core.run_service import RunSubmitRequest
from pilot107.core.run_store import RunStore
from pilot107.core.workflow_manifest import (
    WorkflowManifest,
    WorkflowManifestConflict,
    WorkflowResourceCeiling,
    WorkflowResourceLimitExceeded,
    WorkflowService,
    WorkflowStage,
)


def test_manifest_round_trip_preserves_versioned_stage_truth(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "pilot107.db")
    service = WorkflowService(store=store, run_service=None)
    created = service.create(
        WorkflowManifest(
            workflow_id="wf-round-trip",
            owner="alice",
            stages=(
                WorkflowStage(
                    stage_id="array",
                    kind="array",
                    request=_request(array="0-3", array_concurrency=2, gpus=1),
                    array_tasks=(0, 1, 2, 3),
                ),
            ),
        )
    )

    loaded = service.status(created.workflow_id, actor="alice")

    assert loaded.version == 1
    assert loaded.stages[0].array_tasks == (0, 1, 2, 3)
    assert loaded.stages[0].request.resource_plan.array == ArraySpec(
        expression="0-3", max_concurrency=2
    )


def test_manifest_compare_and_swap_rejects_stale_decision(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "pilot107.db")
    service = WorkflowService(store=store, run_service=None)
    first = service.create(
        WorkflowManifest(
            workflow_id="wf-fence",
            owner="alice",
            stages=(WorkflowStage("preflight", "preflight", _request()),),
        )
    )
    service.save(first, expected_version=1)

    with pytest.raises(WorkflowManifestConflict):
        service.save(first, expected_version=1)


def test_dependency_layer_peak_is_checked_before_submit(tmp_path: Path) -> None:
    service = WorkflowService(
        store=RunStore(tmp_path / "pilot107.db"),
        run_service=None,
    )
    manifest = WorkflowManifest(
        workflow_id="wf-peak",
        owner="alice",
        stages=(
            WorkflowStage(
                "left",
                "array",
                _request(array="0-7", array_concurrency=4, gpus=1),
                array_tasks=tuple(range(8)),
            ),
            WorkflowStage(
                "right",
                "array",
                _request(array="0-7", array_concurrency=4, gpus=1),
                array_tasks=tuple(range(8)),
            ),
        ),
    )

    with pytest.raises(WorkflowResourceLimitExceeded) as caught:
        service.validate(
            manifest,
            WorkflowResourceCeiling(cpus=16, memory_mib=4096, gpus=4),
        )

    assert caught.value.layer == ("left", "right")
    assert caught.value.requested_gpus == 8
    assert service.store.list_runs_page(owner="alice")[0] == []


def test_cycle_and_unknown_dependency_fail_closed(tmp_path: Path) -> None:
    service = WorkflowService(
        store=RunStore(tmp_path / "pilot107.db"),
        run_service=None,
    )
    cyclic = WorkflowManifest(
        workflow_id="wf-cycle",
        owner="alice",
        stages=(
            WorkflowStage("a", "preflight", _request(), dependencies=("b",)),
            WorkflowStage("b", "merge", _request(), dependencies=("a",)),
        ),
    )
    unknown = WorkflowManifest(
        workflow_id="wf-unknown",
        owner="alice",
        stages=(WorkflowStage("a", "merge", _request(), dependencies=("missing",)),),
    )

    with pytest.raises(ValueError, match="cycle"):
        service.validate(cyclic, WorkflowResourceCeiling.unbounded())
    with pytest.raises(ValueError, match="unknown"):
        service.validate(unknown, WorkflowResourceCeiling.unbounded())


def _request(
    *, array: str | None = None, array_concurrency: int | None = None, gpus: int = 0
) -> RunSubmitRequest:
    return RunSubmitRequest(
        owner="alice",
        workdir=Path("/public/home/alice/experiment"),
        script="#!/bin/bash\ntrue\n",
        resource_plan=ResourcePlan(
            partition="debug",
            qos="normal",
            nodes=1,
            ntasks=1,
            cpus_per_task=2,
            memory_value=512,
            memory_unit="M",
            gpus_total=gpus,
            time_limit="00:05:00",
            array=None if array is None else ArraySpec(array, max_concurrency=array_concurrency),
        ),
    )
