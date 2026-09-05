#!/usr/bin/env python3
"""HTTP acceptance for Project Sandbox -> AgentTask -> VM-local Slurm."""

from __future__ import annotations

import json
import os
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta
from typing import Any

_TERMINAL_RUN_STATES = {"SUCCEEDED", "FAILED", "CANCELLED", "TIMEOUT", "OOM"}
_TERMINAL_TASK_STATES = {"succeeded", "failed", "cancelled", "auth_required"}
_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


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
    load,
    ready,
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


def run_smoke(
    client: ApiClient,
    *,
    smoke_id: str,
    model_profile_id: str,
    partition: str,
    qos: str,
    timeout_seconds: float,
    poll_interval_seconds: float,
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
    sandbox_succeeded = sandbox.get("status") == "succeeded"
    if not sandbox_succeeded:
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

    def load_task() -> dict[str, Any] | None:
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
        load_task,
        lambda value: isinstance(value, dict),
        timeout_seconds=timeout_seconds,
        interval_seconds=poll_interval_seconds,
    )
    assert isinstance(task, dict)
    task_id = _required_string(task, "task_id")

    def load_terminal_task() -> dict[str, Any]:
        return client.get(f"/agent-tasks/{_encoded(task_id)}")

    task = _poll(
        "AgentTask completion",
        load_terminal_task,
        lambda value: value.get("state") in _TERMINAL_TASK_STATES,
        timeout_seconds=timeout_seconds,
        interval_seconds=poll_interval_seconds,
    )
    if task.get("state") != "succeeded":
        raise RuntimeError(f"AgentTask did not succeed: {task}")
    linked_run_id = _required_string(task, "linked_run_id")
    result = task.get("result")
    if not isinstance(result, dict):
        raise RuntimeError(f"AgentTask has no terminal result: {task}")
    evidence_refs = result.get("evidence_refs")
    if not isinstance(evidence_refs, list) or not evidence_refs or not all(
        isinstance(item, str) and item for item in evidence_refs
    ):
        raise RuntimeError(f"AgentTask result has no Evidence references: {task}")

    def load_run() -> dict[str, Any]:
        return client.get(f"/runs/{_encoded(linked_run_id)}")

    run = _poll(
        "Slurm Run, Evidence, and Capsule completion",
        load_run,
        lambda value: (
            value.get("state") in _TERMINAL_RUN_STATES
            and value.get("collection_state") == "succeeded"
            and value.get("capsule_state") == "ready"
        ),
        timeout_seconds=timeout_seconds,
        interval_seconds=poll_interval_seconds,
    )
    if run.get("state") != "SUCCEEDED":
        raise RuntimeError(f"linked validation Run did not succeed: {run}")
    job_id = _required_string(run, "job_id")
    if not job_id.isdecimal():
        raise RuntimeError(f"linked validation Run has a non-Slurm job ID: {run}")
    backend = run.get("backend")
    if not isinstance(backend, dict) or backend.get("kind") in {None, "demo"}:
        raise RuntimeError(f"linked validation Run did not use a scheduler backend: {run}")

    evidence = client.get(f"/runs/{_encoded(linked_run_id)}/evidence")
    objects = evidence.get("objects")
    if (
        evidence.get("collection_state") != "succeeded"
        or not isinstance(objects, list)
        or not objects
    ):
        raise RuntimeError(f"linked validation Run has incomplete Evidence: {evidence}")
    capsule = client.get(f"/runs/{_encoded(linked_run_id)}/capsule")
    capsule_state = capsule.get("capsule_state")
    capsule_payload = capsule.get("capsule")
    if capsule_state != "ready" or not isinstance(capsule_payload, dict):
        raise RuntimeError(f"linked validation Run has no ready Capsule: {capsule}")
    capsule_id = _required_string(capsule_payload, "capsule_id")

    def load_followup() -> dict[str, Any] | None:
        for event in _all_events(client, session_id):
            event_turn_id = event.get("turn_id")
            if isinstance(event_turn_id, str) and event_turn_id and event_turn_id != turn_id:
                return event
        return None

    followup = _poll(
        "ready-outbox follow-up Turn event",
        load_followup,
        lambda value: isinstance(value, dict),
        timeout_seconds=timeout_seconds,
        interval_seconds=poll_interval_seconds,
    )
    assert isinstance(followup, dict)
    followup_turn_id = _required_string(followup, "turn_id")

    evidence_object_ids = [
        item.get("object_id")
        for item in objects
        if isinstance(item, dict) and isinstance(item.get("object_id"), str)
    ]
    return {
        "status": "ok",
        "source_ref": source_ref,
        "project_id": project_id,
        "workspace_id": workspace_id,
        "change_set_id": change_set_id,
        "sandbox_succeeded": sandbox_succeeded,
        "sandbox_result_id": _required_string(sandbox, "result_id"),
        "session_id": session_id,
        "turn_id": turn_id,
        "task_id": task_id,
        "linked_run_id": linked_run_id,
        "job_id": job_id,
        "run_state": run["state"],
        "evidence_refs": evidence_refs,
        "evidence_object_ids": evidence_object_ids,
        "capsule_state": capsule_state,
        "capsule_id": capsule_id,
        "followup_turn_id": followup_turn_id,
        "followup_event_id": followup.get("event_id"),
        "followup_event_type": followup.get("event_type"),
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
        )
    except Exception as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(report, sort_keys=True, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
