#!/usr/bin/env bash
set -euo pipefail

task15_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
compose=(
  docker compose
  --env-file "$task15_root/simulator/compose/.env.example"
  -f "$task15_root/simulator/compose/compose.yml"
)

"${compose[@]}" up -d mariadb slurmdbd slurmctld worker-1 worker-2 login-node-sim >/dev/null
for _attempt in $(seq 1 60); do
  if "${compose[@]}" exec -T --user alice login-node-sim \
    sinfo --noheader --format '%P' >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
"${compose[@]}" exec -T --user alice login-node-sim \
  sinfo --noheader --format '%P' >/dev/null

cd "$task15_root"
PILOT107_TASK15_ROOT="$task15_root" PYTHONPATH="$task15_root/src" uv run python - <<'PY'
from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

from pilot107.adapters.slurm import (
    DockerComposeExecutor,
    DockerComposeTarget,
    DockerSimulatorCommandBackend,
)
from pilot107.core.resources import ArraySpec, ResourcePlan
from pilot107.core.run_service import RunService, RunSubmitRequest
from pilot107.core.run_store import RunStore
from pilot107.core.states import RunState
from pilot107.core.workflow_manifest import (
    ArtifactTruth,
    WorkflowArtifactGateError,
    WorkflowManifest,
    WorkflowResourceCeiling,
    WorkflowResourceLimitExceeded,
    WorkflowService,
    WorkflowStage,
)

root = Path(os.environ["PILOT107_TASK15_ROOT"])
compose = root / "simulator" / "compose"
executor = DockerComposeExecutor(
    DockerComposeTarget(
        compose_file=compose / "compose.yml",
        env_file=compose / ".env.example",
        workdir=compose,
    )
)
backend = DockerSimulatorCommandBackend(
    executor=executor,
    allowed_roots=["/public/home/alice"],
    timeout_seconds=20,
)
workdir = Path("/public/home/alice/pilot107-task15-d1")
executor.run(["mkdir", "-p", str(workdir)], user="alice", timeout_seconds=10)


def plan(*, array: str | None = None, gpus: int = 0) -> ResourcePlan:
    return ResourcePlan(
        partition="Students",
        qos="qos_stu_default",
        nodes=1,
        ntasks=1,
        cpus_per_task=1,
        memory_value=128,
        memory_unit="M",
        gpus_total=gpus,
        time_limit="00:05:00",
        array=None if array is None else ArraySpec(array, max_concurrency=4),
    )


def request(name: str, script: str, *, array: str | None = None) -> RunSubmitRequest:
    return RunSubmitRequest(
        owner="alice",
        workdir=workdir,
        script=f"#!/bin/bash\nset -Eeuo pipefail\n{script}\n",
        job_name=f"task15-{name}",
        resource_plan=plan(array=array),
    )


def truth(task: int) -> ArtifactTruth:
    return ArtifactTruth(
        task_index=task,
        artifact_path=f"shards/task_{task}.bin",
        artifact_sha256=f"live-artifact-{task}",
        metadata_path=f"metadata/task_{task}.json",
        metadata_sha256=f"live-metadata-{task}",
        complete_marker_path=f"complete/task_{task}.COMPLETE",
        complete=True,
    )


array_body = """
mkdir -p shards metadata complete
task="${SLURM_ARRAY_TASK_ID:?}"
printf 'artifact-%s\\n' "$task" > "shards/task_${task}.bin"
printf '{\"task\":%s}\\n' "$task" > "metadata/task_${task}.json"
printf 'ok\\n' > "complete/task_${task}.COMPLETE"
""".strip()

