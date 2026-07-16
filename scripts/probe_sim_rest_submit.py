"""Simulator REST submit probe — REAL submit / get / cancel against v0.0.41.

Previously this probe was effectively skipped because the auth probe reported
``blocked`` (it sent ``dev-token`` which real JWT auth rejects). With Lane 1's
live JWT auth and Lane 3's real-token mint, this probe now:

* mints a real JWT via ``scontrol token`` (as ``alice``);
* targets ``v0.0.41`` by default for the Slurm 25.11 simulator target;
* submits a real sleep job through ``RestNativeSlurmBackend``;
* verifies the job is visible via ``GET /slurm/v0.0.41/job/{id}``;
* cancels it via ``DELETE /slurm/v0.0.41/job/{id}``.

The token is NEVER persisted to the output JSON, logs, or error messages.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _sim_rest_helpers import (  # noqa: E402
    DEFAULT_API_VERSION,
    DEFAULT_REST_USER,
    detect_sim_rest_url,
    mint_sim_token,
)

from pilot107.adapters.slurm import (
    RestAuthStyle,
    RestNativeSlurmBackend,
    SlurmBackendError,
    SlurmSubmissionRejected,
    SlurmTransportError,
    SubmitIntent,
    UrllibHttpTransport,
)
from pilot107.core.resources import ResourcePlan
from pilot107.core.run_store import utc_now_iso
from pilot107.core.states import RunState


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    output_path = root / "artifacts" / "probes" / "sim_rest_submit.json"
    base_url = detect_sim_rest_url()
    api_version = DEFAULT_API_VERSION
    rest_user = DEFAULT_REST_USER

    token_mint: dict[str, Any]
    try:
        token = mint_sim_token(user=rest_user)
        token_mint = {"status": "ok"}
    except Exception as exc:  # noqa: BLE001
        payload = {
            "observed_at": utc_now_iso(),
            "target": base_url,
            "api_version": api_version,
            "rest_user": rest_user,
            "token_mint": {"status": "failed", "error": str(exc)},
            "submit_attempt": None,
            "summary": {"status": "skipped", "skipped_reason": "token mint failed"},
        }
        _write_payload(output_path, payload)
        print("sim rest submit probe skipped (token mint failed)")
        print("artifact=" + str(output_path))
        return 1

    payload = _probe_submit(
        base_url=base_url,
        api_version=api_version,
        rest_user=rest_user,
        token=token,
        token_mint=token_mint,
    )
    _write_payload(output_path, payload)
    print("sim rest submit probe " + payload["summary"]["status"])
    print("artifact=" + str(output_path))
    return 0 if payload["summary"]["status"] == "submitted" else 1


def _write_payload(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _probe_submit(
    *,
    base_url: str,
    api_version: str,
    rest_user: str,
    token: str,
    token_mint: dict[str, Any],
) -> dict[str, Any]:
    workdir = f"/public/home/{rest_user}/rest-submit-probe"

    setup = _ensure_workdir(workdir, rest_user)
    if setup["returncode"] != 0:
        return {
            "observed_at": utc_now_iso(),
            "target": base_url,
            "api_version": api_version,
            "rest_user": rest_user,
            "token_mint": token_mint,
            "workdir_setup": setup,
            "submit_attempt": None,
            "summary": {
                "status": "failed",
                "failed_reason": "failed to prepare simulator REST submit workdir",
            },
        }

    backend = RestNativeSlurmBackend(
        transport=UrllibHttpTransport(
            base_url=base_url,
            timeout_seconds=15.0,
            auth_style=RestAuthStyle.SLURM_HEADERS,
            slurm_username=rest_user,
        ),
        api_version=api_version,
        token=token,
    )

    submit_attempt: dict[str, Any]
    get_attempt: dict[str, Any]
    cancel_attempt: dict[str, Any]
    status: str
    reason: str | None

    # ----- submit -----
    try:
        receipt = backend.submit(
            SubmitIntent(
                user=rest_user,
                workdir=Path(workdir),
                script="#!/bin/bash\nhostname\necho rest-submit-ok\n",
                resource_plan=ResourcePlan(
                    partition="Students",
                    qos="qos_stu_medium_2gpu",
                    nodes=1,
                    ntasks=1,
                    cpus_per_task=1,
                    time_limit="00:02:00",
                ),
                idempotency_key=f"rest-submit-probe-{utc_now_iso()}",
            )
        )
        submit_attempt = {
            "status": "submitted",
            "job_id": receipt.job_id,
            "run_state": receipt.run_state.value,
            "strategy": receipt.strategy.value,
        }
    except (SlurmBackendError, SlurmSubmissionRejected, SlurmTransportError) as exc:
        return {
            "observed_at": utc_now_iso(),
            "target": base_url,
            "api_version": api_version,
            "rest_user": rest_user,
            "token_mint": token_mint,
            "workdir_setup": setup,
            "submit_attempt": {
                "status": "failed",
                "error_type": exc.__class__.__name__,
                "error": str(exc),
            },
            "get_attempt": None,
            "cancel_attempt": None,
            "summary": {
                "status": "failed",
                "failed_reason": "REST submit rejected or transport failed",
            },
        }

    # ----- get -----
    try:
        snapshot = backend.get_job(user=rest_user, job_id=receipt.job_id)
        get_attempt = {
            "status": "ok",
            "job_id": snapshot.job_id,
            "run_state": snapshot.run_state.value,
            "owner": snapshot.owner,
            "raw_state_flags": snapshot.raw_state_flags,
        }
    except (SlurmBackendError, SlurmTransportError) as exc:
        get_attempt = {
            "status": "failed",
            "error_type": exc.__class__.__name__,
            "error": str(exc),
        }

    # ----- cancel -----
    try:
        cancelled = backend.cancel(user=rest_user, job_id=receipt.job_id)
        cancel_attempt = {
            "status": "ok",
            "run_state": cancelled.run_state.value,
        }
    except (SlurmBackendError, SlurmTransportError) as exc:
        cancel_attempt = {
            "status": "failed",
            "error_type": exc.__class__.__name__,
            "error": str(exc),
        }

    # Wait briefly for the cancel to land so the final snapshot is meaningful.
    final_snapshot = _wait_for_terminal(backend=backend, user=rest_user, job_id=receipt.job_id)

    submit_ok = submit_attempt.get("status") == "submitted"
    get_ok = get_attempt.get("status") == "ok"
    cancel_ok = cancel_attempt.get("status") == "ok"
    if submit_ok and get_ok and cancel_ok:
        status = "submitted"
        reason = None
    else:
        status = "failed"
        reason = "one of submit/get/cancel failed"

    return {
        "observed_at": utc_now_iso(),
        "target": base_url,
        "api_version": api_version,
        "rest_user": rest_user,
        "token_mint": token_mint,
        "workdir_setup": setup,
        "submit_attempt": submit_attempt,
        "get_attempt": get_attempt,
        "cancel_attempt": cancel_attempt,
        "final_snapshot": final_snapshot,
        "summary": {"status": status, "failed_reason": reason},
    }


def _ensure_workdir(workdir: str, rest_user: str) -> dict[str, Any]:
    result = subprocess.run(
        [
            "docker",
            "exec",
            "pilot107-sim-login-node-sim-1",
            "bash",
            "-lc",
            (
                f"mkdir -p {workdir} && chown {rest_user}:{rest_user} {workdir} "
                f"&& chmod 0750 {workdir}"
            ),
        ],
        check=False,
        text=True,
        capture_output=True,
        timeout=10,
    )
    return {
        "returncode": result.returncode,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
        "workdir": workdir,
    }


def _wait_for_terminal(
    *,
    backend: RestNativeSlurmBackend,
    user: str,
    job_id: str,
) -> dict[str, Any] | None:
    for _ in range(15):
        try:
            snapshot = backend.get_job(user=user, job_id=job_id)
        except SlurmTransportError:
            return None
        if snapshot.run_state in {RunState.SUCCEEDED, RunState.FAILED, RunState.CANCELLED}:
            return {
                "run_state": snapshot.run_state.value,
                "owner": snapshot.owner,
                "raw_state_flags": snapshot.raw_state_flags,
                "exit_code": snapshot.exit_code,
                "reason": snapshot.reason,
            }
        time.sleep(1)
    return None


if __name__ == "__main__":
    raise SystemExit(main())
