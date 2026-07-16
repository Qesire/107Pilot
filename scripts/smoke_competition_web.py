from __future__ import annotations

import json
import os
import ssl
import sys
import time
import urllib.request
from dataclasses import dataclass

BASE_URL = os.environ.get("PILOT107_COMPETITION_BASE_URL", "https://127.0.0.1:8443/api/v1").rstrip("/")
HEADERS = {"Content-Type": "application/json", "X-Pilot107-User": "alice"}
SSL_CONTEXT = ssl._create_unverified_context() if BASE_URL.startswith("https://") else None


@dataclass(frozen=True)
class RunResult:
    run: dict
    evidence: dict
    capsule: dict


def main() -> int:
    try:
        _assert_health()
        success = _run_case(
            name="success",
            command=(
                "hostname\n"
                "echo competition-success\n"
                "mkdir -p pilot107-competition-success\n"
                "echo ok > pilot107-competition-success/result.txt\n"
            ),
            expected_state="SUCCEEDED",
        )
        failure = _run_case(
            name="failure",
            command=(
                "hostname\n"
                "echo competition-failure >&2\n"
                "mkdir -p pilot107-competition-failure\n"
                "echo failed > pilot107-competition-failure/result.txt\n"
                "exit 42\n"
            ),
            expected_state="FAILED",
        )
        cancelled = _run_cancel_case()
    except Exception as exc:
        print(f"competition web smoke failed: {exc}", file=sys.stderr)
        return 1

    print(
        "competition web smoke ok "
        f"success={success.run['run_id']}:{success.run['job_id']} "
        f"failure={failure.run['run_id']}:{failure.run['job_id']} "
        f"cancelled={cancelled.run['run_id']}:{cancelled.run['job_id']} "
        f"capsules={success.capsule['capsule']['capsule_id']},"
        f"{failure.capsule['capsule']['capsule_id']},"
        f"{cancelled.capsule['capsule']['capsule_id']}"
    )
    return 0


def _assert_health() -> None:
    recipes = _get("/recipes")
    items = recipes.get("items")
    if not isinstance(items, list) or not items:
        raise RuntimeError(f"recipes endpoint returned no items: {recipes}")


def _run_case(*, name: str, command: str, expected_state: str) -> RunResult:
    run = _create_submit_and_wait(command=command, expected_state=expected_state)
    evidence = _load_evidence(run)
    capsule = _build_capsule(run)
    _assert_common(
        name=name,
        run=run,
        evidence=evidence,
        capsule=capsule,
        expected_state=expected_state,
    )
    return RunResult(run=run, evidence=evidence, capsule=capsule)


def _run_cancel_case() -> RunResult:
    contract = _post("/contracts", _contract("sleep 60\n"))
    prepared = _post("/runs/prepare", {"contract_id": contract["contract_id"]})
    submitted = _post(f"/runs/{prepared['run_id']}/submit", {})
    run = submitted
    for _ in range(20):
        run = _get(f"/runs/{prepared['run_id']}")
        if run.get("job_id"):
            break
        time.sleep(1)
    _post(f"/runs/{prepared['run_id']}/cancel", {})
    run = _wait_run(prepared["run_id"], expected_state="CANCELLED")
    evidence = _load_evidence(run)
    capsule = _build_capsule(run)
    _assert_common(
        name="cancelled",
        run=run,
        evidence=evidence,
        capsule=capsule,
        expected_state="CANCELLED",
    )
    return RunResult(run=run, evidence=evidence, capsule=capsule)


def _create_submit_and_wait(*, command: str, expected_state: str) -> dict:
    contract = _post("/contracts", _contract(command))
    prepared = _post("/runs/prepare", {"contract_id": contract["contract_id"]})
    _post(f"/runs/{prepared['run_id']}/submit", {})
    return _wait_run(prepared["run_id"], expected_state=expected_state)


def _wait_run(run_id: str, *, expected_state: str) -> dict:
    last = {}
    for _ in range(240):
        last = _get(f"/runs/{run_id}")
        if last.get("state") == expected_state and last.get("collection_state") == "succeeded":
            return last
        time.sleep(1)
    raise RuntimeError(f"run {run_id} did not reach {expected_state}/succeeded: {last}")


def _load_evidence(run: dict) -> dict:
    evidence = _get(f"/runs/{run['run_id']}/evidence")
    paths = {item.get("logical_path") for item in evidence.get("objects", [])}
    required = {
        "submission/slurm_submit_response.json",
        "slurm/accounting.json",
        "logs/stdout.tail.json",
        "logs/stderr.tail.json",
        "environment/summary.json",
        "outputs/inventory.json",
        "derived/result_summary.v1.json",
    }
    missing = required - paths
    if missing:
        raise RuntimeError(f"evidence missing for {run['run_id']}: {sorted(missing)}")
    return evidence


def _build_capsule(run: dict) -> dict:
    capsule = _post(f"/runs/{run['run_id']}/capsule", {})
    if capsule.get("capsule_state") != "ready":
        raise RuntimeError(f"capsule not ready for {run['run_id']}: {capsule}")
    if not capsule.get("capsule", {}).get("manifest_sha256"):
        raise RuntimeError(f"capsule manifest missing for {run['run_id']}: {capsule}")
    return capsule


def _assert_common(
    *,
    name: str,
    run: dict,
    evidence: dict,
    capsule: dict,
    expected_state: str,
) -> None:
    if run.get("state") != expected_state:
        raise RuntimeError(f"{name} wrong state: {run}")
    if run.get("submit_strategy") != "command":
        raise RuntimeError(f"{name} did not use Docker Slurm command backend: {run}")
    if run.get("job_id", "").startswith("demo-"):
        raise RuntimeError(f"{name} used demo backend: {run}")
    if evidence.get("collection_state") != "succeeded":
        raise RuntimeError(f"{name} evidence not succeeded: {evidence}")
    if capsule.get("capsule_state") != "ready":
        raise RuntimeError(f"{name} capsule not ready: {capsule}")


def _contract(command: str) -> dict:
    return {
        "recipe_version_id": "recipe_python_cpu@1.0.0",
        "project": {"workdir": "/public/home/alice"},
        "entry": {
            "command": command,
            "expected_outputs": ["pilot107-competition-success/result.txt"],
        },
        "resources": {
            "partition": "Students",
            "qos": "qos_stu_medium_2gpu",
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
