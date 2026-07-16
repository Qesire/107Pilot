from __future__ import annotations

import json
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from pilot107.adapters.slurm import InMemorySlurmBackend
from pilot107.api.evidence_query import EvidenceQueryService
from pilot107.api.http_app import Pilot107HttpApi
from pilot107.api.http_app import make_handler as make_api_handler
from pilot107.core.contracts import ContractService, ContractStore, RecipeCatalog
from pilot107.core.run_service import RunService
from pilot107.core.run_store import RunStore
from pilot107.web.server import WebConfig
from pilot107.web.server import make_handler as make_web_handler
from pilot107.worker.evidence import EvidenceStore


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    runtime_dir = root / "data" / "phase0"
    db_path = runtime_dir / "web-smoke.db"
    evidence_root = runtime_dir / "web-smoke-evidence"
    store = RunStore(db_path)
    catalog = RecipeCatalog()
    api = Pilot107HttpApi(
        store=store,
        evidence_query=EvidenceQueryService(
            store=store, evidence_store=EvidenceStore(evidence_root)
        ),
        run_service=RunService(store=store, backend=InMemorySlurmBackend()),
        recipe_catalog=catalog,
        contract_service=ContractService(catalog=catalog, store=ContractStore(db_path)),
        auth_required=True,
    )

    with (
        _served(make_api_handler(api)) as api_url,
        _served(
            make_web_handler(WebConfig(api_base_url=api_url, demo_user="alice"))
        ) as web_url,
    ):
        index = urllib.request.urlopen(f"{web_url}/", timeout=10).read().decode("utf-8")
        recipes = _get(f"{web_url}/api/v1/recipes")
        contract = _post(f"{web_url}/api/v1/contracts", _contract())
        prepared = _post(
            f"{web_url}/api/v1/runs/prepare", {"contract_id": contract["contract_id"]}
        )
        submitted = _post(f"{web_url}/api/v1/runs/{prepared['run_id']}/submit", {})

    assert "107Pilot" in index, index[:120]
    assert recipes["items"][0]["recipe_id"] == "recipe_python_cpu", recipes
    assert prepared["state"] == "VALIDATED", prepared
    assert submitted["submit_strategy"] == "in_memory", submitted
    print(
        "web mvp smoke "
        f"contract={contract['contract_id']} run={submitted['run_id']} job={submitted['job_id']}"
    )
    return 0


def _contract() -> dict:
    return {
        "recipe_version_id": "recipe_python_cpu@1.0.0",
        "project": {"workdir": "/public/home/alice"},
        "entry": {"command": "echo web-smoke-ok"},
        "resources": {
            "partition": "Students",
            "qos": "qos_stu_medium_2gpu",
            "nodes": 1,
            "ntasks": 1,
            "cpus_per_task": 1,
            "time_limit": "00:05:00",
        },
    }


def _get(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def _post(url: str, payload: dict) -> dict:
    request = urllib.request.Request(
        url=url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


class _served:
    def __init__(self, handler) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> str:
        self.thread.start()
        time.sleep(0.05)
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def __exit__(self, exc_type, exc, tb) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())
