#!/usr/bin/env python3
"""Live acceptance for Project -> AgentTask -> Slurm -> Evidence gate -> follow-up.

The script is deliberately fail-closed: a scheduling receipt is only an admission
fact, legacy AgentTask success is not accepted as scientific completion, and the
ready follow-up may appear only after the immutable terminal gate is verified.
"""

from __future__ import annotations

import json
import os
import re
import ssl
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta
from typing import Any, Callable

_TERMINAL_TASK_STATES = {"succeeded", "failed", "cancelled", "auth_required"}
_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_COMPLETION_POLICIES = {"evidence_required", "evidence_and_capsule_required"}


class ApiClient:
    def __init__(self, base_url: str, *, owner: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.owner = owner
        self.context = (
            ssl._create_unverified_context() if self.base_url.startswith("https://") else None
        )

    def get(self, path: str) -> dict[str, Any]:
        return self.request("GET", path)

    def post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self.request("POST", path, payload=payload)

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"X-Pilot107-User": self.owner}
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"{self.base_url}{path}", data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=30, context=self.context) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"{method} {path} failed with HTTP {exc.code}: {detail}") from None
        except urllib.error.URLError as exc:
            raise RuntimeError(f"{method} {path} transport failed: {exc.reason}") from None
        try:
            decoded = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"{method} {path} returned invalid JSON: {exc}") from None
        if not isinstance(decoded, dict):
            raise RuntimeError(f"{method} {path} returned a non-object JSON document")
        return decoded


def _encoded(value: str) -> str:
    return urllib.parse.quote(value, safe="")


def _required_string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"response is missing {key}: {payload}")
    return value


def _poll(
    description: str,
    load: Callable[[], Any],
    ready: Callable[[Any], bool],
    *,
    timeout_seconds: float,
    interval_seconds: float,
):
    deadline = time.monotonic() + timeout_seconds
    last = None
    while time.monotonic() < deadline:
        last = load()
        if ready(last):
            return last
        time.sleep(interval_seconds)
    raise RuntimeError(f"timed out waiting for {description}: {last}")


def _all_events(client: ApiClient, session_id: str) -> list[dict[str, Any]]:
    after = 0
    events: list[dict[str, Any]] = []
    while True:
        page = client.get(
            f"/agent-sessions/{_encoded(session_id)}/events?after_event_id={after}&limit=100"
        )
        items = page.get("items")
        if not isinstance(items, list):
            raise RuntimeError(f"Agent event page has no items: {page}")
        events.extend(item for item in items if isinstance(item, dict))
        paging = page.get("page")
        if not isinstance(paging, dict) or not paging.get("has_more"):
            return events
        next_after = paging.get("next_after_event_id")
        if isinstance(next_after, bool) or not isinstance(next_after, int) or next_after <= after:
            raise RuntimeError(f"Agent event cursor did not advance: {page}")
        after = next_after


def _followup_turn_ids(client: ApiClient, session_id: str, originating_turn_id: str) -> list[str]:
    turn_ids = {
        event_turn_id
        for event in _all_events(client, session_id)
        if isinstance((event_turn_id := event.get("turn_id")), str)
        and event_turn_id
        and event_turn_id != originating_turn_id
    }
    return sorted(turn_ids)


def _assert_no_followup(
    client: ApiClient,
    *,
    session_id: str,
    originating_turn_id: str,
    task: dict[str, Any],
) -> None:
    followups = _followup_turn_ids(client, session_id, originating_turn_id)
    if followups:
        raise RuntimeError(
            "ready follow-up appeared before terminal Evidence gate: "
            f"gate_state={task.get('gate_state')!r}, followups={followups}"
        )


