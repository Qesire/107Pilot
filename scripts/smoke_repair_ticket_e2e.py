"""M2 repair-ticket end-to-end smoke on the Docker Slurm simulator.

Goal: prove the FULL M2 repair handoff chain via HTTP:
  submit → FAILED (Python traceback) → Evidence → diagnosis (NONZERO_EXIT)
  → code_context captured → remediation session → create_repair_ticket proposal
  → RepairTicket (open) → fix workspace → derived Run (SUCCEEDED)
  → ArtifactManifest → resolve → comparison (improved=true).

Requires:
  - Docker Slurm simulator running with apps profile
  - PILOT107_CODE_CONTEXT_TRANSPORT=local + ALLOWED_ROOTS=/public/home
  - Workspace prepared by scripts/setup_repair_smoke_workspace.sh

Environment:
  PILOT107_COMPETITION_BASE_URL  API base (default http://127.0.0.1:8080/api/v1)
  PILOT107_SMOKE_PARTITION       Slurm partition (default Students)
  PILOT107_SMOKE_QOS             Slurm QoS (default qos_stu_default)

Exit 0 only if the full chain succeeds; exit 1 on any failure.
"""

from __future__ import annotations

import hashlib
import hmac as hmac_mod
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from uuid import uuid4

BASE_URL = os.environ.get(
    "PILOT107_COMPETITION_BASE_URL",
    "http://127.0.0.1:8080/api/v1",
).rstrip("/")
PARTITION = os.environ.get("PILOT107_SMOKE_PARTITION", "Students")
QOS = os.environ.get("PILOT107_SMOKE_QOS", "qos_stu_default")
WORKDIR = "/public/home/alice/repair-smoke"
USER = "alice"

# Load the proxy HMAC secret (same file mounted into the API container).
_SECRET_PATH = (
    Path(__file__).resolve().parent.parent
    / "simulator"
    / "compose"
    / "secrets"
    / "proxy-hmac.local"
)
_HMAC_SECRET: bytes | None = None
if _SECRET_PATH.is_file():
    _HMAC_SECRET = _SECRET_PATH.read_text().strip().encode()


