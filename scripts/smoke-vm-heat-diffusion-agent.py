#!/usr/bin/env python3
"""Model-driven 2D heat-diffusion acceptance through the public HTTP API."""

from __future__ import annotations

import base64
import json
import os
import re
import ssl
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pilot107.scientific.heat_diffusion_validation import audit_heat_diffusion_outputs

_TERMINAL_RUN_STATES = {"SUCCEEDED", "FAILED", "CANCELLED", "TIMEOUT", "OOM"}
_TERMINAL_TASK_STATES = {"succeeded", "failed", "cancelled", "auth_required"}
_TERMINAL_TURN_EVENTS = {"turn_completed", "turn_failed"}
_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REQUIRED_OUTPUTS = (
    "raw-results.csv",
    "convergence.json",
    "scaling.json",
    "report.md",
    "convergence.svg",
    "scaling.svg",
)
_PROJECT_GOAL = (
    "Build a reproducible CPU experiment for the 2D heat equation on [0,1]^2. "
    "Use a C/OpenMP explicit five-point solver and Python-standard-library analysis. "
    "Verify the analytic sine-mode solution on grids 64, 128, and 256, report "
    "observed order in [1.8, 2.2], and measure 1/2/4-thread scaling in one four-CPU "
    "Slurm job via distinct srun -c 1, srun -c 2, and srun -c 4 steps. Produce "
    "Echo each exact srun command before executing it so stdout Evidence proves the steps. "
    "raw-results.csv, convergence.json, scaling.json, report.md, convergence.svg, "
    "and scaling.svg with accessible labels plus platform snapshot and Run provenance. "
    "Use scripts/validate_project.py for network-free, Python-standard-library static "
    "sandbox validation; it must not require a C compiler or run the full experiment. "
    "Compile and execute the C/OpenMP solver only inside the Slurm validation. Use "
    "scripts/run_experiment.sh as the single Slurm entrypoint."
)


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
            with urllib.request.urlopen(request, timeout=60, context=self.context) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"{method} {path} failed with HTTP {exc.code}: {detail}") from None
        except urllib.error.URLError as exc:
            raise RuntimeError(f"{method} {path} transport failed: {exc.reason}") from None
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"{method} {path} returned invalid JSON: {exc}") from None
        if not isinstance(value, dict):
            raise RuntimeError(f"{method} {path} returned a non-object JSON document")
        return value


def _encoded(value: str) -> str:
    return urllib.parse.quote(value, safe="")


def _required_string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"response is missing {key}: {payload}")
    return value


def _snapshot_identity(payload: object) -> tuple[str, str, str]:
    current = payload
    for _ in range(4):
        if not isinstance(current, dict):
            break
        authority_id = current.get("authority_id")
        snapshot_id = current.get("snapshot_id")
        content_sha256 = current.get("content_sha256")
        if isinstance(authority_id, str) and isinstance(snapshot_id, str):
            if (
                not authority_id
                or not snapshot_id
                or not isinstance(content_sha256, str)
                or _SHA256.fullmatch(content_sha256) is None
            ):
                raise RuntimeError("platform snapshot identity is incomplete")
            return authority_id, snapshot_id, content_sha256
        nested = current.get("latest_snapshot")
        if not isinstance(nested, dict):
            nested = current.get("result")
        current = nested
    raise RuntimeError("platform response omitted its authoritative snapshot identity")


def _poll(
    description: str,
    load: Callable[[], Any],
    ready: Callable[[Any], bool],
    *,
    timeout_seconds: float,
    interval_seconds: float,
) -> Any:
    deadline = time.monotonic() + timeout_seconds
    last: Any = None
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


