"""Rule-remediation derived-Run gap smoke on the cpu-rc profile.

Goal: prove a REAL failed run -> rule-evaluated diagnosis -> approved remediation
-> derived Run -> re-evaluation, on the cpu-rc stack VIA HTTP (not direct Python
API, not hand-injected diagnosis as ``smoke_sim_phase2._verify_agent_remediation``
does).

HTTP endpoints relied on (src/pilot107/api/remediation_routes.py):
  - POST /runs/{run_id}/remediation-sessions          (remediation_routes.py:152)
  - POST /remediation-sessions/{id}/advance           (remediation_routes.py:192)
  - POST /remediation-sessions/{id}/approve           (remediation_routes.py:200)
  - POST /remediation-sessions/{id}/execute           (remediation_routes.py:234)
  - GET  /remediation-sessions/{id}                   (remediation_routes.py:84, detail)
  - GET  /runs/{id}                                   (http_app.py:416)
  - POST /contracts, /runs/prepare, /runs/{id}/submit (smoke_competition_web.py patterns)

GAP (confirmed by reading the rule engine):
  The deterministic rule engine (``src/pilot107/core/diagnosis.py`` +
  ``data/known_errors/*.yaml``) does NOT author a fully-resolved
  ``suggested_patch`` for any CPU-RC-matchable failure. Every fallback/known
  rule's ``fix_template.patch`` is either ``{}`` (e.g. RUNTIME.NONZERO_EXIT ->
  ``policy_status="manual_only"``) or contains ``null`` placeholders (e.g.
  SLURM.INVALID_QOS -> ``policy_status="requires_input"``). The only non-null
  patch (SLURM.WORKDIR_NOT_SHARED) contains ``<user>``/``<run_id>`` placeholders
  that ``AgentPolicyEngine`` classifies as ``requires_input``.

  Consequence: ``AgentPolicyEngine._action_for`` (``src/pilot107/core/advice.py``)
  never returns ``policy_status="allowed_preview"`` for a real rule-evaluated
  diagnosis, so ``RemediationService._plan_turn`` transitions the session to
  ``AWAITING_INPUT`` or ``BLOCKED`` (stop_reason="no_safe_action"), never to
  ``AWAITING_APPROVAL``. Therefore HTTP ``approve`` / ``execute`` cannot fire and
  no derived Run is produced through the real rule-evaluated HTTP path.

This smoke exercises as much of the path as IS wired:
  1. Submit a contract designed to FAIL with a rule-matchable error (``exit 7``)
     -> RUNTIME.NONZERO_EXIT (state_match FAILED + exit_code not in {None,"0:0"}).
  2. Wait run FAILED + collection_state succeeded + diagnosis_state succeeded.
  3. POST create remediation session (provider:"none", manual_approval).
  4. POST advance (provider:"none").
  5. GET session detail; assert a turn + proposal exist (rule-evaluated diagnosis
     produced an action bound to advice).
  6. If any proposal has ``policy_status == "allowed_preview"``, continue:
     approve -> execute -> wait derived Run SUCCEEDED -> assert
     ``parent_run_id`` and ``lineage_reason == "agent_remediation"``.
  7. Otherwise: print the gap, exit 1.

Per the task contract: exit 0 ONLY if the full derived-Run succeeds; exit 1 with
a clear message if the path is incomplete. Do NOT fake it.
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
        # 1. Submit a contract that fails with exit 7 -> RUNTIME.NONZERO_EXIT.
        command = (
            "hostname\n"
            "echo cpu-rc-remediation-failure >&2\n"
            "mkdir -p pilot107-cpu-rc-remediation\n"
            "echo failed > pilot107-cpu-rc-remediation/result.txt\n"
            "exit 7\n"
        )
        run = _create_submit_and_wait(command=command, expected_state="FAILED")
        source_run_id = run["run_id"]
        if run.get("exit_code") not in {"7", "7:0"}:
            print(
                f"remediation smoke failed: expected exit 7, got {run.get('exit_code')!r}",
                file=sys.stderr,
            )
            return 1

        # 3. Create remediation session (deterministic rules, not LLM).
        request_key = f"accept-cpu-rc-remediation-{source_run_id}"
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

        # 4. Advance (drive WAITING_EVIDENCE -> DIAGNOSING -> PLANNING -> terminal).
        session = _post(f"/remediation-sessions/{session_id}/advance", {"provider": "none"})
        # Poll session state until it stops progressing.
        deadline = time.time() + 60
        while (
            session.get("state") in {"WAITING_EVIDENCE", "DIAGNOSING", "PLANNING"}
            and time.time() < deadline
        ):
            time.sleep(1)
            session = _post(f"/remediation-sessions/{session_id}/advance", {"provider": "none"})

        # 5. Inspect proposals.
        detail = _get(f"/remediation-sessions/{session_id}")
        proposals = detail.get("proposals") or []
        turns = detail.get("turns") or []
        if not turns or not proposals:
            print(
                f"remediation smoke failed: rule engine produced no turn/proposal "
                f"for run={source_run_id}: state={detail.get('state')} "
                f"turns={len(turns)} proposals={len(proposals)}",
                file=sys.stderr,
            )
            return 1

        allowed = [p for p in proposals if p.get("policy_status") == "allowed_preview"]

        # 6/7. Full path only if a rule-authored allowed_preview action exists.
        if not allowed:
            statuses = sorted({p.get("policy_status") for p in proposals})
            print(
                "remediation smoke GAP: rule-evaluated diagnosis on cpu-rc produced "
                f"no auto-approvable action (policy_status in {statuses}); "
                f"session state={detail.get('state')} "
                f"stop_reason={detail.get('stop_reason')}. "
                "The rule engine authors only empty/null-placeholder patches, so "
                "HTTP approve -> execute -> derived-Run cannot fire. "
                "Derived-Run remediation via the real rule-evaluated HTTP path is "
                "NOT wired. Run remains FAILED; no fake derived Run was created.",
                file=sys.stderr,
            )
            return 1

        proposal = allowed[0]
        version_raw = detail.get("version")
        if not isinstance(version_raw, int):
            print(
                f"remediation smoke failed: session version not an int: {version_raw!r}",
                file=sys.stderr,
            )
            return 1
        version = version_raw
        # Approve.
        _post(
            f"/remediation-sessions/{session_id}/approve",
            {
                "proposal_id": proposal["proposal_id"],
                "expected_version": version,
                "note": "accept-cpu-rc-remediation-smoke",
            },
        )
        # Execute (submit the derived run).
        exec_payload = _post(
            f"/remediation-sessions/{session_id}/execute",
            {
                "proposal_id": proposal["proposal_id"],
                "expected_version": version + 1,
                "submit": True,
            },
        )
        derived_run_id = (
            exec_payload.get("execution", {}).get("derived_run_id")
            or exec_payload.get("derived_run_id")
        )
        if not derived_run_id:
            # Fall back to inspecting executions on the session.
            detail = _get(f"/remediation-sessions/{session_id}")
            executions = detail.get("executions") or []
            if executions:
                derived_run_id = executions[-1].get("derived_run_id")
        if not derived_run_id:
            print(
                f"remediation smoke failed: execute did not produce a derived_run_id "
                f"for session={session_id}: {exec_payload}",
                file=sys.stderr,
            )
            return 1

        derived = _wait_run(derived_run_id, expected_state="SUCCEEDED")
        if derived.get("parent_run_id") != source_run_id:
            print(
                f"remediation smoke failed: derived run parent_run_id="
                f"{derived.get('parent_run_id')!r} != source {source_run_id!r}",
                file=sys.stderr,
            )
            return 1
        if derived.get("lineage_reason") != "agent_remediation":
            print(
                f"remediation smoke failed: derived run lineage_reason="
                f"{derived.get('lineage_reason')!r} != agent_remediation",
                file=sys.stderr,
            )
            return 1
        capsule = _get(f"/runs/{derived_run_id}/capsule")
        manifest_sha = (capsule.get("capsule") or {}).get("manifest_sha256")
        if not manifest_sha:
            print(
                f"remediation smoke failed: derived run capsule missing manifest "
                f"for run={derived_run_id}: {capsule}",
                file=sys.stderr,
            )
            return 1
        print(
            f"cpu-rc-remediation smoke ok source={source_run_id} "
            f"derived={derived_run_id} capsule={manifest_sha}"
        )
        return 0
    except Exception as exc:  # noqa: BLE001 - smoke reports failures as exit 1
        print(f"cpu-rc-remediation smoke failed: {exc}", file=sys.stderr)
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
        if (
            last.get("state") == expected_state
            and last.get("collection_state") == "succeeded"
            # Diagnosis must be terminal before remediation advance will progress.
            and last.get("diagnosis_state") in {"succeeded", "skipped"}
        ):
                return last
        time.sleep(1)
    raise RuntimeError(f"run {run_id} did not reach {expected_state}/succeeded: {last}")


def _contract(command: str) -> dict:
    return {
        "recipe_version_id": "recipe_python_cpu@1.0.0",
        "project": {"workdir": "/public/home/alice"},
        "entry": {
            "command": command,
            "expected_outputs": ["pilot107-cpu-rc-remediation/result.txt"],
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
