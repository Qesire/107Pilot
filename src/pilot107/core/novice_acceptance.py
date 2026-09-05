"""Machine-verifiable gate for the Phase 3D novice usability study."""

from __future__ import annotations

import statistics
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

SCHEMA_VERSION = "pilot107.novice-acceptance/v1"
MIN_PARTICIPANTS = 5
MAX_MEDIAN_FIRST_SUCCESS_SECONDS = 600.0
REQUIRED_TASKS = (
    "found_python_cpu_template",
    "adopted_template",
    "modified_workdir_and_command",
    "understood_preflight",
    "submitted_run",
    "located_logs",
    "located_results",
    "located_failure_reason",
)


@dataclass(frozen=True)
class NoviceAcceptanceReport:
    status: str
    study_id: str
    required_participants: int
    recorded_participants: int
    completed_participants: int
    median_first_success_seconds: float | None
    max_median_first_success_seconds: float
    terminal_free: bool
    issues: tuple[str, ...]

    def to_payload(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "study_id": self.study_id,
            "required_participants": self.required_participants,
            "recorded_participants": self.recorded_participants,
            "completed_participants": self.completed_participants,
            "median_first_success_seconds": self.median_first_success_seconds,
            "max_median_first_success_seconds": self.max_median_first_success_seconds,
            "terminal_free": self.terminal_free,
            "issues": list(self.issues),
        }


@dataclass(frozen=True)
class NoviceStudyReadinessReport:
    status: str
    user: str
    identity_mode: str
    template_release_id: str | None
    failure_run_id: str
    issues: tuple[str, ...]

    def to_payload(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "user": self.user,
            "identity_mode": self.identity_mode,
            "template_release_id": self.template_release_id,
            "failure_run_id": self.failure_run_id,
            "issues": list(self.issues),
        }


def evaluate_novice_acceptance(payload: Mapping[str, Any]) -> NoviceAcceptanceReport:
    """Evaluate anonymized study records without treating automation as human evidence."""

    issues: list[str] = []
    if payload.get("schema_version") != SCHEMA_VERSION:
        issues.append(f"schema_version must be {SCHEMA_VERSION}")
    study_id = _required_string(payload, "study_id", issues, prefix="study")
    if payload.get("evidence_source") != "facilitated_human_study":
        issues.append("evidence_source must be facilitated_human_study")
    participants_raw = payload.get("participants")
    if not isinstance(participants_raw, list):
        participants_raw = []
        issues.append("participants must be a list")

    durations: list[float] = []
    completed = 0
    terminal_free = True
    participant_ids: set[str] = set()
    contract_ids: set[str] = set()
    success_run_ids: set[str] = set()
    for index, raw in enumerate(participants_raw):
        prefix = f"participants[{index}]"
        if not isinstance(raw, Mapping):
            issues.append(f"{prefix} must be an object")
            continue
        participant_id = _required_string(raw, "participant_id", issues, prefix=prefix)
        if participant_id in participant_ids:
            issues.append(f"{prefix}.participant_id must be unique")
        participant_ids.add(participant_id)
        if raw.get("slurm_experience") != "none":
            issues.append(f"{prefix}.slurm_experience must be none")
        if raw.get("automated") is not False:
            issues.append(f"{prefix}.automated must be false")
        if raw.get("completed") is not True:
            issues.append(f"{prefix}.completed must be true")
            continue
        completed += 1
        used_terminal = raw.get("used_terminal")
        if used_terminal is not False:
            terminal_free = False
            issues.append(f"{prefix}.used_terminal must be false")
        tasks = raw.get("tasks")
        if not isinstance(tasks, Mapping):
            issues.append(f"{prefix}.tasks must be an object")
        else:
            for task in REQUIRED_TASKS:
                if tasks.get(task) is not True:
                    issues.append(f"{prefix}.tasks.{task} must be true")
        contract_id, success_run_id = _require_traceability(raw, prefix=prefix, issues=issues)
        if contract_id in contract_ids:
            issues.append(f"{prefix}.contract_id must be unique")
        contract_ids.add(contract_id)
        if success_run_id in success_run_ids:
            issues.append(f"{prefix}.success_run_id must be unique")
        success_run_ids.add(success_run_id)
        duration = _duration_seconds(raw, prefix=prefix, issues=issues)
        if duration is not None:
            durations.append(duration)

    recorded = len(participants_raw)
    median_seconds = statistics.median(durations) if durations else None
    structurally_valid = not issues
    if recorded < MIN_PARTICIPANTS and structurally_valid:
        status = "pending"
        issues.append(f"need at least {MIN_PARTICIPANTS} participants; recorded {recorded}")
    elif not structurally_valid:
        status = "failed"
    elif len(durations) != recorded:
        status = "failed"
        issues.append("every participant must have a valid first-success duration")
    elif median_seconds is None or median_seconds > MAX_MEDIAN_FIRST_SUCCESS_SECONDS:
        status = "failed"
        issues.append(
            f"median first-success duration exceeds {MAX_MEDIAN_FIRST_SUCCESS_SECONDS:.0f} seconds"
        )
    else:
        status = "passed"

    return NoviceAcceptanceReport(
        status=status,
        study_id=study_id,
        required_participants=MIN_PARTICIPANTS,
        recorded_participants=recorded,
        completed_participants=completed,
        median_first_success_seconds=median_seconds,
        max_median_first_success_seconds=MAX_MEDIAN_FIRST_SUCCESS_SECONDS,
        terminal_free=terminal_free,
        issues=tuple(issues),
    )


