"""Backend-neutral Workspace mutation-journal domain records.

PostgreSQL is the only runtime journal authority. The historical SQLite journal
implementation has been removed; its class name remains only as a fail-closed
sentinel for stale imports.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pilot107.core.identity import is_safe_username

_SQLITE_RETIRED = "SQLite Workspace mutation journal authority has been retired"


class WorkspaceMutationState(StrEnum):
    PREPARED = "prepared"
    FILES_APPLIED = "files_applied"
    COMMITTED = "committed"
    CONFLICTED = "conflicted"
    ROLLED_BACK = "rolled_back"


@dataclass(frozen=True)
class WorkspaceMutationFile:
    path: str
    operation: Literal["create", "modify", "delete"]
    before_sha256: str | None
    after_sha256: str | None

    def __post_init__(self) -> None:
        _relative_path(self.path)
        if self.operation not in {"create", "modify", "delete"}:
            raise ValueError("Workspace mutation operation is invalid")
        if self.operation == "create":
            if self.before_sha256 is not None or self.after_sha256 is None:
                raise ValueError("create mutation digests are invalid")
        elif self.operation == "delete":
            if self.before_sha256 is None or self.after_sha256 is not None:
                raise ValueError("delete mutation digests are invalid")
        elif self.before_sha256 is None or self.after_sha256 is None:
            raise ValueError("modify mutation digests are invalid")
        if self.before_sha256 is not None:
            _digest(self.before_sha256, "before_sha256")
        if self.after_sha256 is not None:
            _digest(self.after_sha256, "after_sha256")


@dataclass(frozen=True)
class WorkspaceMutationJournal:
    mutation_id: str
    workspace_id: str
    project_id: str
    owner: str
    request_key: str
    intent_digest: str
    change_set_id: str | None
    from_revision: int
    from_digest: str
    to_revision: int | None
    to_digest: str | None
    writer_id: str
    fencing_token: int
    state: WorkspaceMutationState
    files: tuple[WorkspaceMutationFile, ...]
    backup_ref: str
    error_code: str | None
    created_at: str
    updated_at: str

    def __post_init__(self) -> None:
        if not self.mutation_id.startswith("workspace-mutation-"):
            raise ValueError("mutation_id is invalid")
        _key(self.workspace_id, "workspace_id")
        _key(self.project_id, "project_id")
        if not is_safe_username(self.owner):
            raise ValueError("Workspace mutation owner is invalid")
        _key(self.request_key, "request_key")
        _digest(self.intent_digest, "intent_digest")
        if self.change_set_id is not None:
            _key(self.change_set_id, "change_set_id")
        _positive(self.from_revision, "from_revision")
        _digest(self.from_digest, "from_digest")
        if (self.to_revision is None) != (self.to_digest is None):
            raise ValueError("Workspace mutation target revision/digest must be paired")
        if self.to_revision is not None:
            if self.to_revision != self.from_revision + 1:
                raise ValueError(
                    "Workspace mutation target revision must advance by one"
                )
            assert self.to_digest is not None
            _digest(self.to_digest, "to_digest")
        _key(self.writer_id, "writer_id")
        _positive(self.fencing_token, "fencing_token")
        object.__setattr__(self, "files", tuple(self.files))
        if not self.files or len(self.files) > 256:
            raise ValueError(
                "Workspace mutation files must contain 1 to 256 entries"
            )
        if len({item.path for item in self.files}) != len(self.files):
            raise ValueError("Workspace mutation files contain duplicate paths")
        if any(not isinstance(item, WorkspaceMutationFile) for item in self.files):
            raise TypeError("Workspace mutation contains an invalid file plan")
        _key(self.backup_ref, "backup_ref")
        if self.error_code is not None:
            _key(self.error_code, "error_code")
        _timestamp_value(self.created_at, "created_at")
        _timestamp_value(self.updated_at, "updated_at")


class SQLiteWorkspaceMutationJournalStore:
    """Rejected compatibility sentinel; never a usable persistence backend."""

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise RuntimeError(_SQLITE_RETIRED)


def _relative_path(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value.startswith("/")
        or "\\" in value
        or "\0" in value
    ):
        raise ValueError("Workspace mutation path is invalid")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("Workspace mutation path is invalid")
    return value


def _key(value: str, label: str) -> str:
    if not isinstance(value, str) or not value or "\0" in value or len(value) > 4096:
        raise ValueError(f"{label} is invalid")
    return value


def _digest(value: str, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{label} must be a SHA-256 hex digest")
    return value


def _positive(value: int, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{label} must be positive")
    return value


def _timestamp_value(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or "\0" in value:
        raise ValueError(f"{label} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} is invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{label} must be timezone-aware")
    return parsed.astimezone(UTC).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


__all__ = [
    "SQLiteWorkspaceMutationJournalStore",
    "WorkspaceMutationFile",
    "WorkspaceMutationJournal",
    "WorkspaceMutationState",
]
