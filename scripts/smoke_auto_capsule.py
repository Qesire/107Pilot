"""Auto-Capsule end-to-end gap smoke.

Proves the Worker auto-generates a Capsule after Evidence SUCCEEDED WITHOUT the
client calling explicit ``POST /runs/{id}/capsule``. Reuses the HTTP contract /
prepare / submit / poll helpers and conventions of ``smoke_competition_web.py``
(``urllib``, ``X-Pilot107-User: alice`` header, unverified TLS context, single
success run).

Flow:
  1. Submit a single success contract via HTTP.
  2. Poll ``GET /runs/{id}`` until ``state == "SUCCEEDED"`` and
     ``collection_state == "succeeded"`` (Evidence collected by the worker).
  3. DO NOT POST /runs/{id}/capsule. Instead poll ``GET /runs/{id}`` watching
     ``capsule_state`` until it becomes ``"ready"`` (timeout ~60s).
  4. ``GET /runs/{id}/capsule`` and assert ``capsule.manifest_sha256`` present.

Exits 0 on success, 1 on timeout / missing auto-capsule.
Assumes ``PILOT107_AUTO_CAPSULE`` is default-on (no env needed).
"""

from __future__ import annotations

import json
import os
import ssl
import sys
import time
import urllib.request

BASE_URL = os.environ.get(
    "PILOT107_COMPETITION_BASE_URL",
    os.environ.get("PILOT107_PUBLIC_URL", "https://127.0.0.1:8443").rstrip("/") + "/api/v1",
).rstrip("/")
HEADERS = {"Content-Type": "application/json", "X-Pilot107-User": "alice"}
SSL_CONTEXT = ssl._create_unverified_context() if BASE_URL.startswith("https://") else None


def main() -> int:
    try:
        command = (
            "hostname\n"
            "echo auto-capsule-ok\n"
            "mkdir -p pilot107-auto-capsule\n"
            "echo ok > pilot107-auto-capsule/result.txt\n"
        )
        run = _create_submit_and_wait(command=command, expected_state="SUCCEEDED")
        run_id = run["run_id"]
        # The worker auto-builds the capsule after collection succeeds; poll for
        # capsule_state == "ready" WITHOUT issuing an explicit capsule POST.
        capsule_state = run.get("capsule_state")
        deadline = time.time() + 60
        while capsule_state != "ready" and time.time() < deadline:
            time.sleep(1)
            run = _get(f"/runs/{run_id}")
            capsule_state = run.get("capsule_state")
        if capsule_state != "ready":
            print(
                f"auto-capsule smoke failed: capsule_state={capsule_state!r} "
                f"never reached ready for run={run_id}",
                file=sys.stderr,
            )
            return 1
        capsule = _get(f"/runs/{run_id}/capsule")
        manifest_sha = (capsule.get("capsule") or {}).get("manifest_sha256")
        if not manifest_sha:
            print(
                f"auto-capsule smoke failed: capsule missing manifest_sha256 for run={run_id}: "
                f"{capsule}",
                file=sys.stderr,
            )
            return 1
        print(f"auto-capsule smoke ok run={run_id} capsule={manifest_sha}")
        return 0
    except Exception as exc:  # noqa: BLE001 - smoke reports failures as exit 1
        print(f"auto-capsule smoke failed: {exc}", file=sys.stderr)
        return 1


def _create_submit_and_wait(*, command: str, expected_state: str) -> dict:
    contract = _post("/contracts", _contract(command))
    prepared = _post("/runs/prepare", {"contract_id": contract["contract_id"]})
    _post(f"/runs/{prepared['run_id']}/submit", {})
    return _wait_run(prepared["run_id"], expected_state=expected_state)


def _wait_run(run_id: str, *, expected_state: str) -> dict:
    last: dict = {}
    for _ in range(240):
        last = _get(f"/runs/{run_id}")
        if last.get("state") == expected_state and last.get("collection_state") == "succeeded":
            return last
        time.sleep(1)
    raise RuntimeError(f"run {run_id} did not reach {expected_state}/succeeded: {last}")


def _contract(command: str) -> dict:
    return {
        "recipe_version_id": "recipe_python_cpu@1.0.0",
        "project": {"workdir": "/public/home/alice"},
        "entry": {
            "command": command,
            "expected_outputs": ["pilot107-auto-capsule/result.txt"],
        },
        "resources": {
            "partition": os.environ.get("PILOT107_SMOKE_PARTITION", "CPU-RC"),
            "qos": os.environ.get("PILOT107_SMOKE_QOS", "qos_cpu_rc"),
            "nodes": 1,
            "ntasks": 1,
            "cpus_per_task": 1,
            "time_limit": "00:05:00",
        },
    }


def _get(path: str) -> dict:
    request = urllib.request.Request(url=f"{BASE_URL}{path}", headers={"X-Pilot107-User": "alice"})
    with urllib.request.urlopen(request, timeout=30, context=SSL_CONTEXT) as response:
        return json.loads(response.read().decode("utf-8"))


def _post(path: str, payload: dict) -> dict:
    request = urllib.request.Request(
        url=f"{BASE_URL}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers=HEADERS,
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30, context=SSL_CONTEXT) as response:
        return json.loads(response.read().decode("utf-8"))


if __name__ == "__main__":
    raise SystemExit(main())