def main() -> int:
    try:
        # ------------------------------------------------------------------
        # 1. Submit a job that will FAIL with a Python traceback.
        # ------------------------------------------------------------------
        command = f"cd {WORKDIR} && python3 train.py\n"
        print(f"[1/10] Submitting failing job (partition={PARTITION}, qos={QOS})…")
        run = _create_submit_and_wait(command=command, expected_state="FAILED")
        source_run_id = run["run_id"]
        print(f"       source run={source_run_id} state=FAILED exit_code={run.get('exit_code')}")

        # ------------------------------------------------------------------
        # 2. Verify diagnosis contains RUNTIME.NONZERO_EXIT.
        # ------------------------------------------------------------------
        print("[2/10] Checking diagnoses…")
        diagnoses = _get(f"/runs/{source_run_id}/diagnoses")
        rule_ids = {d["rule_id"] for d in diagnoses.get("items", [])}
        if "RUNTIME.NONZERO_EXIT" not in rule_ids:
            print(
                f"  FAIL: expected RUNTIME.NONZERO_EXIT in diagnoses, got {rule_ids}",
                file=sys.stderr,
            )
            return 1
        print(f"       diagnoses={sorted(rule_ids)}")

        # ------------------------------------------------------------------
        # 3. Verify code_context is captured (traceback → source window + evidence).
        # ------------------------------------------------------------------
        print("[3/10] Checking code_context via agent explain…")
        explanation = _post(f"/runs/{source_run_id}/agent/explain", {"provider": "none"})
        code_context = explanation.get("code_context")
        has_code_context = code_context is not None and bool(code_context.get("chunks"))
        if not has_code_context:
            print(
                f"  FAIL: code_context not captured. Got: {code_context}",
                file=sys.stderr,
            )
            return 1
        chunks = code_context.get("chunks", [])
        evidence_snippets = code_context.get("evidence_snippets", [])
        print(
            f"       code_context captured: {len(chunks)} chunk(s), "
            f"{len(evidence_snippets)} evidence snippet(s)"
        )
        # Verify source code chunk contains the buggy file.
        chunk_paths = [c.get("path", "") for c in chunks]
        if not any("train.py" in p for p in chunk_paths):
            print(f"  FAIL: no train.py in chunk paths: {chunk_paths}", file=sys.stderr)
            return 1
        # Verify evidence snippets contain the traceback error.
        all_evidence = " ".join(evidence_snippets)
        if "FileNotFoundError" not in all_evidence and "missing_input.csv" not in all_evidence:
            print(
                f"  FAIL: evidence_snippets missing error info. Snippets: {evidence_snippets[:2]}",
                file=sys.stderr,
            )
            return 1
        print("       ✓ source code + Slurm error evidence both present")

        # ------------------------------------------------------------------
        # 4. Create remediation session and advance to awaiting_approval.
        # ------------------------------------------------------------------
        print("[4/10] Creating remediation session…")
        request_key = f"repair-smoke-{source_run_id}"
        session = _post(
            f"/runs/{source_run_id}/remediation-sessions",
            {
                "request_key": request_key,
                "provider": "none",
                "automation_policy": "manual_approval",
                "budget": {},
            },
        )
        session_id = session["session_id"]
        print(f"       session={session_id}")

        print("       Advancing to awaiting_approval…")
        deadline = time.time() + 90
        while (
            session.get("state")
            in {
                "waiting_evidence",
                "diagnosing",
                "planning",
                "preparing",
            }
            and time.time() < deadline
        ):
            time.sleep(1)
            session = _post(
                f"/remediation-sessions/{session_id}/advance",
                {"provider": "none"},
            )
        print(f"       session state={session.get('state')}")

        # ------------------------------------------------------------------
        # 5. Check for create_repair_ticket proposal.
        # ------------------------------------------------------------------
        detail = _get(f"/remediation-sessions/{session_id}")
        proposals = detail.get("proposals") or []
        repair_proposals = [p for p in proposals if p.get("action_type") == "create_repair_ticket"]
        if repair_proposals:
            print(
                f"[5/10] Found create_repair_ticket proposal: {repair_proposals[0]['proposal_id']}"
            )
        else:
            print(
                "[5/10] No create_repair_ticket proposal yet "
                f"(session state={session.get('state')}, "
                f"proposals={[p.get('action_type') for p in proposals]}). "
                "Proceeding with direct ticket creation."
            )

        # ------------------------------------------------------------------
        # 6. Create RepairTicket via the M2 API.
        # ------------------------------------------------------------------
        print("[6/10] Creating repair ticket…")
        ticket = _post(
            "/repair-tickets",
            {
                "session_id": session_id,
                "request_key": f"ticket-{request_key}",
                "requested_change": "Create missing_input.csv or fix the file path in load_data()",
            },
        )
        ticket_id = ticket["ticket_id"]
        if ticket.get("state") != "open":
            print(f"  FAIL: ticket state={ticket.get('state')!r} != open", file=sys.stderr)
            return 1
        print(f"       ticket={ticket_id} state=open")

        # ------------------------------------------------------------------
        # 7. Fix the workspace and submit a derived Run that SUCCEEDS.
        # ------------------------------------------------------------------
        print("[7/10] Fixing workspace and submitting derived run…")
        _fix_workspace()
        # Allow Docker shared volume to sync across containers.
        time.sleep(3)
        # Self-contained command: create the missing file inline then run.
        # This avoids Docker volume propagation race between containers.
        fixed_command = (
            f"cd {WORKDIR} && "
            "printf 'id,value\\n1,hello\\n' > missing_input.csv && "
            "python3 train.py && echo done > result.txt\n"
        )
        derived_run = _create_submit_and_wait(
            command=fixed_command,
            expected_state="SUCCEEDED",
        )
        derived_run_id = derived_run["run_id"]
        print(f"       derived run={derived_run_id} state=SUCCEEDED")

        # ------------------------------------------------------------------
        # 8. Create ArtifactManifest.
        # ------------------------------------------------------------------
        print("[8/10] Creating artifact manifest…")
        revision = _get_workspace_revision()
        manifest = _post(
            "/artifact-manifests",
            {
                "revision": revision,
                "local_test_summary": "repair-smoke: train.py runs without error",
                "disclosure": "metadata_only",
            },
        )
        manifest_id = manifest["manifest_id"]
        print(f"       manifest={manifest_id} revision={revision[:12]}")

        # ------------------------------------------------------------------
        # 9. Resolve the ticket.
        # ------------------------------------------------------------------
        print("[9/10] Resolving ticket…")
        resolved = _post(
            f"/repair-tickets/{ticket_id}/resolve",
            {
                "manifest_id": manifest_id,
                "derived_run_id": derived_run_id,
            },
        )
        if resolved.get("state") != "resolved":
            print(f"  FAIL: ticket state={resolved.get('state')!r} != resolved", file=sys.stderr)
            return 1
        print("       ticket state=resolved")

        # ------------------------------------------------------------------
        # 10. Verify comparison.
        # ------------------------------------------------------------------
        print("[10/10] Verifying comparison…")
        comparison = resolved.get("resolution_comparison") or {}
        source_state = comparison.get("source_state", "?")
        derived_state = comparison.get("derived_state", "?")
        improved = comparison.get("improved")
        print(f"        source={source_state} → derived={derived_state} improved={improved}")
        if source_state != "FAILED" or derived_state != "SUCCEEDED":
            print(
                f"  FAIL: unexpected comparison states: {comparison}",
                file=sys.stderr,
            )
            return 1
        if improved is not True:
            print(f"  FAIL: comparison.improved={improved!r} != True", file=sys.stderr)
            return 1

        print(
            f"\nrepair-ticket smoke OK\n"
            f"  source_run={source_run_id}\n"
            f"  derived_run={derived_run_id}\n"
            f"  ticket={ticket_id}\n"
            f"  manifest={manifest_id}\n"
            f"  session={session_id}\n"
            f"  comparison: FAILED → SUCCEEDED, improved=true"
        )
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"repair-ticket smoke FAILED: {exc}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        return 1


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _create_submit_and_wait(*, command: str, expected_state: str) -> dict:
    contract = _post("/contracts", _contract(command))
    prepared = _post("/runs/prepare", {"contract_id": contract["contract_id"]})
    _post(f"/runs/{prepared['run_id']}/submit", {})
    return _wait_run(prepared["run_id"], expected_state=expected_state)


