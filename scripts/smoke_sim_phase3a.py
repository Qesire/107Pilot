from __future__ import annotations

import json
import tempfile
import threading
import time
import urllib.error
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
from pilot107.core.contracts import ContractService, ContractStore, RecipeCatalog
from pilot107.core.run_service import RunService
from pilot107.core.run_store import RunStore
from pilot107.core.states import RunState
from pilot107.worker.evidence import EvidenceStore


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    compose_dir = root / "simulator" / "compose"
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
    stamp = str(time.time_ns())
    workdir = f"/public/home/alice/pilot107-phase3a-{stamp}"
    kit_root = f"{workdir}/kit"
    setup = executor.run(
        ["mkdir", "-p", workdir, kit_root, f"{workdir}/logs"],
        user="alice",
        timeout_seconds=10.0,
    )
    if setup.returncode != 0:
        raise RuntimeError(f"failed to create Phase 3A workdir: {setup.stderr}")

    with tempfile.TemporaryDirectory(prefix="pilot107-phase3a-") as temp_dir:
        db_path = Path(temp_dir) / "pilot107.db"
        store = RunStore(db_path)
        contract_store = ContractStore(db_path)
        contract_service = ContractService(
            catalog=RecipeCatalog(store=contract_store),
            store=contract_store,
        )
        service = RunService(store=store, backend=backend)
        contract = contract_service.create(
            owner="alice",
            payload=_contract_payload(workdir=workdir, kit_root=kit_root),
        )
        run = service.submit(contract_service.to_submit_request(contract))
        run = _wait_for_terminal(service, run.run_id)
        if run.state != RunState.SUCCEEDED or run.exit_code != "0:0":
            raise RuntimeError(f"real Phase 3A run failed: {run.state} {run.exit_code}")
        child = store.create_run(
            run_id=f"run_phase3a_child_{stamp}",
            contract_id=contract.contract_id,
            owner="alice",
            workdir=workdir,
            script="echo child",
            parent_run_id=run.run_id,
            lineage_reason="manual_retry",
            workflow={
                "dependencies": [run.run_id],
                "retry": {"max_attempts": 1, "backoff_seconds": 0},
                "automation": {"level": "explain", "require_approval": True},
            },
        )
        advice, _ = store.create_agent_advice(
            advice_id=f"advice_phase3a_{stamp}",
            run_id=run.run_id,
            owner="alice",
            request_key="phase3a-live",
            state="ready",
            source_run_updated_at=run.updated_at,
            evidence_bundle_sha256="phase3a-live-evidence",
            provider="none",
            model=None,
            payload={"summary": "Phase 3A pending approval", "actions": []},
        )
        evidence_store = EvidenceStore(Path(temp_dir) / "evidence")
        api = Pilot107HttpApi(
            store=store,
            evidence_query=EvidenceQueryService(
                store=store,
                evidence_store=evidence_store,
            ),
            run_service=service,
            contract_service=contract_service,
            auth_required=True,
        )
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(api))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            _verify_http_read_models(
                base_url=base_url,
                run_id=run.run_id,
                child_run_id=child.run_id,
                contract_id=contract.contract_id,
                advice_id=advice.advice_id,
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    print(
        f"phase3a live read-model smoke passed run={run.run_id} job={run.job_id} workdir={workdir}"
    )
    return 0


def _verify_http_read_models(
    *,
    base_url: str,
    run_id: str,
    child_run_id: str,
    contract_id: str,
    advice_id: str,
) -> None:
    run_list_url = f"{base_url}/api/v1/runs?state=SUCCEEDED&limit=1"
    runs, run_headers = _get_json(run_list_url)
    contracts, _ = _get_json(f"{base_url}/api/v1/contracts?limit=1")
    events, _ = _get_json(f"{base_url}/api/v1/runs/{run_id}/events?limit=100")
    lineage, _ = _get_json(f"{base_url}/api/v1/runs/{child_run_id}/lineage")
    pending, _ = _get_json(f"{base_url}/api/v1/agent/advice?pending=true")
    if runs["items"][0]["job_id"] is None:
        raise RuntimeError("real Slurm job is missing from Run list")
    if contracts["items"][0]["contract_id"] != contract_id:
        raise RuntimeError("Contract list did not return the submitted Contract")
    if not any(item["event_type"] == "run.snapshot" for item in events["items"]):
        raise RuntimeError("event read model omitted real Slurm snapshots")
    edge_types = {item["type"] for item in lineage["edges"]}
    if not {"lineage", "workflow_dependency"}.issubset(edge_types):
        raise RuntimeError(f"lineage graph is incomplete: {lineage['edges']!r}")
    if [item["advice_id"] for item in pending["items"]] != [advice_id]:
        raise RuntimeError("pending Advice queue is incomplete")

    etag = run_headers.get("ETag")
    if etag is None:
        raise RuntimeError("Run list response did not include ETag")
    status = _get_status(
        run_list_url,
        extra_headers={"If-None-Match": etag},
    )
    if status != 304:
        raise RuntimeError(f"If-None-Match did not return 304: {status}")

    request = urllib.request.Request(
        f"{base_url}/api/v1/runs/{run_id}/events/stream?once=true&type=run.snapshot",
        headers={"X-Pilot107-User": "alice"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        body = response.read().decode()
    if "event: run_event" not in body or '"event_type":"run.snapshot"' not in body:
        raise RuntimeError("SSE replay did not contain a real snapshot summary")
    if "raw_response" in body:
        raise RuntimeError("SSE replay leaked a raw event payload")


def _get_json(url: str) -> tuple[dict, dict[str, str]]:
    request = urllib.request.Request(url, headers={"X-Pilot107-User": "alice"})
    with urllib.request.urlopen(request, timeout=10) as response:
        payload = json.loads(response.read().decode())
        return payload, dict(response.headers.items())


def _get_status(url: str, *, extra_headers: dict[str, str]) -> int:
    request = urllib.request.Request(
        url,
        headers={"X-Pilot107-User": "alice", **extra_headers},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code


def _wait_for_terminal(service: RunService, run_id: str):
    current = service.get(run_id)
    for _ in range(60):
        current = service.reconcile_once(run_id)
        if current.state in {RunState.SUCCEEDED, RunState.FAILED, RunState.CANCELLED}:
            return current
        time.sleep(1)
    raise RuntimeError(f"run did not reach terminal state: {run_id}")


def _contract_payload(*, workdir: str, kit_root: str) -> dict:
    return {
        "recipe_version_id": "recipe_student_cpu_basic@1.0.0",
        "project": {"name": "phase3a-live", "workdir": workdir},
        "entry": {"command": "printf 'phase3a-live\\n'"},
        "runtime": {"environment": {"KIT_ROOT": kit_root}},
        "resources": {
            "partition": "Students",
            "qos": "qos_stu_cpu_long",
            "nodes": 1,
            "ntasks": 1,
            "cpus_per_task": 1,
            "time_limit": "00:05:00",
        },
    }


if __name__ == "__main__":
    raise SystemExit(main())