def _validate_schedule_receipt(
    task: dict[str, Any],
    *,
    owner: str,
    session_id: str,
    originating_turn_id: str,
) -> tuple[str, str]:
    policy = task.get("completion_policy")
    if policy not in _COMPLETION_POLICIES:
        raise RuntimeError(f"AgentTask completion policy is invalid: {task}")
    if task.get("legacy_gate_unverified") is not False:
        raise RuntimeError(f"fresh AgentTask is marked legacy/unverified: {task}")
    receipt = task.get("schedule_receipt")
    if not isinstance(receipt, dict):
        raise RuntimeError(f"AgentTask has no scheduling receipt: {task}")
    task_id = _required_string(task, "task_id")
    linked_run_id = _required_string(task, "linked_run_id")
    expected = {
        "task_id": task_id,
        "owner": owner,
        "session_id": session_id,
        "originating_turn_id": originating_turn_id,
        "run_id": linked_run_id,
        "completion_policy": policy,
    }
    mismatches = {
        key: (receipt.get(key), value)
        for key, value in expected.items()
        if receipt.get(key) != value
    }
    if mismatches:
        raise RuntimeError(f"schedule receipt lineage mismatch: {mismatches}")
    submit_state = receipt.get("submit_state")
    if submit_state not in {
        "admitted", "submitting", "pending", "submitted", "submission_uncertain"
    }:
        raise RuntimeError(f"schedule receipt has invalid non-terminal state: {receipt}")
    # The frozen Phase-1 schedule receipt is immutable and currently records
    # admission state only. A numeric Slurm Job ID is verified from the Run,
    # not manufactured or required here.
    receipt_job_id = receipt.get("slurm_job_id")
    if receipt_job_id is not None and (
        not isinstance(receipt_job_id, str) or not receipt_job_id.isdecimal()
    ):
        raise RuntimeError(f"schedule receipt carries an invalid Slurm Job ID: {receipt}")
    return policy, linked_run_id


def _validate_terminal_gate(
    task: dict[str, Any],
    *,
    policy: str,
    linked_run_id: str,
) -> tuple[list[str], str | None]:
    if task.get("state") != "succeeded" or task.get("gate_state") != "completed":
        raise RuntimeError(f"AgentTask did not reach verified completion: {task}")
    if task.get("legacy_gate_unverified") is not False:
        raise RuntimeError(f"terminal AgentTask is legacy/unverified: {task}")
    receipt = task.get("gate_receipt")
    if not isinstance(receipt, dict):
        raise RuntimeError(f"successful AgentTask has no terminal gate receipt: {task}")
    if receipt.get("task_id") != task.get("task_id") or receipt.get("run_id") != linked_run_id:
        raise RuntimeError(f"terminal gate receipt lineage mismatch: {receipt}")
    if receipt.get("evidence_state") != "finalized":
        raise RuntimeError(f"terminal gate Evidence is not finalized: {receipt}")
    if receipt.get("integrity_state") != "verified":
        raise RuntimeError(f"terminal gate Evidence integrity is not verified: {receipt}")
    integrity_verified_at = receipt.get("integrity_verified_at")
    if not isinstance(integrity_verified_at, str) or not integrity_verified_at:
        raise RuntimeError(f"terminal gate has no integrity timestamp: {receipt}")
    evidence_digest = receipt.get("evidence_digest")
    if not isinstance(evidence_digest, str) or len(evidence_digest) != 64:
        raise RuntimeError(f"terminal gate has invalid Evidence digest: {receipt}")
    evidence_refs = receipt.get("evidence_refs")
    if not isinstance(evidence_refs, list) or not evidence_refs or not all(
        isinstance(item, str) and item for item in evidence_refs
    ):
        raise RuntimeError(f"terminal gate has no Evidence references: {receipt}")
    result = task.get("result")
    if not isinstance(result, dict) or result.get("status") != "succeeded":
        raise RuntimeError(f"terminal AgentTask has no successful result: {task}")
    if result.get("evidence_refs") != evidence_refs:
        raise RuntimeError(
            "legacy result references differ from immutable gate references: "
            f"result={result.get('evidence_refs')}, gate={evidence_refs}"
        )

    capsule_ref = receipt.get("capsule_ref")
    capsule_state = receipt.get("capsule_state")
    if policy == "evidence_and_capsule_required":
        if capsule_state != "READY" or not isinstance(capsule_ref, str) or not capsule_ref:
            raise RuntimeError(f"Capsule-required gate has no verified Capsule: {receipt}")
    else:
        if capsule_state != "not_required" or capsule_ref is not None:
            raise RuntimeError(
                "evidence-only gate incorrectly depends on Capsule completion: "
                f"{receipt}"
            )
    return evidence_refs, capsule_ref if isinstance(capsule_ref, str) else None


