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

import hashlib
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
        pre = _create_submit_and_wait(
            command=command,
            expected_state="SUCCEEDED",
            expected_outputs=["pilot107-restart-recovery-pre/result.txt"],
        )
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
        post = _create_submit_and_wait(
            command=new_command,
            expected_state="SUCCEEDED",
            expected_outputs=["pilot107-restart-recovery-post/result.txt"],
        )
        post_run_id = post["run_id"]
        post = _wait_capsule_ready(post_run_id)
        _assert_state(post, "post-restart")

        # 6. Assert output evidence attribution for the post-restart run.
        _assert_post_restart_inventory(post_run_id)

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


def _create_submit_and_wait(
    *,
    command: str,
    expected_state: str,
    expected_outputs: list[str],
) -> dict:
    contract = _post("/contracts", _contract(command, expected_outputs=expected_outputs))
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


def _contract(command: str, *, expected_outputs: list[str]) -> dict:
    return {
        "recipe_version_id": "recipe_python_cpu@1.0.0",
        "project": {"workdir": "/public/home/alice"},
        "entry": {
            "command": command,
            "expected_outputs": expected_outputs,
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


def _assert_post_restart_inventory(post_run_id: str) -> None:
    """Verify the post-restart run's outputs/inventory.json attributes files correctly.

    The post-restart run shares the workdir with the pre-restart run, so the
    pre-restart result.txt must appear as ``preexisting`` (mtime < post-run
    started_at) while the post-restart result.txt must be ``created_by_run`` and
    flagged ``in_expected_outputs``.
    """
    tree = _get(f"/runs/{post_run_id}/evidence")
    object_id = next(
        (
            obj["object_id"]
            for obj in tree.get("objects", [])
            if obj.get("logical_path") == "outputs/inventory.json"
        ),
        None,
    )
    if object_id is None:
        raise RuntimeError(
            f"post-restart run {post_run_id}: outputs/inventory.json not found in evidence tree"
        )
    preview = _get(f"/runs/{post_run_id}/evidence/objects/{object_id}")
    payload = preview.get("preview", {})
    if not payload.get("available"):
        raise RuntimeError(
            f"post-restart run {post_run_id}: inventory preview unavailable: {payload!r}"
        )
    inventory = json.loads(payload["content"])
    files_by_path = {item["relative_path"]: item for item in inventory.get("files", [])}

    post_rel = "pilot107-restart-recovery-post/result.txt"
    pre_rel = "pilot107-restart-recovery-pre/result.txt"

    post_entry = files_by_path.get(post_rel)
    if post_entry is None:
        raise RuntimeError(
            f"post-restart inventory missing {post_rel}: {list(files_by_path)}"
        )
    # Round 4 changed compute_file_attribution so expected outputs with a
    # captured baseline classify as "created"/"modified"/"unchanged"/"missing"
    # (strict baseline-vs-final) instead of the mtime-based "created_by_run".
    # The post-restart run genuinely produces its expected output, so accept
    # either "created" (baseline-aware, current) or "created_by_run" (legacy
    # mtime fallback when no baseline was captured).
    post_attribution = post_entry.get("attribution")
    if post_attribution not in {"created", "modified", "created_by_run"}:
        raise RuntimeError(
            f"post-restart {post_rel}: attribution={post_attribution!r} "
            f"not in created/modified/created_by_run"
        )
    if post_entry.get("in_expected_outputs") is not True:
        raise RuntimeError(
            f"post-restart {post_rel}: in_expected_outputs="
            f"{post_entry.get('in_expected_outputs')!r} != true"
        )

    # The post-restart run writes ``ok\n`` to result.txt; assert the inventory
    # captured its content SHA under final_sha256.
    expected_sha = hashlib.sha256(b"ok\n").hexdigest()
    actual_sha = post_entry.get("final_sha256")
    if not isinstance(actual_sha, str) or not actual_sha:
        raise RuntimeError(
            f"post-restart {post_rel}: final_sha256 missing/empty: {actual_sha!r}"
        )
    if actual_sha != expected_sha:
        raise RuntimeError(
            f"post-restart {post_rel}: final_sha256={actual_sha!r} != expected {expected_sha!r}"
        )

    pre_entry = files_by_path.get(pre_rel)
    if pre_entry is None:
        raise RuntimeError(
            f"post-restart inventory missing pre-restart leftover {pre_rel}: "
            f"{list(files_by_path)}"
        )
    if pre_entry.get("attribution") != "preexisting":
        raise RuntimeError(
            f"pre-restart leftover {pre_rel}: attribution={pre_entry.get('attribution')!r} "
            f"!= preexisting"
        )

    summary = inventory.get("attribution_summary", {})
    # Round 4 reclassified expected outputs as created/modified/unchanged/missing
    # (baseline-aware). The post-restart run produces its expected output, so
    # either the new "created"/"modified" or the legacy "created_by_run" bucket
    # must be non-empty.
    produced_count = (
        int(summary.get("created", 0))
        + int(summary.get("modified", 0))
        + int(summary.get("created_by_run", 0))
    )
    if produced_count < 1:
        raise RuntimeError(
            f"post-restart attribution_summary produced count < 1: {summary!r}"
        )


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
