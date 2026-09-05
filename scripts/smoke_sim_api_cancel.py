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
from pilot107.core.states import RunState
from pilot107.worker.evidence import EvidenceStore


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
    service = RunService(store=store, backend=backend)
    run = service.submit(
        RunSubmitRequest(
            owner="alice",
            workdir=Path("/public/home/alice"),
            script=(
                "#!/bin/bash\nhostname\necho api-cancel-started\nsleep 60\necho should-not-print\n"
            ),
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
    time.sleep(1)

    api = Pilot107HttpApi(
        store=store,
        evidence_query=EvidenceQueryService(store=store, evidence_store=evidence_store),
        run_service=service,
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(api))
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        payload = _post_json(f"http://127.0.0.1:{port}/api/v1/runs/{run.run_id}/cancel")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    stored = store.get_run(run.run_id)
    if (
        payload["run_id"] != run.run_id
        or payload["state"] != "CANCELLED"
        or stored.state != RunState.CANCELLED
    ):
        print(f"unexpected api cancel result: payload={payload} stored={stored}", file=sys.stderr)
        return 1

    print(f"api cancel smoke {payload['run_id']} job={payload['job_id']} state={payload['state']}")
    print("url=" + f"http://127.0.0.1:{port}/api/v1/runs/{run.run_id}/cancel")
    return 0


def _post_json(url: str) -> dict:
    request = urllib.request.Request(
        url=url,
        data=b"{}",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


if __name__ == "__main__":
    raise SystemExit(main())