def _parse_restart_argv() -> list[str] | None:
    raw = os.environ.get("PILOT107_AGENT_TASK_RESTART_ARGV")
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("PILOT107_AGENT_TASK_RESTART_ARGV must be a JSON argv array") from exc
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(item, str) and item and "\0" not in item for item in value)
    ):
        raise ValueError("PILOT107_AGENT_TASK_RESTART_ARGV must be a non-empty JSON argv array")
    return value


def _restart_and_wait(client: ApiClient, argv: list[str], *, timeout_seconds: float) -> None:
    completed = subprocess.run(argv, check=False, timeout=timeout_seconds)
    if completed.returncode != 0:
        raise RuntimeError(f"restart command failed with exit code {completed.returncode}: {argv}")

    def api_ready() -> bool:
        try:
            client.get("/health/ready")
        except Exception:
            return False
        return True

    _poll(
        "API readiness after restart",
        api_ready,
        bool,
        timeout_seconds=timeout_seconds,
        interval_seconds=1.0,
    )


def run_smoke(
    client: ApiClient,
    *,
    smoke_id: str,
    model_profile_id: str,
    partition: str,
    qos: str,
    timeout_seconds: float,
    poll_interval_seconds: float,
    restart_argv: list[str] | None,
) -> dict[str, Any]:
    if _SAFE_ID.fullmatch(smoke_id) is None:
        raise ValueError("PILOT107_AGENT_TASK_SMOKE_ID is invalid")
    owner = client.owner
    prefix = f"vm-agent-task-{smoke_id}"
    source_ref = f"/public/home/{owner}/pilot107-agent-task-{smoke_id}"

    client.post("/files/mkdir", {"path": source_ref})
    project_view = client.post(
        "/agent-projects",
        {
            "origin": "existing",
            "goal": "Run one bounded VM-local Slurm validation.",
            "request_key": f"{prefix}-project",
            "source_ref": source_ref,
        },
    )
    project = project_view.get("project")
    workspace = project_view.get("workspace")
    if not isinstance(project, dict) or not isinstance(workspace, dict):
        raise RuntimeError(f"Project creation returned an incomplete view: {project_view}")
    project_id = _required_string(project, "project_id")
    workspace_id = _required_string(workspace, "workspace_id")
    snapshot = workspace.get("snapshot")
    if not isinstance(snapshot, dict):
        raise RuntimeError(f"Project Workspace has no snapshot: {workspace}")
    workspace_snapshot_digest = _required_string(snapshot, "digest")

    change_set = client.post(
        f"/agent-workspaces/{_encoded(workspace_id)}/patch",
        {
            "project_id": project_id,
            "path": "main.py",
            "expected_source_digest": None,
            "operation": "create",
            "content": "print('agent-task-workspace-ok')\n",
        },
    )
    change_set_id = _required_string(change_set, "change_set_id")
    sandbox = client.post(
        f"/agent-workspaces/{_encoded(workspace_id)}/sandbox",
        {
            "project_id": project_id,
            "change_set_id": change_set_id,
            "argv": ["python", "-m", "py_compile", "main.py"],
            "timeout": 10,
        },
    )
    if sandbox.get("status") != "succeeded":
        raise RuntimeError(f"Workspace Sandbox did not succeed: {sandbox}")

    persisted_project = client.get(f"/agent-projects/{_encoded(project_id)}")
    persisted_changes = persisted_project.get("change_sets")
    if not isinstance(persisted_changes, list) or not any(
        isinstance(item, dict)
        and item.get("change_set_id") == change_set_id
        and item.get("state") == "reviewable"
        for item in persisted_changes
    ):
        raise RuntimeError(f"Sandbox did not make ChangeSet reviewable: {persisted_project}")

    expires_at = (datetime.now(UTC) + timedelta(minutes=15)).isoformat().replace(
        "+00:00", "Z"
    )
    session = client.post(
        "/agent-sessions",
        {
            "request_key": f"{prefix}-session",
            "profile_id": "experiment_builder",
            "model_profile_id": model_profile_id,
            "source": {
                "project_id": project_id,
                "workspace_id": workspace_id,
                "resource_envelope": {
                    "partition": partition,
                    "qos": qos,
                    "cpus": 1,
                    "memory_mib": 512,
                    "gpu_type": None,
                    "gpus": 0,
                    "walltime_seconds": 300,
                    "max_tasks": 1,
                    "max_submissions": 1,
                    "workspace_snapshot_digest": workspace_snapshot_digest,
                    "expires_at": expires_at,
                    "approved_by": owner,
                },
            },
        },
    )
    session_id = _required_string(session, "session_id")
    if session.get("model_profile_id") != model_profile_id:
        raise RuntimeError(f"Agent Session model profile changed unexpectedly: {session}")
    expected_model = os.environ.get("PILOT107_AGENT_TASK_EXPECTED_MODEL_PROFILE")
    if expected_model and session.get("model_profile_id") != expected_model:
        raise RuntimeError(
            f"live acceptance requires model profile {expected_model!r}, got "
            f"{session.get('model_profile_id')!r}"
        )
    state_version = session.get("state_version")
    if isinstance(state_version, bool) or not isinstance(state_version, int):
        raise RuntimeError(f"Agent Session has no state_version: {session}")

    validation_request_key = f"{prefix}-validation"
    validation_script = (
        "#!/bin/bash\n"
        "set -euo pipefail\n"
        "python3 -m py_compile main.py\n"
        "python3 main.py\n"
        "printf 'agent-task-slurm-ok\\n'\n"
    )
    prompt = (
        "Call validation_schedule exactly once and then stop. Do not call any other tool. "
        "The platform has already bound the Project and Workspace; do not provide their IDs. "
        "Use exactly these arguments: "
        f"request_key={validation_request_key}; cpus=1; memory_mib=512; gpus=0; "
        "walltime_seconds=300; tasks=1; submissions=1; "
        f"job_name={prefix}; script={json.dumps(validation_script)}."
    )
    turn = client.post(
        f"/agent-sessions/{_encoded(session_id)}/turns",
        {
            "request_key": f"{prefix}-turn",
            "message": prompt,
            "expected_state_version": state_version,
        },
    )
    turn_id = _required_string(turn, "turn_id")

    def load_only_task() -> dict[str, Any] | None:
        payload = client.get(f"/agent-sessions/{_encoded(session_id)}/tasks")
        items = payload.get("items")
        if not isinstance(items, list):
            raise RuntimeError(f"AgentTask list has no items: {payload}")
        candidates = [item for item in items if isinstance(item, dict)]
        if not candidates:
            return None
        if len(candidates) != 1:
            raise RuntimeError(f"expected exactly one AgentTask: {payload}")
        return candidates[0]

    task = _poll(
        "AgentTask creation",
        load_only_task,
        lambda value: isinstance(value, dict),
        timeout_seconds=timeout_seconds,
        interval_seconds=poll_interval_seconds,
    )
    assert isinstance(task, dict)
    task_id = _required_string(task, "task_id")

    def load_scheduled_task() -> dict[str, Any]:
        current = client.get(f"/agent-tasks/{_encoded(task_id)}")
        if current.get("gate_state") != "completed":
            _assert_no_followup(
                client,
                session_id=session_id,
                originating_turn_id=turn_id,
                task=current,
            )
        return current

    task = _poll(
        "non-terminal scheduling receipt",
        load_scheduled_task,
        lambda value: isinstance(value.get("schedule_receipt"), dict),
        timeout_seconds=timeout_seconds,
        interval_seconds=poll_interval_seconds,
    )
    policy, linked_run_id = _validate_schedule_receipt(
        task,
        owner=owner,
        session_id=session_id,
        originating_turn_id=turn_id,
    )

    def load_gated_task() -> dict[str, Any]:
        current = client.get(f"/agent-tasks/{_encoded(task_id)}")
        if current.get("gate_state") != "completed":
            _assert_no_followup(
                client,
                session_id=session_id,
                originating_turn_id=turn_id,
                task=current,
            )
        return current

    task = _poll(
        "terminal AgentTask Evidence gate",
        load_gated_task,
        lambda value: (
            value.get("gate_state") == "completed"
            or value.get("state") in _TERMINAL_TASK_STATES
        ),
        timeout_seconds=timeout_seconds,
        interval_seconds=poll_interval_seconds,
    )
    evidence_refs, capsule_ref = _validate_terminal_gate(
        task,
        policy=policy,
        linked_run_id=linked_run_id,
    )

    run = client.get(f"/runs/{_encoded(linked_run_id)}")
    if run.get("state") != "SUCCEEDED" or run.get("collection_state") != "succeeded":
        raise RuntimeError(f"linked validation Run is not authoritatively complete: {run}")
    job_id = _required_string(run, "job_id")
    if not job_id.isdecimal():
        raise RuntimeError(f"linked validation Run has a non-Slurm Job ID: {run}")
    backend = run.get("backend")
    if not isinstance(backend, dict) or backend.get("kind") in {None, "demo"}:
        raise RuntimeError(f"linked validation Run did not use a scheduler backend: {run}")
    schedule_receipt = task.get("schedule_receipt")
    if isinstance(schedule_receipt, dict) and schedule_receipt.get("slurm_job_id") not in {None, job_id}:
        raise RuntimeError(
            "schedule receipt and authoritative Run disagree on Slurm Job ID: "
            f"receipt={schedule_receipt.get('slurm_job_id')!r}, run={job_id!r}"
        )

    evidence = client.get(f"/runs/{_encoded(linked_run_id)}/evidence")
    objects = evidence.get("objects")
    if (
        evidence.get("collection_state") != "succeeded"
        or not isinstance(objects, list)
        or not objects
    ):
        raise RuntimeError(f"linked validation Run has incomplete Evidence: {evidence}")

    capsule_state = run.get("capsule_state")
    capsule_id: str | None = None
    if policy == "evidence_and_capsule_required":
        capsule = client.get(f"/runs/{_encoded(linked_run_id)}/capsule")
        capsule_payload = capsule.get("capsule")
        if capsule.get("capsule_state") != "ready" or not isinstance(capsule_payload, dict):
            raise RuntimeError(f"Capsule-required validation has no ready Capsule: {capsule}")
        capsule_id = _required_string(capsule_payload, "capsule_id")
        if capsule_ref is None:
            raise RuntimeError("terminal gate lost its required Capsule reference")

    def load_followup_turn_ids() -> list[str]:
        return _followup_turn_ids(client, session_id, turn_id)

    followup_turn_ids = _poll(
        "exactly one ready follow-up Turn",
        load_followup_turn_ids,
        lambda values: len(values) >= 1,
        timeout_seconds=timeout_seconds,
        interval_seconds=poll_interval_seconds,
    )
    if len(followup_turn_ids) != 1:
        raise RuntimeError(f"expected exactly one follow-up Turn: {followup_turn_ids}")
    followup_turn_id = followup_turn_ids[0]

    restart_verified = False
    if restart_argv is not None:
        _restart_and_wait(client, restart_argv, timeout_seconds=timeout_seconds)
        # Allow recovered outboxes/workers to replay. Exactly-once identity must
        # remain stable after the restart window.
        settle_seconds = min(10.0, max(2.0, poll_interval_seconds * 3))
        time.sleep(settle_seconds)
        after_restart = _followup_turn_ids(client, session_id, turn_id)
        if after_restart != [followup_turn_id]:
            raise RuntimeError(
                "restart/replay created duplicate or changed follow-up Turn: "
                f"before={[followup_turn_id]}, after={after_restart}"
            )
        reloaded_task = client.get(f"/agent-tasks/{_encoded(task_id)}")
        reloaded_refs, reloaded_capsule_ref = _validate_terminal_gate(
            reloaded_task,
            policy=policy,
            linked_run_id=linked_run_id,
        )
        if reloaded_refs != evidence_refs or reloaded_capsule_ref != capsule_ref:
            raise RuntimeError("restart changed immutable AgentTask gate facts")
        restart_verified = True

    followup_events = [
        event
        for event in _all_events(client, session_id)
        if event.get("turn_id") == followup_turn_id
    ]
    if not followup_events:
        raise RuntimeError("follow-up Turn has no durable events")

    evidence_object_ids = [
        item.get("object_id")
        for item in objects
        if isinstance(item, dict) and isinstance(item.get("object_id"), str)
    ]
    return {
        "status": "ok",
        "acceptance_scope": (
            "live_with_restart" if restart_verified else "live_without_restart"
        ),
        "restart_verified": restart_verified,
        "model_profile_id": model_profile_id,
        "source_ref": source_ref,
        "project_id": project_id,
        "workspace_id": workspace_id,
        "change_set_id": change_set_id,
        "sandbox_succeeded": True,
        "sandbox_result_id": _required_string(sandbox, "result_id"),
        "session_id": session_id,
        "turn_id": turn_id,
        "task_id": task_id,
        "completion_policy": policy,
        "gate_state": task.get("gate_state"),
        "linked_run_id": linked_run_id,
        "job_id": job_id,
        "run_state": run.get("state"),
        "evidence_refs": evidence_refs,
        "evidence_object_ids": evidence_object_ids,
        "capsule_state": capsule_state,
        "capsule_id": capsule_id,
        "capsule_ref": capsule_ref,
        "followup_turn_id": followup_turn_id,
        "followup_event_count": len(followup_events),
    }


