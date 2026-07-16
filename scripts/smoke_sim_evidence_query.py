from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from pilot107.adapters.slurm import (
    DockerComposeExecutor,
    DockerComposeTarget,
    DockerSimulatorCommandBackend,
)
from pilot107.api.evidence_query import EvidenceQueryService
from pilot107.core.resources import ResourcePlan
from pilot107.core.run_service import RunService, RunSubmitRequest
from pilot107.core.run_store import RunStore
from pilot107.core.states import CollectionState, RunState
from pilot107.worker.evidence import DockerSlurmEvidenceCollector, EvidenceStore
from pilot107.worker.runtime_worker import RuntimeReconcileWorker


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    compose_dir = root / "simulator" / "compose"
    runtime_dir = root / "data" / "phase0"
    store = RunStore(runtime_dir / "pilot107.db")
    evidence_store = EvidenceStore(runtime_dir / "evidence")
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
        timeout_seconds=20.0,
    )
    collector = DockerSlurmEvidenceCollector(
        store=evidence_store,
        executor=executor,
        allowed_roots=["/public/home/alice"],
        run_store=store,
        timeout_seconds=20.0,
    )
    service = RunService(store=store, backend=backend)
    worker = RuntimeReconcileWorker(
        service=service,
        batch_size=20,
        task_handler=collector,
        worker_id="smoke-evidence-query-worker",
    )

    run = service.submit(
        RunSubmitRequest(
            owner="alice",
            workdir=Path("/public/home/alice"),
            script="#!/bin/bash\nhostname\necho query-evidence\n",
            resource_plan=ResourcePlan(
                partition="Students",
                qos="qos_stu_medium_2gpu",
                nodes=1,
                ntasks=1,
                cpus_per_task=1,
                time_limit="00:05:00",
            ),
        )
    )

    final_run = run
    for _ in range(30):
        worker.tick()
        final_run = store.get_run(run.run_id)
        if (
            final_run.state in {RunState.SUCCEEDED, RunState.FAILED, RunState.CANCELLED}
            and final_run.collection_state == CollectionState.SUCCEEDED
        ):
            break
        time.sleep(1)

    payload = EvidenceQueryService(store=store, evidence_store=evidence_store).get_evidence_tree(
        run.run_id
    )
    paths = _flatten_paths(payload["tree"])
    required = {
        "submission/slurm_submit_response.json",
        "submission/user_script.original.sh",
        "submission/submitted_script.resolved.sh",
        "submission/execution_wrapper.generated.sh",
        "slurm/accounting.json",
        "slurm/job_detail.json",
        "logs/stdout.tail.json",
        "logs/stderr.tail.json",
        "environment/summary.json",
        "outputs/inventory.json",
        "derived/result_summary.v1.json",
        "manifest/manifest.json",
    }
    missing = required - paths
    object_paths = {obj["logical_path"] for obj in payload["objects"]}
    missing_objects = required - object_paths - {"manifest/manifest.json"}
    if final_run.collection_state != CollectionState.SUCCEEDED or missing or missing_objects:
        print(
            f"unexpected evidence query result: collection={final_run.collection_state} "
            f"missing={sorted(missing)} missing_objects={sorted(missing_objects)} "
            f"payload={json.dumps(payload, ensure_ascii=False)[:1000]}",
            file=sys.stderr,
        )
        return 1

    print(
        f"evidence query smoke {run.run_id} job={final_run.job_id} "
        f"collection={payload['collection_state']} "
        f"files={len(paths)} objects={len(payload['objects'])}"
    )
    print("tasks=" + ",".join(f"{task['task_type']}:{task['state']}" for task in payload["tasks"]))
    print("paths=" + ",".join(sorted(required)))
    return 0


def _flatten_paths(node: dict) -> set[str]:
    paths: set[str] = set()
    if node["kind"] == "file":
        paths.add(node["logical_path"])
    for child in node.get("children", []):
        paths |= _flatten_paths(child)
    return paths


if __name__ == "__main__":
    raise SystemExit(main())
