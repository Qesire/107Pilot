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
from pilot107.core.run_service import RunService
from pilot107.core.run_store import RunStore
from pilot107.core.states import CollectionState, RunState
from pilot107.worker.evidence import DockerSlurmEvidenceCollector, EvidenceStore
from pilot107.worker.runtime_worker import RuntimeReconcileWorker

AUTH_HEADERS = {"X-Pilot107-User": "alice"}


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
        worker_id="smoke-api-submit-worker",
    )
    api = Pilot107HttpApi(
        store=store,
        evidence_query=EvidenceQueryService(store=store, evidence_store=evidence_store),
        run_service=service,
        auth_required=True,
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(api))
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base_url = f"http://127.0.0.1:{port}/api/v1"
        prepared = _post_json(f"{base_url}/runs/prepare", _submit_payload())
        if prepared["state"] != "VALIDATED" or not prepared["script_artifacts"]["wrapper_sha256"]:
            print(f"unexpected prepare response: {prepared}", file=sys.stderr)
            return 1
        submitted = _post_json(f"{base_url}/runs/{prepared['run_id']}/submit", {})
        if submitted["state"] != "SUBMITTED" or not submitted["job_id"]:
            print(f"unexpected submit response: {submitted}", file=sys.stderr)
            return 1

        final_run = store.get_run(prepared["run_id"])
        task_errors: list[str] = []
        for _ in range(40):
            result = worker.tick()
            task_errors = [error.message for error in result.task_errors]
            final_run = store.get_run(prepared["run_id"])
            if (
                final_run.state in {RunState.SUCCEEDED, RunState.FAILED, RunState.CANCELLED}
                and final_run.collection_state == CollectionState.SUCCEEDED
            ):
                break
            time.sleep(1)
        run_payload = _get_json(f"{base_url}/runs/{prepared['run_id']}")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    if (
        final_run.state != RunState.SUCCEEDED
        or final_run.exit_code != "0:0"
        or final_run.collection_state != CollectionState.SUCCEEDED
        or run_payload["state"] != "SUCCEEDED"
    ):
        print(
            f"unexpected api submit final result: run={final_run}; "
            f"run_payload={run_payload}; task_errors={task_errors}",
            file=sys.stderr,
        )
        return 1

    print(
        f"api submit smoke {final_run.run_id} job={final_run.job_id} "
        f"state={final_run.state} collection={final_run.collection_state}"
    )
    print("url=" + f"http://127.0.0.1:{port}/api/v1/runs/{final_run.run_id}")
    return 0


def _submit_payload() -> dict:
    return {
        "owner": "alice",
        "workdir": "/public/home/alice",
        "script": (
            "#!/bin/bash\n"
            "hostname\n"
            "echo api-submit\n"
            "mkdir -p pilot107-api-submit-output\n"
            "echo ok > pilot107-api-submit-output/result.txt\n"
        ),
        "resource_plan": {
            "partition": "Students",
            "qos": "qos_stu_medium_2gpu",
            "nodes": 1,
            "ntasks": 1,
            "cpus_per_task": 1,
            "time_limit": "00:05:00",
        },
    }


def _post_json(url: str, payload: dict) -> dict:
    request = urllib.request.Request(
        url=url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **AUTH_HEADERS},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def _get_json(url: str) -> dict:
    request = urllib.request.Request(url=url, headers=AUTH_HEADERS, method="GET")
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


if __name__ == "__main__":
    raise SystemExit(main())