def main() -> int:
    base_url = os.environ.get("PILOT107_COMPETITION_BASE_URL", "").rstrip("/")
    if not base_url:
        print(json.dumps({"status": "error", "error": "PILOT107_COMPETITION_BASE_URL is required"}))
        return 2
    owner = os.environ.get("PILOT107_AGENT_TASK_OWNER", "alice")
    smoke_id = os.environ.get(
        "PILOT107_AGENT_TASK_SMOKE_ID", f"{int(time.time())}-{os.getpid()}"
    )
    try:
        report = run_smoke(
            ApiClient(base_url, owner=owner),
            smoke_id=smoke_id,
            model_profile_id=os.environ.get(
                "PILOT107_AGENT_TASK_MODEL_PROFILE", "campus-default"
            ),
            partition=os.environ.get("PILOT107_SMOKE_PARTITION", "CPU-RC"),
            qos=os.environ.get("PILOT107_SMOKE_QOS", "qos_cpu_rc"),
            timeout_seconds=float(
                os.environ.get("PILOT107_AGENT_TASK_TIMEOUT_SECONDS", "360")
            ),
            poll_interval_seconds=float(
                os.environ.get("PILOT107_AGENT_TASK_POLL_SECONDS", "1")
            ),
            restart_argv=_parse_restart_argv(),
        )
    except Exception as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(report, sort_keys=True, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
