"""Durable state for the phase-aware Experiment Builder workflow."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

_IDENTIFIER = re.compile(r"^[A-Za-z0-9._:-]{1,256}$")
_DIGEST = re.compile(r"^[a-f0-9]{64}$")


class BuilderSubmissionConflict(RuntimeError):
    """A Builder request replay or optimistic transition conflicted."""


class BuilderPhase(StrEnum):
    DRAFTING = "drafting"
    SANDBOX_FAILED = "sandbox_failed"
    VALIDATION_SCHEDULED = "validation_scheduled"


class BuilderSubmissionState(StrEnum):
    RUNNING = "running"
    SANDBOX_FAILED = "sandbox_failed"
    SCHEDULED = "scheduled"


@dataclass(frozen=True)
class BuilderSubmissionRecord:
    submission_id: str
    owner: str
    session_id: str
    turn_id: str
    project_id: str
    workspace_id: str
    request_key: str
    input_digest: str
    phase: BuilderPhase
    state: BuilderSubmissionState
    version: int
    base_change_set_id: str | None
    change_set_id: str | None
    sandbox_result_id: str | None
    task_id: str | None
    receipt: Mapping[str, object] | None
    created_at: str
    updated_at: str

    def __post_init__(self) -> None:
        for value, label in (
            (self.submission_id, "submission_id"),
            (self.owner, "owner"),
            (self.session_id, "session_id"),
            (self.turn_id, "turn_id"),
            (self.project_id, "project_id"),
            (self.workspace_id, "workspace_id"),
            (self.request_key, "request_key"),
        ):
            _identifier(value, label)
        if not _DIGEST.fullmatch(self.input_digest):
            raise ValueError("input_digest is invalid")
        if not isinstance(self.phase, BuilderPhase):
            raise TypeError("Builder phase is invalid")
        if not isinstance(self.state, BuilderSubmissionState):
            raise TypeError("Builder submission state is invalid")
        if isinstance(self.version, bool) or not isinstance(self.version, int) or self.version < 1:
            raise ValueError("Builder submission version is invalid")
        for optional_value, label in (
            (self.base_change_set_id, "base_change_set_id"),
            (self.change_set_id, "change_set_id"),
            (self.sandbox_result_id, "sandbox_result_id"),
            (self.task_id, "task_id"),
        ):
            if optional_value is not None:
                _identifier(optional_value, label)
        expected_phase = {
            BuilderSubmissionState.RUNNING: BuilderPhase.DRAFTING,
            BuilderSubmissionState.SANDBOX_FAILED: BuilderPhase.SANDBOX_FAILED,
            BuilderSubmissionState.SCHEDULED: BuilderPhase.VALIDATION_SCHEDULED,
        }[self.state]
        if self.phase is not expected_phase:
            raise ValueError("Builder phase does not match submission state")
        if self.state is BuilderSubmissionState.RUNNING and self.receipt is not None:
            raise ValueError("running Builder submission cannot have a receipt")
        if self.state is not BuilderSubmissionState.RUNNING and self.receipt is None:
            raise ValueError("terminal Builder submission requires a receipt")
        if self.receipt is not None:
            object.__setattr__(self, "receipt", _finite_json_object(self.receipt))
        object.__setattr__(self, "created_at", _timestamp(self.created_at, "created_at"))
        object.__setattr__(self, "updated_at", _timestamp(self.updated_at, "updated_at"))


def builder_submission_payload(value: BuilderSubmissionRecord) -> dict[str, Any]:
    if not isinstance(value, BuilderSubmissionRecord):
        raise TypeError("value must be a BuilderSubmissionRecord")
    return {
        "submission_id": value.submission_id,
        "owner": value.owner,
        "session_id": value.session_id,
        "turn_id": value.turn_id,
        "project_id": value.project_id,
        "workspace_id": value.workspace_id,
        "request_key": value.request_key,
        "input_digest": value.input_digest,
        "phase": value.phase.value,
        "state": value.state.value,
        "version": value.version,
        "base_change_set_id": value.base_change_set_id,
        "change_set_id": value.change_set_id,
        "sandbox_result_id": value.sandbox_result_id,
        "task_id": value.task_id,
        "receipt": None if value.receipt is None else dict(value.receipt),
        "created_at": value.created_at,
        "updated_at": value.updated_at,
    }


def builder_submission_from_payload(payload: Mapping[str, object]) -> BuilderSubmissionRecord:
    if not isinstance(payload, Mapping):
        raise TypeError("Builder submission payload must be an object")
    return BuilderSubmissionRecord(
        submission_id=str(payload["submission_id"]),
        owner=str(payload["owner"]),
        session_id=str(payload["session_id"]),
        turn_id=str(payload["turn_id"]),
        project_id=str(payload["project_id"]),
        workspace_id=str(payload["workspace_id"]),
        request_key=str(payload["request_key"]),
        input_digest=str(payload["input_digest"]),
        phase=BuilderPhase(str(payload["phase"])),
        state=BuilderSubmissionState(str(payload["state"])),
        version=_payload_version(payload["version"]),
        base_change_set_id=_optional_text(payload.get("base_change_set_id")),
        change_set_id=_optional_text(payload.get("change_set_id")),
        sandbox_result_id=_optional_text(payload.get("sandbox_result_id")),
        task_id=_optional_text(payload.get("task_id")),
        receipt=_optional_mapping(payload.get("receipt")),
        created_at=str(payload["created_at"]),
        updated_at=str(payload["updated_at"]),
    )


def _finite_json_object(value: Mapping[str, object]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError("Builder receipt must be an object")
    try:
        encoded = json.dumps(
            dict(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("Builder receipt must be finite JSON") from exc
    if len(encoded.encode("utf-8")) > 1_048_576:
        raise ValueError("Builder receipt exceeds 1 MiB")
    decoded = json.loads(encoded)
    if not isinstance(decoded, dict):
        raise TypeError("Builder receipt must be an object")
    return decoded


def _identifier(value: str, label: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{label} is invalid")
    return value


def _optional_text(value: object) -> str | None:
    return None if value is None else str(value)


def _optional_mapping(value: object) -> Mapping[str, object] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise TypeError("Builder receipt must be an object")
    return value


def _payload_version(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("Builder submission version must be an integer")
    return value


def _timestamp(value: str, label: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be a string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} is invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{label} must include a timezone")
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")