with tempfile.TemporaryDirectory(prefix="pilot107-task15-d1-") as temporary:
    store = RunStore(Path(temporary) / "pilot107.db")
    runs = RunService(store=store, backend=backend)
    service = WorkflowService(store=store, run_service=runs)
    manifest = WorkflowManifest(
        workflow_id="wf-task15-d1",
        owner="alice",
        stages=(
            WorkflowStage("preflight", "preflight", request("preflight", "test -w .")),
            WorkflowStage(
                "array",
                "array",
                request("array", array_body, array="0-11"),
                dependencies=("preflight",),
                array_tasks=tuple(range(12)),
            ),
            WorkflowStage(
                "merge",
                "merge",
                request("merge", "cat shards/task_*.bin > merged.bin"),
                dependencies=("array",),
            ),
        ),
    )
    service.validate(
        manifest,
        WorkflowResourceCeiling(cpus=8, memory_mib=2048, gpus=0),
    )
    try:
        service.validate(
            WorkflowManifest(
                workflow_id="wf-task15-peak",
                owner="alice",
                stages=(
                    WorkflowStage("gpu-left", "array", request("gpu-left", "true", array="0-7"), array_tasks=tuple(range(8))),
                    WorkflowStage("gpu-right", "array", request("gpu-right", "true", array="0-7"), array_tasks=tuple(range(8))),
                ),
            ),
            WorkflowResourceCeiling(cpus=7, memory_mib=4096, gpus=0),
        )
    except WorkflowResourceLimitExceeded:
        pass
    else:
        raise RuntimeError("dependency-layer resource ceiling did not fail closed")

    current = service.resume(service.create(manifest).workflow_id, actor="alice")
    array_run = store.get_run(current.stage("array").decisions[0].run_id)
    dependency_events = [
        event
        for event in store.list_events(array_run.run_id)
        if event.event_type == "workflow.dependencies_resolved"
    ]
    if not dependency_events or not dependency_events[-1].payload.get("dependency_job_ids"):
        raise RuntimeError("array stage was not submitted with afterok dependency")

    for _ in range(90):
        current = service.reconcile(current.workflow_id, actor="alice")
        if current.stage("array").decisions[0].run_state == RunState.SUCCEEDED.value:
            break
        time.sleep(1)
    else:
        raise RuntimeError("initial array did not reach SUCCEEDED")

    missing_paths = [
        str(workdir / kind / f"task_{task}.{suffix}")
        for task in range(8, 12)
        for kind, suffix in (("shards", "bin"), ("metadata", "json"), ("complete", "COMPLETE"))
    ]
    executor.run(["rm", "-f", *missing_paths], user="alice", timeout_seconds=10)
    current = service.record_artifact_truth(
        current.workflow_id,
        stage_id="array",
        truth=tuple(truth(task) for task in range(8)),
        actor="alice",
    )
    try:
        service.resume(current.workflow_id, actor="alice")
    except WorkflowArtifactGateError:
        pass
    else:
        raise RuntimeError("merge advanced with missing artifact truth")

    recovery = service.plan_recovery(current.workflow_id, actor="alice")
    if recovery.array_expression != "8-11":
        raise RuntimeError(f"unexpected recovery expression: {recovery.array_expression}")
    current = service.recover(current.workflow_id, actor="alice")
    recovery_run_id = current.stage("array").decisions[-1].run_id
    for _ in range(90):
        current = service.reconcile(current.workflow_id, actor="alice")
        if store.get_run(recovery_run_id).state == RunState.SUCCEEDED:
            break
        time.sleep(1)
    else:
        raise RuntimeError("recovery array did not reach SUCCEEDED")

    current = service.record_artifact_truth(
        current.workflow_id,
        stage_id="array",
        truth=tuple(truth(task) for task in range(12)),
        actor="alice",
    )
    current = service.resume(current.workflow_id, actor="alice")
    merge_run_id = current.stage("merge").decisions[0].run_id
    for _ in range(90):
        current = service.reconcile(current.workflow_id, actor="alice")
        if store.get_run(merge_run_id).state == RunState.SUCCEEDED:
            break
        time.sleep(1)
    else:
        raise RuntimeError("merge did not reach SUCCEEDED")

    cancel_manifest = service.create(
        WorkflowManifest(
            workflow_id="wf-task15-cancel",
            owner="alice",
            stages=(
                WorkflowStage("preflight", "preflight", request("cancel", "sleep 30")),
            ),
        )
    )
    cancel_manifest = service.resume(cancel_manifest.workflow_id, actor="alice")
    cancelled = service.cancel(cancel_manifest.workflow_id, actor="alice")
    if cancelled != service.status(cancelled.workflow_id, actor="alice"):
        raise RuntimeError("cancel and status did not return identical manifest truth")
    if len(service.resume(cancelled.workflow_id, actor="alice").stage("preflight").decisions) != 1:
        raise RuntimeError("cancelled workflow was resubmitted")

    print(
        "task15 D1 PASS "
        f"workflow={current.workflow_id} recovery={recovery.array_expression} "
        f"merge={store.get_run(merge_run_id).state.value} cancel={cancelled.state}"
    )
PY