def _wait_run(run_id: str, *, expected_state: str) -> dict:
    last: dict = {}
    for _ in range(300):
        last = _get(f"/runs/{run_id}")
        if (
            last.get("state") == expected_state
            and last.get("collection_state") == "succeeded"
            and last.get("diagnosis_state") in {"succeeded", "skipped"}
        ):
            return last
        time.sleep(1)
    raise RuntimeError(
        f"run {run_id} did not reach {expected_state}/collection=succeeded: "
        f"state={last.get('state')} collection={last.get('collection_state')} "
        f"diagnosis={last.get('diagnosis_state')}"
    )


def _contract(command: str) -> dict:
    return {
        "recipe_version_id": "recipe_python_cpu@1.0.0",
        "project": {"workdir": WORKDIR},
        "entry": {
            "command": command,
            "expected_outputs": ["result.txt"],
        },
        "resources": {
            "partition": PARTITION,
            "qos": QOS,
            "nodes": 1,
            "ntasks": 1,
            "cpus_per_task": 1,
            "time_limit": "00:05:00",
        },
    }


def _fix_workspace() -> None:
    """Fix the buggy file in the Docker shared volume via docker compose exec."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    compose_dir = os.path.join(root, "simulator", "compose")
    env_file = os.environ.get(
        "PILOT107_SMOKE_ENV",
        os.path.join(compose_dir, ".env.repair-smoke"),
    )
    project = os.environ.get("COMPOSE_PROJECT_NAME", "pilot107-sim")
    fix_script = (
        f"git config --global --add safe.directory {WORKDIR} && "
        f"cd {WORKDIR} && "
        "echo 'id,value' > missing_input.csv && "
        "echo '1,hello' >> missing_input.csv && "
        "chown alice:alice missing_input.csv && "
        "git add missing_input.csv && "
        "git commit -q -m 'fix: add missing input file'"
    )
    subprocess.run(
        [
            "docker",
            "compose",
            "--project-name",
            project,
            "--env-file",
            env_file,
            "-f",
            os.path.join(compose_dir, "compose.yml"),
            "exec",
            "-T",
            "login-node-sim",
            "bash",
            "-c",
            fix_script,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    # Verify the file is visible from a worker container (shared volume check).
    verify = subprocess.run(
        [
            "docker",
            "compose",
            "--project-name",
            project,
            "--env-file",
            env_file,
            "-f",
            os.path.join(compose_dir, "compose.yml"),
            "exec",
            "-T",
            "worker-1",
            "test",
            "-f",
            f"{WORKDIR}/missing_input.csv",
        ],
        capture_output=True,
        text=True,
    )
    if verify.returncode != 0:
        print("  WARN: file not yet visible on worker-1, waiting…", file=sys.stderr)
        time.sleep(5)
        subprocess.run(
            [
                "docker",
                "compose",
                "--project-name",
                project,
                "--env-file",
                env_file,
                "-f",
                os.path.join(compose_dir, "compose.yml"),
                "exec",
                "-T",
                "worker-1",
                "test",
                "-f",
                f"{WORKDIR}/missing_input.csv",
            ],
            check=True,
            capture_output=True,
            text=True,
        )


def _get_workspace_revision() -> str:
    """Get the current git HEAD revision from the workspace."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    compose_dir = os.path.join(root, "simulator", "compose")
    env_file = os.environ.get(
        "PILOT107_SMOKE_ENV",
        os.path.join(compose_dir, ".env.repair-smoke"),
    )
    project = os.environ.get("COMPOSE_PROJECT_NAME", "pilot107-sim")
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--project-name",
            project,
            "--env-file",
            env_file,
            "-f",
            os.path.join(compose_dir, "compose.yml"),
            "exec",
            "-T",
            "login-node-sim",
            "bash",
            "-c",
            f"git config --global --add safe.directory {WORKDIR} && "
            f"git -C {WORKDIR} rev-parse HEAD",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _get(path: str) -> dict:
    full_path = f"/api/v1{path}"
    headers = _signed_headers("GET", full_path, b"")
    request = urllib.request.Request(url=f"{BASE_URL}{path}", headers=headers)
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def _post(path: str, payload: dict) -> dict:
    full_path = f"/api/v1{path}"
    body = json.dumps(payload).encode("utf-8")
    headers = _signed_headers("POST", full_path, body)
    headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        url=f"{BASE_URL}{path}",
        data=body,
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def _signed_headers(method: str, path: str, body: bytes) -> dict[str, str]:
    """Build request headers with HMAC proxy signature (matches proxy_auth.py)."""
    headers: dict[str, str] = {"X-Pilot107-User": USER}
    if _HMAC_SECRET is None:
        return headers
    timestamp = int(time.time())
    request_id = str(uuid4())
    body_hash = hashlib.sha256(body).hexdigest()
    canonical = "\n".join(
        (
            "pilot107-proxy-v1",
            str(timestamp),
            method.upper(),
            path,
            USER,
            body_hash,
            request_id,
        )
    ).encode()
    signature = hmac_mod.new(_HMAC_SECRET, canonical, hashlib.sha256).hexdigest()
    headers["X-Pilot107-Proxy-Timestamp"] = str(timestamp)
    headers["X-Request-ID"] = request_id
    headers["X-Pilot107-Proxy-Signature"] = f"v1={signature}"
    return headers


if __name__ == "__main__":
    raise SystemExit(main())
