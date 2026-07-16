from __future__ import annotations

import json
import sys
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from pilot107.adapters.slurm import (
    DockerComposeExecutor,
    DockerComposeTarget,
    DockerSimulatorCommandBackend,
)
from pilot107.api.evidence_query import EvidenceQueryService
from pilot107.api.http_app import Pilot107HttpApi, make_handler
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
        worker_id="smoke-api-evidence-worker",
    )

    run = service.submit(
        RunSubmitRequest(
            owner="alice",
            workdir=Path("/public/home/alice"),
            script="#!/bin/bash\nhostname\necho api-evidence\n",
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
    current = run
    for _ in range(60):
        worker.tick()
        current = store.get_run(run.run_id)
        if (
            current.state in {RunState.SUCCEEDED, RunState.FAILED, RunState.CANCELLED}
            and current.collection_state == CollectionState.SUCCEEDED
        ):
            break
        time.sleep(1)
    if current.collection_state != CollectionState.SUCCEEDED:
        tasks = {
            task["task_type"]: task["state"] for task in store.list_collection_tasks(run.run_id)
        }
        print(
            f"api evidence smoke did not collect: run={current} tasks={tasks}",
            file=sys.stderr,
        )
        return 1

    api = Pilot107HttpApi(
        store=store, evidence_query=EvidenceQueryService(store=store, evidence_store=evidence_store)
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(api))
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        path = f"/api/v1/runs/{run.run_id}/evidence"
        payload = _get_json(f"http://127.0.0.1:{port}{path}")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    paths = _flatten_paths(payload["tree"])
    required = {
        "submission/slurm_submit_response.json",
        "submission/execution_wrapper.generated.sh",
        "slurm/accounting.json",
        "logs/stdout.tail.json",
        "environment/summary.json",
        "outputs/inventory.json",
        "derived/result_summary.v1.json",
        "manifest/manifest.json",
    }
    missing = required - paths
    object_paths = {obj["logical_path"] for obj in payload["objects"]}
    missing_objects = required - object_paths - {"manifest/manifest.json"}
    if payload["collection_state"] != "succeeded" or missing or missing_objects:
        print(
            f"unexpected api evidence result: collection={payload['collection_state']} "
            f"missing={sorted(missing)} missing_objects={sorted(missing_objects)}",
            file=sys.stderr,
        )
        return 1

    print(
        f"api evidence smoke {run.run_id} job={payload['job_id']} "
        f"collection={payload['collection_state']} "
        f"files={len(paths)} objects={len(payload['objects'])}"
    )
    print("url=" + f"http://127.0.0.1:{port}/api/v1/runs/{run.run_id}/evidence")
    return 0


def _get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def _flatten_paths(node: dict) -> set[str]:
    paths: set[str] = set()
    if node["kind"] == "file":
        paths.add(node["logical_path"])
    for child in node.get("children", []):
        paths |= _flatten_paths(child)
    return paths


if __name__ == "__main__":
    raise SystemExit(main())