def evaluate_novice_study_readiness(
    *,
    session: Mapping[str, Any],
    templates: Mapping[str, Any],
    failure_run: Mapping[str, Any],
    failure_evidence: Mapping[str, Any],
    failure_diagnoses: Mapping[str, Any],
) -> NoviceStudyReadinessReport:
    """Verify that the live deployment can execute the facilitated task card."""

    issues: list[str] = []
    user = str(session.get("user") or "")
    identity_mode = str(session.get("identity_mode") or "")
    if identity_mode != "fixed_user" or session.get("switchable") is not False:
        issues.append("study deployment must use non-switchable fixed_user identity")
    if not user:
        issues.append("web session must resolve a user")

    eligible_template = next(
        (
            item
            for item in _mapping_items(templates.get("items"))
            if _is_eligible_cpu_template(item)
        ),
        None,
    )
    if eligible_template is None:
        issues.append("no published, gated Python CPU template is available")

    failure_run_id = str(failure_run.get("run_id") or "")
    if not failure_run_id:
        issues.append("failure Run response is missing run_id")
    if failure_run.get("owner") != user:
        issues.append("failure Run owner must match the resolved Web user")
    if failure_run.get("state") != "FAILED":
        issues.append("failure Run must be FAILED")
    if failure_run.get("submit_strategy") != "command" or str(
        failure_run.get("job_id") or ""
    ).startswith("demo-"):
        issues.append("failure Run must come from the real command backend")
    if failure_run.get("collection_state") != "succeeded":
        issues.append("failure Run Evidence collection must be succeeded")
    if failure_run.get("diagnosis_state") != "succeeded":
        issues.append("failure Run diagnosis must be succeeded")

    evidence_paths = {
        str(item.get("logical_path")) for item in _mapping_items(failure_evidence.get("objects"))
    }
    if not {"logs/stdout.tail.json", "logs/stderr.tail.json"} & evidence_paths:
        issues.append("failure Run must expose a bounded log Evidence object")
    for required_path in (
        "derived/result_summary.v1.json",
        "slurm/accounting.json",
    ):
        if required_path not in evidence_paths:
            issues.append(f"failure Run Evidence is missing {required_path}")

    diagnoses = _mapping_items(failure_diagnoses.get("items"))
    if not diagnoses:
        issues.append("failure Run must expose at least one deterministic diagnosis")
    elif not all(item.get("evidence_refs") for item in diagnoses):
        issues.append("every failure diagnosis must cite Evidence")

    return NoviceStudyReadinessReport(
        status="ready" if not issues else "not_ready",
        user=user,
        identity_mode=identity_mode,
        template_release_id=(
            None if eligible_template is None else str(eligible_template.get("release_id"))
        ),
        failure_run_id=failure_run_id,
        issues=tuple(issues),
    )


def _mapping_items(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _is_eligible_cpu_template(item: Mapping[str, Any]) -> bool:
    payload = item.get("payload")
    compatibility = item.get("compatibility")
    gate_report = item.get("gate_report")
    return (
        item.get("withdrawn_at") is None
        and isinstance(payload, Mapping)
        and payload.get("recipe_version_id") == "recipe_python_cpu@1.0.0"
        and isinstance(compatibility, Mapping)
        and compatibility.get("gpu") is False
        and isinstance(gate_report, Mapping)
        and gate_report.get("status") == "OK"
        and bool(item.get("release_id"))
    )


def _required_string(
    payload: Mapping[str, Any],
    field: str,
    issues: list[str],
    *,
    prefix: str,
) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        issues.append(f"{prefix}.{field} must be a non-empty string")
        return ""
    return value.strip()


def _duration_seconds(
    payload: Mapping[str, Any],
    *,
    prefix: str,
    issues: list[str],
) -> float | None:
    started = _timestamp(payload.get("started_at"), f"{prefix}.started_at", issues)
    succeeded = _timestamp(payload.get("first_success_at"), f"{prefix}.first_success_at", issues)
    if started is None or succeeded is None:
        return None
    duration = (succeeded - started).total_seconds()
    if duration < 0:
        issues.append(f"{prefix}.first_success_at must not precede started_at")
        return None
    return duration


def _timestamp(value: Any, field: str, issues: list[str]) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        issues.append(f"{field} must be an RFC3339 timestamp")
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        issues.append(f"{field} must be an RFC3339 timestamp")
        return None
    if parsed.tzinfo is None:
        issues.append(f"{field} must include a timezone")
        return None
    return parsed


def _require_traceability(
    payload: Mapping[str, Any],
    *,
    prefix: str,
    issues: list[str],
) -> tuple[str, str]:
    contract_id = _required_string(payload, "contract_id", issues, prefix=prefix)
    success_run_id = _required_string(payload, "success_run_id", issues, prefix=prefix)
    failure_run_id = _required_string(payload, "failure_run_id", issues, prefix=prefix)
    if success_run_id and success_run_id == failure_run_id:
        issues.append(f"{prefix}.success_run_id must differ from failure_run_id")
    evidence_refs = payload.get("evidence_refs")
    if not isinstance(evidence_refs, list) or not all(
        isinstance(item, str) and item.startswith("evidence://runs/") for item in evidence_refs
    ):
        issues.append(f"{prefix}.evidence_refs must contain Evidence URIs")
        return contract_id, success_run_id
    success_prefix = f"evidence://runs/{success_run_id}/"
    failure_prefix = f"evidence://runs/{failure_run_id}/"
    if not any(item.startswith(success_prefix) and "/logs/" in item for item in evidence_refs):
        issues.append(f"{prefix}.evidence_refs must include a success Run log object")
    if not any(item.startswith(success_prefix) and "/outputs/" in item for item in evidence_refs):
        issues.append(f"{prefix}.evidence_refs must include a success Run output object")
    if not any(item.startswith(failure_prefix) and "/logs/" in item for item in evidence_refs):
        issues.append(f"{prefix}.evidence_refs must include a failure Run log object")
    return contract_id, success_run_id
