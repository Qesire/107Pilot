from __future__ import annotations

import json
import os
import ssl
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_DIR = ROOT / "simulator" / "compose"
ENV_FILE = Path(
    os.environ.get(
        "PILOT107_COMPETITION_ENV_FILE",
        str(COMPOSE_DIR / ".env.competition"),
    )
)
if not ENV_FILE.exists():
    ENV_FILE = COMPOSE_DIR / ".env.competition.example"

BASE_URL = os.environ.get("PILOT107_COMPETITION_BASE_URL", "https://127.0.0.1:8443/api/v1").rstrip("/")
HEADERS = {"Content-Type": "application/json", "X-Pilot107-User": "alice"}
SSL_CONTEXT = ssl._create_unverified_context() if BASE_URL.startswith("https://") else None
EXPECTED_TRANSPORT = os.environ.get(
    "PILOT107_EXPECT_EVIDENCE_TRANSPORT",
    "command_fallback",
).strip()


def main() -> int:
    try:
        run = _create_submit_and_wait()
        evidence = _get(f"/runs/{run['run_id']}/evidence")
        _assert_required_objects(run["run_id"], evidence)
        payloads = _read_evidence_payloads(run["run_id"])
        observed = _assert_transport_payloads(payloads)
    except Exception as exc:
        print(f"competition evidence transport smoke failed: {exc}", file=sys.stderr)
        return 1

    print(
        "competition evidence transport smoke ok "
        f"run={run['run_id']} job={run['job_id']} "
        f"expected={EXPECTED_TRANSPORT} "
        f"logs={observed['stdout']}/{observed['stderr']} "
        f"outputs={observed['outputs']}"
    )
    return 0


def _create_submit_and_wait() -> dict[str, Any]:
    contract = _post(
        "/contracts",
        {
            "recipe_version_id": "recipe_python_cpu@1.0.0",
            "project": {"workdir": "/public/home/alice"},
            "entry": {
                "command": (
                    "hostname\n"
                    "echo transport-stdout\n"
                    "echo transport-stderr >&2\n"
                    "mkdir -p pilot107-transport-smoke\n"
                    "echo transport-ok > pilot107-transport-smoke/result.txt\n"
                ),
                "expected_outputs": ["pilot107-transport-smoke/result.txt"],
            },
            "resources": {
                "partition": "Students",
                "qos": "qos_stu_medium_2gpu",
                "nodes": 1,
                "ntasks": 1,
                "cpus_per_task": 1,
                "time_limit": "00:05:00",
            },
        },
    )
    prepared = _post("/runs/prepare", {"contract_id": contract["contract_id"]})
    _post(f"/runs/{prepared['run_id']}/submit", {})
    for _ in range(240):
        run = _get(f"/runs/{prepared['run_id']}")
        if run.get("state") == "SUCCEEDED" and run.get("collection_state") == "succeeded":
            return run
        time.sleep(1)
    raise RuntimeError(f"run did not reach SUCCEEDED/succeeded: {run}")


def _assert_required_objects(run_id: str, evidence: dict[str, Any]) -> None:
    paths = {item.get("logical_path") for item in evidence.get("objects", [])}
    required = {
        "logs/stdout.tail.json",
        "logs/stderr.tail.json",
        "outputs/inventory.json",
    }
    missing = required - paths
    if missing:
        raise RuntimeError(f"evidence objects missing for {run_id}: {sorted(missing)}")


def _read_evidence_payloads(run_id: str) -> dict[str, dict[str, Any]]:
    code = r"""
import json
from pathlib import Path
run_id = __import__("os").environ["PILOT107_SMOKE_RUN_ID"]
root = Path("/var/lib/pilot107/evidence") / "runs" / run_id
payload = {
    "stdout": json.loads((root / "logs" / "stdout.tail.json").read_text(encoding="utf-8")),
    "stderr": json.loads((root / "logs" / "stderr.tail.json").read_text(encoding="utf-8")),
    "outputs": json.loads((root / "outputs" / "inventory.json").read_text(encoding="utf-8")),
}
print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
"""
    completed = subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            str(ENV_FILE),
            "-f",
            str(COMPOSE_DIR / "compose.yml"),
            "-f",
            str(COMPOSE_DIR / "compose.competition.yml"),
            "--profile",
            "competition",
            "exec",
            "-T",
            "-e",
            f"PILOT107_SMOKE_RUN_ID={run_id}",
            "pilot107-api",
            "python3",
            "-c",
            code,
        ],
        check=False,
        text=True,
        capture_output=True,
        env={**os.environ, "PILOT107_SMOKE_RUN_ID": run_id},
    )
    if completed.returncode != 0:
        raise RuntimeError(
            "failed to read evidence payloads from pilot107-api: "
            f"stdout={completed.stdout!r} stderr={completed.stderr!r}"
        )
    decoded = json.loads(completed.stdout)
    if not isinstance(decoded, dict):
        raise RuntimeError(f"unexpected evidence payload container: {decoded!r}")
    return decoded


def _assert_transport_payloads(payloads: dict[str, dict[str, Any]]) -> dict[str, str]:
    observed = {
        key: str(payloads[key].get("transport") or "command_fallback")
        for key in ("stdout", "stderr", "outputs")
    }
    for key in ("stdout", "stderr", "outputs"):
        if observed[key] != EXPECTED_TRANSPORT:
            raise RuntimeError(
                f"{key} transport mismatch: expected={EXPECTED_TRANSPORT!r} "
                f"observed={observed[key]!r}"
            )
    if "transport-stdout" not in str(payloads["stdout"].get("tail")):
        raise RuntimeError("stdout tail did not contain submitted output")
    output_paths = {item.get("relative_path") for item in payloads["outputs"].get("files", [])}
    if "pilot107-transport-smoke/result.txt" not in output_paths:
        raise RuntimeError(f"outputs inventory missing generated result: {sorted(output_paths)}")
    return observed


def _get(path: str) -> dict[str, Any]:
    request = urllib.request.Request(f"{BASE_URL}{path}", headers=HEADERS, method="GET")
    with urllib.request.urlopen(request, context=SSL_CONTEXT, timeout=20) as response:
        return _json_object(response.read().decode("utf-8"))


def _post(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{BASE_URL}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers=HEADERS,
        method="POST",
    )
    with urllib.request.urlopen(request, context=SSL_CONTEXT, timeout=20) as response:
        return _json_object(response.read().decode("utf-8"))


def _json_object(text: str) -> dict[str, Any]:
    decoded = json.loads(text)
    if not isinstance(decoded, dict):
        raise RuntimeError(f"expected JSON object, got {type(decoded).__name__}")
    return decoded


if __name__ == "__main__":
    raise SystemExit(main())
