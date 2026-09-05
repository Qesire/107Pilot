"""Rule-remediation derived-Run end-to-end smoke on the cpu-rc profile.

Goal: prove a REAL failed run -> rule-evaluated diagnosis -> approved remediation
-> derived Run -> re-evaluation, on the cpu-rc stack VIA HTTP (not direct Python
API, not hand-injected diagnosis as ``smoke_sim_phase2._verify_agent_remediation``
does). This smoke closes the FULL chain: diagnosis, execution, AND result
evaluation are all proven (the remediation session reaches ``succeeded`` with a
``verified_success`` evaluation bound to the derived run).

This smoke requires the cpu-rc Docker Slurm simulator to enforce walltime via
task/cgroup (ProctrackType=proctrack/cgroup, TaskPlugin=task/cgroup,
CgroupPlugin=cgroup/v2, /sys/fs/cgroup mounted rw, cgroup: host). With that in
place, Slurm cancels a job that exceeds its time_limit with a TIMEOUT terminal
state, which the rule engine matches as RUNTIME.TIMEOUT.

Scenario: submit a contract with a 00:01:00 time_limit and a 75s sleep that
creates the expected output file AFTER the sleep. Slurm cancels with TIMEOUT
before the output is produced, the rule engine authors suggested_patch =
{resources.time_limit: null}, AgentPolicyEngine resolves the null to the cpu-rc
profile's max_wall_hours (4 -> "04:00:00"), and the derived run re-submits the
same 75s sleep with the full 4h limit -> SUCCEEDED (output produced). The smoke
then drives the session through evaluation and asserts the evaluation closure:
session state == succeeded, >=1 evaluation bound to the derived run and the
current execution, outcome == verified_success, non-empty comparison, and
non-empty evidence_refs.

HTTP endpoints relied on (src/pilot107/api/remediation_routes.py):
  - POST /runs/{run_id}/remediation-sessions          (remediation_routes.py:152)
  - POST /remediation-sessions/{id}/advance           (remediation_routes.py:192)
  - POST /remediation-sessions/{id}/approve           (remediation_routes.py:200)
  - POST /remediation-sessions/{id}/execute           (remediation_routes.py:234)
  - GET  /remediation-sessions/{id}                   (remediation_routes.py:84, detail)
  - GET  /runs/{id}                                   (http_app.py:416)
  - POST /contracts, /runs/prepare, /runs/{id}/submit (smoke_competition_web.py patterns)

Provider is "none" (deterministic rules) and the cpu-rc capability profile is
loaded by the API via ``PILOT107_CAPABILITY_PROFILE_PATH``. Exit 0 ONLY if the
full diagnosis -> execution -> evaluation chain succeeds; exit 1 on any failure
(API error, wrong state, missing capsule, missing/incorrect evaluation, etc.).
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
        # 1. Submit a contract that will TIME OUT: a 75s sleep with a 00:01:00
        #    time_limit. The API accepts this (1m is within qos_cpu_rc's
        #    max_wall_hours=4), sbatch accepts it, Slurm (task/cgroup) cancels
        #    the job with a TIMEOUT terminal state once it exceeds 1 minute.
        #    The expected output file is created AFTER the sleep, so a TIMEOUT
        #    kills the job before the output is produced. The diagnosis matches
        #    RUNTIME.TIMEOUT which authors suggested_patch =
        #    {resources.time_limit: null}. AgentPolicyEngine resolves the null
        #    to the cpu-rc profile's max_wall_hours (4 -> "04:00:00"), the
        #    action becomes allowed_preview, and the derived run re-submits the
        #    same 75s sleep with the full 4h limit -> SUCCEEDED.
        command = (
            "sleep 75\n"
            "mkdir -p pilot107-cpu-rc-remediation\n"
            "echo ok > pilot107-cpu-rc-remediation/result.txt\n"
        )
        run = _create_submit_and_wait(
            command=command,
            expected_state="FAILED",
            expected_terminal_state="TIMEOUT",
        )
        source_run_id = run["run_id"]

        # 2. Create remediation session (deterministic rules, not LLM).
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

        # 3. Advance (drive WAITING_EVIDENCE -> DIAGNOSING -> PLANNING -> terminal).
        session = _post(f"/remediation-sessions/{session_id}/advance", {"provider": "none"})
        deadline = time.time() + 60
        while (
            session.get("state") in {"WAITING_EVIDENCE", "DIAGNOSING", "PLANNING"}
            and time.time() < deadline
        ):
            time.sleep(1)
            session = _post(f"/remediation-sessions/{session_id}/advance", {"provider": "none"})

        # 4. Inspect proposals.
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
        if not allowed:
            statuses = sorted({p.get("policy_status") for p in proposals})
            print(
                "remediation smoke failed: rule-evaluated diagnosis on cpu-rc "
                f"produced no auto-approvable action (policy_status in {statuses}); "
                f"session state={detail.get('state')} "
                f"stop_reason={detail.get('stop_reason')}.",
                file=sys.stderr,
            )
            return 1

        proposal = allowed[0]
        # The RUNTIME.TIMEOUT rule authors a patch on resources.time_limit.
        # Assert the diagnosis actually resolved to TIMEOUT (not some other
        # rule) by inspecting the allowed proposal's proposed_patch payload.
        patch = (proposal.get("payload") or {}).get("proposed_patch") or {}
        if "resources.time_limit" not in patch:
            print(
                "remediation smoke failed: allowed proposal patch does not target "
                f"resources.time_limit (diagnosis was not RUNTIME.TIMEOUT): "
                f"patch={patch}",
                file=sys.stderr,
            )
            return 1
        version_raw = detail.get("version")
        if not isinstance(version_raw, int):
            print(
                f"remediation smoke failed: session version not an int: {version_raw!r}",
                file=sys.stderr,
            )
            return 1
        version = version_raw
        # 5. Approve.
        _post(
            f"/remediation-sessions/{session_id}/approve",
            {
                "proposal_id": proposal["proposal_id"],
                "expected_version": version,
                "note": "accept-cpu-rc-remediation-smoke",
            },
        )
        # 6. Execute (submit the derived run).
        exec_payload = _post(
            f"/remediation-sessions/{session_id}/execute",
            {
                "proposal_id": proposal["proposal_id"],
                "expected_version": version + 1,
                "submit": True,
            },
        )
        derived_run_id = exec_payload.get("execution", {}).get(
            "derived_run_id"
        ) or exec_payload.get("derived_run_id")
        execution_id = exec_payload.get("execution_id")
        if not derived_run_id:
            detail = _get(f"/remediation-sessions/{session_id}")
            executions = detail.get("executions") or []
            if executions:
                derived_run_id = executions[-1].get("derived_run_id")
                if not execution_id:
                    execution_id = executions[-1].get("execution_id")
        if not derived_run_id:
            print(
                f"remediation smoke failed: execute did not produce a derived_run_id "
                f"for session={session_id}: {exec_payload}",
                file=sys.stderr,
            )
            return 1
        if not execution_id:
            print(
                f"remediation smoke failed: execute did not produce an execution_id "
                f"for session={session_id}: {exec_payload}",
                file=sys.stderr,
            )
            return 1

        # 7. Wait for the derived Run to succeed and assert remediation lineage.
        derived = _wait_run(derived_run_id, expected_state="SUCCEEDED")
        if derived.get("parent_run_id") != source_run_id:
            print(
                f"remediation smoke failed: derived run parent_run_id="
                f"{derived.get('parent_run_id')!r} != source {source_run_id!r}",
                file=sys.stderr,
            )
            return 1
        lineage_reason = derived.get("lineage_reason")
        if lineage_reason != "agent_remediation":
            print(
                f"remediation smoke failed: derived run lineage_reason="
                f"{lineage_reason!r} != agent_remediation",
                file=sys.stderr,
            )
            return 1
        derived_contract_id = derived.get("contract_id")
        if not derived_contract_id:
            print(
                f"remediation smoke failed: derived run missing contract_id "
                f"for run={derived_run_id}: {derived}",
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

        # 8. Drive the remediation session through evaluation to a terminal
        #    state. After the derived Run reaches SUCCEEDED, the session is in
        #    EXECUTING; calling /advance transitions EXECUTING -> EVALUATING
        #    (appends the EvaluationResult) -> SUCCEEDED (on verified_success).
        #    See src/pilot107/services/remediation_service.py:200,511,545.
        non_terminal = {
            "waiting_evidence",
            "diagnosing",
            "planning",
            "awaiting_input",
            "awaiting_approval",
            "preparing",
            "executing",
            "evaluating",
        }
        detail = _get(f"/remediation-sessions/{session_id}")
        deadline = time.time() + 120
        while detail.get("state") in non_terminal and time.time() < deadline:
            detail = _post(
                f"/remediation-sessions/{session_id}/advance",
                {"provider": "none"},
            )
            if detail.get("state") in non_terminal:
                time.sleep(1)
                detail = _get(f"/remediation-sessions/{session_id}")

        # 9. Assert the session reached the succeeded terminal state.
        session_state = detail.get("state")
        if session_state != "succeeded":
            print(
                f"remediation smoke failed: session did not reach succeeded; "
                f"state={session_state!r} stop_reason={detail.get('stop_reason')!r} "
                f"for session={session_id}",
                file=sys.stderr,
            )
            return 1

        # 10. Assert the evaluation closure: at least one evaluation bound to
        #     this execution and derived run, with a verified-success outcome,
        #     a non-empty comparison, and non-empty evidence_refs.
        evaluations = detail.get("evaluations") or []
        if not evaluations:
            print(
                f"remediation smoke failed: session succeeded but has no "
                f"evaluations for session={session_id}: {detail}",
                file=sys.stderr,
            )
            return 1
        matching = [
            ev
            for ev in evaluations
            if ev.get("derived_run_id") == derived_run_id and ev.get("execution_id") == execution_id
        ]
        if not matching:
            print(
                f"remediation smoke failed: no evaluation bound to "
                f"derived_run_id={derived_run_id} execution_id={execution_id}; "
                f"evaluations={evaluations}",
                file=sys.stderr,
            )
            return 1
        evaluation = matching[-1]
        outcome = evaluation.get("outcome")
        # EvaluationOutcome enum (src/pilot107/core/remediation.py:38-42):
        # verified_success | execution_success_unverified | failed | inconclusive.
        # verified_success is the only outcome that drives the session to
        # succeeded (remediation_service.py:573-575).
        if outcome != "verified_success":
            print(
                f"remediation smoke failed: evaluation outcome={outcome!r} "
                f"!= verified_success for session={session_id}: {evaluation}",
                file=sys.stderr,
            )
            return 1
        comparison = evaluation.get("comparison") or {}
        if not comparison:
            print(
                f"remediation smoke failed: evaluation comparison empty for "
                f"session={session_id}: {evaluation}",
                file=sys.stderr,
            )
            return 1
        # P1 (round 4): the evaluation must actually verify expected outputs
        # were newly produced (created/modified), not just trust the outcome
        # string. Assert the comparison carries an expected_outputs block, the
        # rollup ok flag, per-output status, and the expected_outputs_verified
        # check.
        expected_outputs_block = comparison.get("expected_outputs")
        if not expected_outputs_block or not isinstance(expected_outputs_block, list):
            print(
                f"remediation smoke failed: evaluation comparison.expected_outputs "
                f"missing/empty: {comparison}",
                file=sys.stderr,
            )
            return 1
        if comparison.get("expected_outputs_ok") is not True:
            print(
                f"remediation smoke failed: expected_outputs_ok is not true: "
                f"{comparison.get('expected_outputs_ok')!r}",
                file=sys.stderr,
            )
            return 1
        for entry in expected_outputs_block:
            status = entry.get("status")
            if status not in {"created", "modified"}:
                print(
                    f"remediation smoke failed: expected output "
                    f"{entry.get('path')!r} status={status!r} not in created/modified",
                    file=sys.stderr,
                )
                return 1
        checks = evaluation.get("checks") or []
        if not any(
            c.get("name") == "expected_outputs_verified" and c.get("status") == "passed"
            for c in checks
        ):
            print(
                f"remediation smoke failed: expected_outputs_verified check not passed: {checks}",
                file=sys.stderr,
            )
            return 1
        evidence_refs = evaluation.get("evidence_refs") or []
        if not evidence_refs:
            print(
                f"remediation smoke failed: evaluation evidence_refs empty for "
                f"session={session_id}: {evaluation}",
                file=sys.stderr,
            )
            return 1
        print(
            f"cpu-rc-remediation smoke ok source={source_run_id} "
            f"derived={derived_run_id} capsule={manifest_sha} "
            f"lineage_reason={lineage_reason} derived_contract_id={derived_contract_id} "
            f"session_state={session_state} outcome={outcome} "
            f"evaluation_id={evaluation.get('evaluation_id')} "
            f"evidence_refs={len(evidence_refs)} "
            f"expected_outputs_ok={comparison.get('expected_outputs_ok')}"
        )
        return 0
    except Exception as exc:  # noqa: BLE001 - smoke reports failures as exit 1
        print(f"cpu-rc-remediation smoke failed: {exc}", file=sys.stderr)
        return 1


def _create_submit_and_wait(
    *, command: str, expected_state: str, expected_terminal_state: str | None = None
) -> dict:
    contract = _post("/contracts", _contract(command))
    prepared = _post("/runs/prepare", {"contract_id": contract["contract_id"]})
    _post(f"/runs/{prepared['run_id']}/submit", {})
    return _wait_run(
        prepared["run_id"],
        expected_state=expected_state,
        expected_terminal_state=expected_terminal_state,
    )


def _wait_run(
    run_id: str,
    *,
    expected_state: str,
    expected_terminal_state: str | None = None,
) -> dict:
    last: dict = {}
    for _ in range(240):
        last = _get(f"/runs/{run_id}")
        if (
            last.get("state") == expected_state
            and last.get("collection_state") == "succeeded"
            # Diagnosis must be terminal before remediation advance will progress.
            and last.get("diagnosis_state") in {"succeeded", "skipped"}
            and (
                expected_terminal_state is None
                or last.get("terminal_state") == expected_terminal_state
            )
        ):
            return last
        time.sleep(1)
    raise RuntimeError(
        f"run {run_id} did not reach {expected_state}/succeeded"
        f"{f' terminal_state={expected_terminal_state}' if expected_terminal_state else ''}"
        f": {last}"
    )


def _contract(command: str) -> dict:
    # CPU-RC partition with a VALID qos and a 00:01:00 time_limit so the
    # 75s sleep command times out at runtime (task/cgroup enforces walltime).
    # The API accepts this (1m is within qos_cpu_rc's max_wall_hours=4);
    # sbatch accepts it; Slurm cancels with a TIMEOUT terminal state, matching
    # the RUNTIME.TIMEOUT rule symptom. The AgentPolicyEngine resolves
    # {resources.time_limit: null} -> "04:00:00" (the qos max_wall_hours), so
    # the derived run re-submits with the full limit and the 75s sleep SUCCEEDS
    # (producing the expected output file).
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
            "time_limit": os.environ.get("PILOT107_SMOKE_TIME_LIMIT", "00:01:00"),
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