def _poll_turn(
    client: ApiClient,
    session_id: str,
    turn_id: str,
    *,
    expected_model: str,
    timeout_seconds: float,
    interval_seconds: float,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    events = _poll(
        "Agent Turn completion",
        lambda: _all_events(client, session_id),
        lambda items: any(
            event.get("turn_id") == turn_id
            and event.get("event_type") in _TERMINAL_TURN_EVENTS
            for event in items
        ),
        timeout_seconds=timeout_seconds,
        interval_seconds=interval_seconds,
    )
    terminal = next(
        event
        for event in reversed(events)
        if event.get("turn_id") == turn_id
        and event.get("event_type") in _TERMINAL_TURN_EVENTS
    )
    payload = terminal.get("payload")
    if terminal.get("event_type") != "turn_completed" or not isinstance(payload, dict):
        detail = payload.get("error") if isinstance(payload, dict) else payload
        raise RuntimeError(f"Agent Turn failed: {detail}")
    if payload.get("model") != expected_model:
        raise RuntimeError(
            f"Agent used model {payload.get('model')!r}, expected {expected_model!r}"
        )
    provider = payload.get("provider")
    if not isinstance(provider, str) or not provider:
        raise RuntimeError("turn_completed omitted the actual provider")
    result = payload.get("result")
    if isinstance(result, str) and not result.strip():
        raise RuntimeError("Agent returned a whitespace-only completion")
    if isinstance(result, dict) and isinstance(result.get("text"), str):
        validation_scheduled = any(
            event.get("event_type") == "tool_call_completed"
            and isinstance(event.get("payload"), dict)
            and event["payload"].get("tool_name") == "validation_schedule"
            and event["payload"].get("is_error") is False
            for event in events
        )
        if not result["text"].strip() and not validation_scheduled:
            raise RuntimeError("Agent returned a whitespace-only completion")
    encoded_events = json.dumps(events, ensure_ascii=False)
    for code in ("provider_timeout", "tool_step_budget_exhausted"):
        if code in encoded_events:
            raise RuntimeError(f"Agent Turn contained terminal failure code {code}")
    return payload, events


def _task_for_session(client: ApiClient, session_id: str) -> dict[str, Any] | None:
    payload = client.get(f"/agent-sessions/{_encoded(session_id)}/tasks")
    items = payload.get("items")
    if not isinstance(items, list):
        raise RuntimeError(f"AgentTask list has no items: {payload}")
    tasks = [item for item in items if isinstance(item, dict)]
    if len(tasks) > 1:
        raise RuntimeError(f"expected at most one AgentTask: {payload}")
    return tasks[0] if tasks else None


def _created_files_from_diff(diff: str) -> dict[str, str]:
    files: dict[str, list[str]] = {}
    current: str | None = None
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            current = line[6:]
            files.setdefault(current, [])
        elif current is not None and line.startswith("+") and not line.startswith("+++"):
            files[current].append(line[1:])
    return {name: "\n".join(lines) + "\n" for name, lines in files.items()}


def _validate_generated_sources(files: dict[str, str]) -> None:
    required = {
        "src/heat2d.c",
        "scripts/summarize.py",
        "scripts/run_experiment.sh",
        "scripts/validate_project.py",
    }
    if not required.issubset(files):
        missing = sorted(required - files.keys())
        raise RuntimeError(f"Agent ChangeSet is missing generated files: {missing}")
    c_source = files["src/heat2d.c"]
    if ("#" + "pragma omp") not in c_source:
        raise RuntimeError("generated C solver does not contain an OpenMP pragma")
    forbidden_c = ("system(", "popen(", "fork(", "socket(", "curl ", "wget ")
    if any(token in c_source for token in forbidden_c):
        raise RuntimeError("generated C solver contains a network or process-spawn API")
    python_source = files["scripts/summarize.py"]
    forbidden_python = ("requests", "urllib", "socket", "subprocess", "os.system")
    if any(token in python_source for token in forbidden_python):
        raise RuntimeError("generated summarizer is not Python-standard-library/offline safe")
    run_script = files["scripts/run_experiment.sh"]
    for command in ("srun -c 1", "srun -c 2", "srun -c 4"):
        if command not in run_script:
            raise RuntimeError(f"generated Slurm workflow is missing {command}")


def _download_outputs(
    client: ApiClient,
    *,
    workdir: str,
    destination: Path,
) -> None:
    for name in _REQUIRED_OUTPUTS:
        path = f"{workdir.rstrip('/')}/{name}"
        query = urllib.parse.urlencode({"path": path, "offset": 0, "length": 4 * 1024 * 1024})
        payload = client.get(f"/files/content?{query}")
        encoded = payload.get("data_b64")
        size = payload.get("size")
        if not isinstance(encoded, str) or isinstance(size, bool) or not isinstance(size, int):
            raise RuntimeError(f"file API returned invalid content for {name}: {payload}")
        content = base64.b64decode(encoded, validate=True)
        if len(content) != size:
            raise RuntimeError(f"scientific output {name} was truncated")
        (destination / name).write_bytes(content)


def _evidence_preview_text(
    client: ApiClient,
    *,
    run_id: str,
    evidence: dict[str, Any],
    logical_path: str,
) -> str:
    objects = evidence.get("objects")
    if not isinstance(objects, list):
        raise RuntimeError("Evidence response omitted its objects")
    selected = next(
        (
            item
            for item in objects
            if isinstance(item, dict) and item.get("logical_path") == logical_path
        ),
        None,
    )
    if not isinstance(selected, dict):
        raise RuntimeError(f"Evidence omitted {logical_path}")
    object_id = _required_string(selected, "object_id")
    payload = client.get(
        f"/runs/{_encoded(run_id)}/evidence/objects/{_encoded(object_id)}"
    )
    preview = payload.get("preview")
    if not isinstance(preview, dict) or preview.get("available") is not True:
        raise RuntimeError(f"Evidence preview is unavailable for {logical_path}")
    content = preview.get("content")
    if not isinstance(content, str):
        raise RuntimeError(f"Evidence preview has no content for {logical_path}")
    return content


def _formal_contract(
    *,
    goal: str,
    workdir: str,
    command: str,
    partition: str,
    qos: str,
) -> dict[str, Any]:
    return {
        "recipe_version_id": "recipe_python_cpu@1.0.0",
        "project": {"name": goal[:128], "workdir": workdir},
        "entry": {"command": command, "expected_outputs": list(_REQUIRED_OUTPUTS)},
        "resources": {
            "partition": partition,
            "qos": qos,
            "nodes": 1,
            "ntasks": 1,
            "cpus_per_task": 4,
            "memory": "4096M",
            "gpus_total": 0,
            "gpu_type": None,
            "time_limit": "00:10:00",
        },
    }


def run_smoke(
    client: ApiClient,
    *,
    smoke_id: str,
    model_profile_id: str,
    expected_model: str,
    partition: str,
    qos: str,
    auto_approve: bool,
    timeout_seconds: float,
    poll_interval_seconds: float,
) -> dict[str, Any]:
    if _SAFE_ID.fullmatch(smoke_id) is None:
        raise ValueError("PILOT107_HEAT_SMOKE_ID is invalid")
    if not auto_approve:
        raise RuntimeError("PILOT107_HEAT_SMOKE_AUTO_APPROVE=1 is required")
    owner = client.owner
    prefix = f"vm-heat-{smoke_id}"
    target_root = f"/public/home/{owner}/pilot107-heat-{smoke_id}"

    platform = client.get("/platform/snapshots/latest?scope=login_node")
    authority_id, platform_snapshot_id, _ = _snapshot_identity(platform)
    if authority_id != "vm-slurm":
        raise RuntimeError(f"unexpected platform authority: {authority_id}")

    view = client.post(
        "/agent-projects",
        {
            "origin": "blank",
            "goal": _PROJECT_GOAL,
            "request_key": f"{prefix}-project",
        },
    )
    project = view.get("project")
    workspace = view.get("workspace")
    if not isinstance(project, dict) or not isinstance(workspace, dict):
        raise RuntimeError(f"Project creation returned an incomplete view: {view}")
    project_id = _required_string(project, "project_id")
    workspace_id = _required_string(workspace, "workspace_id")
    snapshot = workspace.get("snapshot")
    if not isinstance(snapshot, dict):
        raise RuntimeError("Project Workspace omitted its snapshot")
    workspace_snapshot_digest = _required_string(snapshot, "digest")

    expires_at = (datetime.now(UTC) + timedelta(minutes=30)).isoformat().replace(
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
                    "cpus": 4,
                    "memory_mib": 4096,
                    "gpu_type": None,
                    "gpus": 0,
                    "walltime_seconds": 600,
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
        raise RuntimeError("Agent Session omitted state_version")
    turn = client.post(
        f"/agent-sessions/{_encoded(session_id)}/turns",
        {
            "request_key": f"{prefix}-turn",
            "message": (
                "Please review the bound Project and complete its approved validation workflow. "
                "Preserve scientific provenance and stop after scheduling the durable validation."
            ),
            "expected_state_version": state_version,
        },
    )
    turn_id = _required_string(turn, "turn_id")
    terminal, events = _poll_turn(
        client,
        session_id,
        turn_id,
        expected_model=expected_model,
        timeout_seconds=timeout_seconds,
        interval_seconds=poll_interval_seconds,
    )

    def load_ready_project() -> dict[str, Any]:
        return client.get(f"/agent-projects/{_encoded(project_id)}")

    ready_view = _poll(
        "Blueprint, ChangeSet, and Sandbox validation",
        load_ready_project,
        lambda payload: (
            isinstance(payload.get("project"), dict)
            and isinstance(payload["project"].get("blueprint"), dict)
            and isinstance(payload.get("change_sets"), list)
            and any(
                isinstance(item, dict)
                and item.get("state") in {"reviewable", "approved", "published"}
                and isinstance(item.get("sandbox_results"), list)
                and any(
                    isinstance(result, dict) and result.get("status") == "succeeded"
                    for result in item["sandbox_results"]
                )
                for item in payload["change_sets"]
            )
        ),
        timeout_seconds=timeout_seconds,
        interval_seconds=poll_interval_seconds,
    )
    blueprint = ready_view["project"]["blueprint"]
    changes = [item for item in ready_view["change_sets"] if isinstance(item, dict)]
    selected = max(changes, key=lambda item: str(item.get("updated_at", "")))
    change_set_id = _required_string(selected, "change_set_id")
    change_set_digest = _required_string(selected, "digest")
    change_set_version = selected.get("version")
    if isinstance(change_set_version, bool) or not isinstance(change_set_version, int):
        raise RuntimeError("ChangeSet omitted its version")
    sandbox_results = selected.get("sandbox_results")
    if not isinstance(sandbox_results, list) or not any(
        isinstance(item, dict)
        and item.get("status") == "succeeded"
        and item.get("argv") == ["python3", "scripts/validate_project.py"]
        for item in sandbox_results
    ):
        raise RuntimeError("Blueprint sandbox validation did not succeed with its declared argv")
    diff_payload = client.get(
        f"/agent-changesets/{_encoded(change_set_id)}/diff?"
        + urllib.parse.urlencode({"project_id": project_id, "workspace_id": workspace_id})
    )
    _validate_generated_sources(
        _created_files_from_diff(_required_string(diff_payload, "unified_diff"))
    )

    task = _poll(
        "AgentTask creation",
        lambda: _task_for_session(client, session_id),
        lambda value: isinstance(value, dict),
        timeout_seconds=timeout_seconds,
        interval_seconds=poll_interval_seconds,
    )
    task_id = _required_string(task, "task_id")
    task = _poll(
        "AgentTask completion",
        lambda: client.get(f"/agent-tasks/{_encoded(task_id)}"),
        lambda value: value.get("state") in _TERMINAL_TASK_STATES,
        timeout_seconds=timeout_seconds,
        interval_seconds=poll_interval_seconds,
    )
    if task.get("state") != "succeeded":
        raise RuntimeError(f"AgentTask did not succeed: {task}")
    validation_run_id = _required_string(task, "linked_run_id")
    task_result = task.get("result")
    if not isinstance(task_result, dict) or not task_result.get("evidence_refs"):
        raise RuntimeError("AgentTask did not retain trusted Evidence")

    validation_run = _poll(
        "validation Run Evidence and Capsule",
        lambda: client.get(f"/runs/{_encoded(validation_run_id)}"),
        lambda value: (
            value.get("state") in _TERMINAL_RUN_STATES
            and value.get("collection_state") == "succeeded"
            and value.get("capsule_state") == "ready"
        ),
        timeout_seconds=timeout_seconds,
        interval_seconds=poll_interval_seconds,
    )
    if validation_run.get("state") != "SUCCEEDED":
        raise RuntimeError(f"validation Run did not succeed: {validation_run}")

    publication = client.post(
        f"/agent-changesets/{_encoded(change_set_id)}/publish",
        {
            "project_id": project_id,
            "workspace_id": workspace_id,
            "expected_version": change_set_version,
            "approved_digest": change_set_digest,
            "target_root": target_root,
        },
    )
    if publication.get("state") != "published":
        raise RuntimeError(f"ChangeSet publication did not succeed: {publication}")
    candidate = client.post(
        f"/agent-changesets/{_encoded(change_set_id)}/formal-run-candidate",
        {
            "project_id": project_id,
            "workspace_id": workspace_id,
            "session_id": session_id,
            "validation_task_id": task_id,
        },
    )
    formal_contract = _formal_contract(
        goal=_PROJECT_GOAL,
        workdir=_required_string(candidate, "published_workdir"),
        command=_required_string(candidate, "default_command"),
        partition=partition,
        qos=qos,
    )
    lineage = {
        "project_id": project_id,
        "workspace_id": workspace_id,
        "session_id": session_id,
        "validation_contract_id": _required_string(candidate, "validation_contract_id"),
        "validation_run_id": _required_string(candidate, "validation_run_id"),
        "validation_evidence_refs": candidate.get("validation_evidence_refs"),
        "formal_contract": formal_contract,
    }
    if not isinstance(lineage["validation_evidence_refs"], list):
        raise RuntimeError("formal candidate omitted validation Evidence")
    approval = client.post(
        f"/agent-changesets/{_encoded(change_set_id)}/formal-preview", lineage
    )
    approved_digest = _required_string(approval, "approval_digest")
    formal = client.post(
        f"/agent-changesets/{_encoded(change_set_id)}/formal-submit",
        {**lineage, "approved_digest": approved_digest},
    )
    formal_run = formal.get("run")
    if not isinstance(formal_run, dict):
        raise RuntimeError("formal submission omitted the Run")
    formal_run_id = _required_string(formal_run, "run_id")
    formal_run = _poll(
        "formal Run Evidence and Capsule",
        lambda: client.get(f"/runs/{_encoded(formal_run_id)}"),
        lambda value: (
            value.get("state") in _TERMINAL_RUN_STATES
            and value.get("collection_state") == "succeeded"
            and value.get("capsule_state") == "ready"
        ),
        timeout_seconds=timeout_seconds,
        interval_seconds=poll_interval_seconds,
    )
    if formal_run.get("state") != "SUCCEEDED":
        raise RuntimeError(f"formal scientific Run did not succeed: {formal_run}")
    backend = formal_run.get("backend")
    if not isinstance(backend, dict) or backend.get("kind") in {None, "demo"}:
        raise RuntimeError("formal scientific Run did not use the VM Slurm backend")
    formal_job_id = _required_string(formal_run, "job_id")
    if not formal_job_id.isdecimal():
        raise RuntimeError("formal Run did not return a real Slurm Job ID")
    evidence = client.get(f"/runs/{_encoded(formal_run_id)}/evidence")
    if evidence.get("collection_state") != "succeeded" or not evidence.get("objects"):
        raise RuntimeError("formal Run Evidence is incomplete")
    stdout_evidence = _evidence_preview_text(
        client,
        run_id=formal_run_id,
        evidence=evidence,
        logical_path="logs/stdout.tail.json",
    )
    for command in ("srun -c 1", "srun -c 2", "srun -c 4"):
        if command not in stdout_evidence:
            raise RuntimeError(f"formal Run Evidence did not prove {command}")
    capsule = client.get(f"/runs/{_encoded(formal_run_id)}/capsule")
    capsule_payload = capsule.get("capsule")
    if capsule.get("capsule_state") != "ready" or not isinstance(capsule_payload, dict):
        raise RuntimeError("formal Run Capsule is not ready")
    capsule_ref = _required_string(capsule_payload, "capsule_id")

    with tempfile.TemporaryDirectory(prefix="pilot107-heat-audit-") as directory:
        output_root = Path(directory)
        _download_outputs(
            client,
            workdir=_required_string(candidate, "published_workdir"),
            destination=output_root,
        )
        audit = audit_heat_diffusion_outputs(output_root)
    if audit.status != "PASS" or audit.observed_order is None:
        raise RuntimeError(f"scientific output audit failed: {audit.checks}")

    event_refs = [
        str(event.get("event_id"))
        for event in events
        if event.get("event_id") is not None
    ]
    return {
        "status": "PASS",
        "model": terminal["model"],
        "provider": terminal["provider"],
        "platform_snapshot_id": platform_snapshot_id,
        "project_id": project_id,
        "change_set_digest": change_set_digest,
        "validation_run_id": validation_run_id,
        "formal_run_id": formal_run_id,
        "formal_job_id": formal_job_id,
        "observed_order": audit.observed_order,
        "threads": list(audit.threads),
        "evidence_refs": task_result["evidence_refs"],
        "capsule_ref": capsule_ref,
        "turn_event_refs": event_refs,
        "blueprint_saved": isinstance(blueprint, dict),
    }


def main() -> int:
    base_url = os.environ.get("PILOT107_COMPETITION_BASE_URL", "").rstrip("/")
    if not base_url:
        print(json.dumps({"status": "FAIL", "error": "PILOT107_PUBLIC_URL is required"}))
        return 2
    owner = os.environ.get("PILOT107_AGENT_OWNER", "alice")
    smoke_id = os.environ.get("PILOT107_HEAT_SMOKE_ID", f"{int(time.time())}-{os.getpid()}")
    try:
        report = run_smoke(
            ApiClient(base_url, owner=owner),
            smoke_id=smoke_id,
            model_profile_id=os.environ.get(
                "PILOT107_AGENT_TASK_MODEL_PROFILE", "campus-default"
            ),
            expected_model=os.environ.get(
                "PILOT107_EXPECTED_AGENT_MODEL", "deepseek-v4-flash"
            ),
            partition=os.environ.get("PILOT107_SMOKE_PARTITION", "CPU-RC"),
            qos=os.environ.get("PILOT107_SMOKE_QOS", "qos_cpu_rc"),
            auto_approve=os.environ.get("PILOT107_HEAT_SMOKE_AUTO_APPROVE") == "1",
            timeout_seconds=float(
                os.environ.get("PILOT107_HEAT_SMOKE_TIMEOUT_SECONDS", "1800")
            ),
            poll_interval_seconds=float(
                os.environ.get("PILOT107_HEAT_SMOKE_POLL_SECONDS", "2")
            ),
        )
    except Exception as exc:
        print(json.dumps({"status": "FAIL", "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(report, sort_keys=True, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
