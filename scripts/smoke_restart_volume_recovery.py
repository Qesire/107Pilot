"""Restart + volume recovery gap smoke for the cpu-rc stack.

Proves ``docker compose down`` mid-stack + restart preserves volume-persisted
run state and the stack still serves new runs.

Flow:
  1. Submit a SUCCEEDED run via HTTP; record run_id; verify
     state=SUCCEEDED, collection_state=succeeded, capsule_state=ready.
  2. ``docker compose -p pilot107-cpu-rc -f ... down`` (same flags as
     scripts/stop-cpu-rc.sh). Assert 0 containers running.
  3. ``bash scripts/start-cpu-rc.sh`` (honors PILOT107_PUBLIC_URL). The script
     waits for service health before returning.
  4. ``GET /runs/{run_id}`` — assert the pre-restart run STILL has
     state=SUCCEEDED, collection_state=succeeded, capsule_state=ready
     (volume-persisted).
  5. Submit a NEW run via HTTP, wait SUCCEEDED.
  6. Print ``restart-volume-recovery ok pre_restart=<id> post_restart=<id>``.

Exits 0 on success, 1 on any mismatch.
"""

from __future__ import annotations

import json
import os
import ssl
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_DIR = ROOT / "simulator" / "compose"
ENV_FILE = Path(os.environ.get("PILOT107_CPU_RC_ENV_FILE", str(COMPOSE_DIR / ".env.cpu-rc")))
PROJECT_NAME = os.environ.get("PILOT107_CPU_RC_PROJECT_NAME", "pilot107-cpu-rc")

BASE_URL = os.environ.get(
    "PILOT107_COMPETITION_BASE_URL",
    os.environ.get("PILOT107_PUBLIC_URL", "https://127.0.0.1:8443").rstrip("/") + "/api/v1",
).rstrip("/")
HEADERS = {"Content-Type": "application/json", "X-Pilot107-User": "alice"}
SSL_CONTEXT = ssl._create_unverified_context() if BASE_URL.startswith("https://") else None


def main() -> int:
    try:
        # 1. Submit a success run and wait for SUCCEEDED + capsule ready.
        command = (
            "hostname\n"
            "echo restart-recovery-pre\n"
            "mkdir -p pilot107-restart-recovery-pre\n"
            "echo ok > pilot107-restart-recovery-pre/result.txt\n"
        )
        pre = _create_submit_and_wait(command=command, expected_state="SUCCEEDED")
        pre_run_id = pre["run_id"]
        pre = _wait_capsule_ready(pre_run_id)
        _assert_state(pre, "pre-restart")

        # 2. docker compose down (same flags as stop-cpu-rc.sh).
        compose_down = subprocess.run(
            [
                "docker", "compose",
                "--project-name", PROJECT_NAME,
                "--env-file", str(ENV_FILE),
                "-f", str(COMPOSE_DIR / "compose.yml"),
                "-f", str(COMPOSE_DIR / "compose.competition.yml"),
                "-f", str(COMPOSE_DIR / "compose.cpu-rc.yml"),
                "--profile", "competition",
                "down",
            ],
            cwd=str(ROOT),
        )
        if compose_down.returncode != 0:
            print(
                f"restart-recovery smoke failed: docker compose down rc={compose_down.returncode}",
                file=sys.stderr,
            )
            return 1
        # Assert 0 containers running for the project.
        ps = subprocess.run(
            [
                "docker", "compose",
                "--project-name", PROJECT_NAME,
                "--env-file", str(ENV_FILE),
                "-f", str(COMPOSE_DIR / "compose.yml"),
                "-f", str(COMPOSE_DIR / "compose.competition.yml"),
                "-f", str(COMPOSE_DIR / "compose.cpu-rc.yml"),
                "--profile", "competition",
                "ps", "-q",
            ],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
        )
        if ps.stdout.strip():
            print(
                f"restart-recovery smoke failed: containers still running after down: "
                f"{ps.stdout.strip()}",
                file=sys.stderr,
            )
            return 1

        # 3. Restart the stack (honors PILOT107_PUBLIC_URL; waits for health).
        start = subprocess.run(
            ["bash", str(ROOT / "scripts" / "start-cpu-rc.sh")],
            cwd=str(ROOT),
        )
        if start.returncode != 0:
            print(
                f"restart-recovery smoke failed: start-cpu-rc.sh rc={start.returncode}",
                file=sys.stderr,
            )
            return 1

        # 4. Pre-restart run still persisted.
        persisted = _wait_capsule_ready(pre_run_id)
        _assert_state(persisted, "pre-restart (post-restart read)")

        # 5. Submit a new run post-restart.
        new_command = (
            "hostname\n"
            "echo restart-recovery-post\n"
            "mkdir -p pilot107-restart-recovery-post\n"
            "echo ok > pilot107-restart-recovery-post/result.txt\n"
        )
        post = _create_submit_and_wait(command=new_command, expected_state="SUCCEEDED")
        post_run_id = post["run_id"]
        post = _wait_capsule_ready(post_run_id)
        _assert_state(post, "post-restart")

        print(f"restart-volume-recovery ok pre_restart={pre_run_id} post_restart={post_run_id}")
        return 0
    except Exception as exc:  # noqa: BLE001 - smoke reports failures as exit 1
        print(f"restart-volume-recovery smoke failed: {exc}", file=sys.stderr)
        return 1


def _assert_state(run: dict, label: str) -> None:
    if run.get("state") != "SUCCEEDED":
        raise RuntimeError(f"{label}: state={run.get('state')!r} != SUCCEEDED: {run}")
    if run.get("collection_state") != "succeeded":
        raise RuntimeError(f"{label}: collection_state={run.get('collection_state')!r}")
    if run.get("capsule_state") != "ready":
        raise RuntimeError(f"{label}: capsule_state={run.get('capsule_state')!r}")


def _wait_capsule_ready(run_id: str) -> dict:
    last: dict = {}
    deadline = time.time() + 120
    while time.time() < deadline:
        last = _get(f"/runs/{run_id}")
        if (
            last.get("state") == "SUCCEEDED"
            and last.get("collection_state") == "succeeded"
            and last.get("capsule_state") == "ready"
        ):
            return last
        time.sleep(1)
    raise RuntimeError(f"run {run_id} did not reach SUCCEEDED/succeeded/ready: {last}")


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
            "expected_outputs": ["pilot107-restart-recovery-pre/result.txt"],
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
    request = urllib.request.Request(
        url=f"{BASE_URL}{path}", headers={"X-Pilot107-User": "alice"}
    )
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
