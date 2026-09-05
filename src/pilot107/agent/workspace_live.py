"""Backend-neutral Workspace live-head domain types and filesystem digesting.

PostgreSQL is the only runtime durability authority. The historical SQLite live
store has been removed; the legacy class name remains only as a fail-closed
sentinel so stale imports cannot silently create a local authority.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from pilot107.agent.workspace import AgentWorkspaceRecord
from pilot107.core.identity import is_safe_username

_DIGEST_HEX_LENGTH = 64
_MAX_MANIFEST_ENTRIES = 10_000
_SQLITE_RETIRED = "SQLite Workspace live authority has been retired"


class WorkspaceLiveConflict(RuntimeError):
    """A Workspace live-head revision, digest, writer, or fence changed."""

    def __init__(self, message: str, *, current: WorkspaceLiveHead | None = None) -> None:
        super().__init__(message)
        self.current = current


@dataclass(frozen=True)
class WorkspaceLiveHead:
    workspace_id: str
    project_id: str
    owner: str
    base_snapshot_digest: str
    live_revision: int
    live_digest: str
    writer_id: str | None
    writer_lease_expires_at: str | None
    fencing_token: int
    created_at: str
    updated_at: str

    def __post_init__(self) -> None:
        _key(self.workspace_id, "workspace_id")
        _key(self.project_id, "project_id")
        if not is_safe_username(self.owner):
            raise ValueError("Workspace live-head owner is invalid")
        _digest(self.base_snapshot_digest, "base_snapshot_digest")
        _digest(self.live_digest, "live_digest")
        _positive(self.live_revision, "live_revision")
        if isinstance(self.fencing_token, bool) or self.fencing_token < 0:
            raise ValueError("fencing_token must be non-negative")
        if (self.writer_id is None) != (self.writer_lease_expires_at is None):
            raise ValueError("Workspace writer and lease expiry must be present together")
        if self.writer_id is not None:
            _writer_id(self.writer_id)
            _timestamp_value(self.writer_lease_expires_at, "writer_lease_expires_at")
        _timestamp_value(self.created_at, "created_at")
        _timestamp_value(self.updated_at, "updated_at")


@dataclass(frozen=True)
class WorkspaceWriterLease:
    workspace_id: str
    owner: str
    writer_id: str
    fencing_token: int
    expires_at: str

    def __post_init__(self) -> None:
        _key(self.workspace_id, "workspace_id")
        if not is_safe_username(self.owner):
            raise ValueError("Workspace writer owner is invalid")
        _writer_id(self.writer_id)
        _positive(self.fencing_token, "fencing_token")
        _timestamp_value(self.expires_at, "expires_at")


class WorkspaceLiveStore(Protocol):
    def ensure_head(self, workspace: AgentWorkspaceRecord) -> WorkspaceLiveHead: ...

    def get_head(self, workspace_id: str, *, owner: str) -> WorkspaceLiveHead: ...

    def claim_writer(
        self,
        workspace_id: str,
        *,
        owner: str,
        writer_id: str,
        lease_seconds: int,
    ) -> WorkspaceWriterLease: ...

    def renew_writer(
        self,
        lease: WorkspaceWriterLease,
        *,
        lease_seconds: int,
    ) -> WorkspaceWriterLease: ...

    def release_writer(self, lease: WorkspaceWriterLease) -> WorkspaceLiveHead: ...

    def compare_and_swap(
        self,
        lease: WorkspaceWriterLease,
        *,
        expected_revision: int,
        expected_digest: str,
        new_digest: str,
    ) -> WorkspaceLiveHead: ...


class SQLiteWorkspaceLiveStore:
    """Rejected compatibility sentinel; never a usable persistence backend."""

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise RuntimeError(_SQLITE_RETIRED)


def build_workspace_live_store(
    project_store: object,
    *,
    clock: Callable[[], datetime] | None = None,
) -> WorkspaceLiveStore:
    """Build the PostgreSQL live-head authority or fail closed."""

    dsn = getattr(project_store, "dsn", None)
    if not isinstance(dsn, str) or not dsn:
        raise RuntimeError(
            "Workspace live-head authority requires PostgreSQL; " + _SQLITE_RETIRED
        )
    from pilot107.agent.postgres_workspace_durability import PostgresWorkspaceLiveStore

    return PostgresWorkspaceLiveStore(dsn, clock=clock)


def capture_workspace_live_digest(
    workspace: AgentWorkspaceRecord,
    *,
    max_entries: int = _MAX_MANIFEST_ENTRIES,
) -> str:
    """Digest current isolated Workspace content and permissions without mutation."""

    if not isinstance(workspace, AgentWorkspaceRecord):
        raise TypeError("workspace must be AgentWorkspaceRecord")
    if isinstance(max_entries, bool) or not isinstance(max_entries, int) or max_entries < 1:
        raise ValueError("max_entries must be positive")
    root = Path(workspace.local_root)
    try:
        root_stat = root.lstat()
    except FileNotFoundError as exc:
        raise WorkspaceLiveConflict("Workspace local root is missing") from exc
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        raise WorkspaceLiveConflict("Workspace local root is not a real directory")
    root = root.resolve(strict=True)
    manifest: list[dict[str, object]] = []
    stack = [root]
    while stack:
        directory = stack.pop()
        try:
            children = sorted(
                os.scandir(directory),
                key=lambda entry: entry.name,
                reverse=True,
            )
        except OSError as exc:
            raise WorkspaceLiveConflict(
                "Workspace live manifest could not be read"
            ) from exc
        for child in children:
            path = Path(child.path)
            relative = path.relative_to(root).as_posix()
            if not relative or relative.startswith("../"):
                raise WorkspaceLiveConflict("Workspace live manifest escaped its root")
            if len(manifest) >= max_entries:
                raise WorkspaceLiveConflict("Workspace live manifest exceeds entry limit")
            try:
                info = child.stat(follow_symlinks=False)
            except OSError as exc:
                raise WorkspaceLiveConflict(
                    "Workspace live manifest entry disappeared"
                ) from exc
            if stat.S_ISLNK(info.st_mode):
                raise WorkspaceLiveConflict("Workspace live manifest contains a symlink")
            mode = stat.S_IMODE(info.st_mode)
            if stat.S_ISDIR(info.st_mode):
                manifest.append(
                    {"path": relative, "kind": "directory", "mode": mode}
                )
                stack.append(path)
                continue
            if not stat.S_ISREG(info.st_mode):
                raise WorkspaceLiveConflict(
                    "Workspace live manifest contains unsupported file type"
                )
            manifest.append(
                {
                    "path": relative,
                    "kind": "file",
                    "mode": mode,
                    "size_bytes": info.st_size,
                    "sha256": _file_sha256(path),
                }
            )
    manifest.sort(key=lambda item: str(item["path"]))
    encoded = _canonical_json(
        {
            "schema_version": "pilot107.workspace-live-manifest/v1",
            "entries": manifest,
        }
    )
    return hashlib.sha256(encoded).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while True:
                block = handle.read(1024 * 1024)
                if not block:
                    break
                digest.update(block)
    except OSError as exc:
        raise WorkspaceLiveConflict("Workspace live file could not be hashed") from exc
    return digest.hexdigest()


def _canonical_json(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("Workspace live manifest is not finite JSON") from exc


def _key(value: str, label: str) -> str:
    if not isinstance(value, str) or not value or "\0" in value or len(value) > 512:
        raise ValueError(f"{label} is invalid")
    return value


def _writer_id(value: str) -> str:
    if not isinstance(value, str) or not value or "\0" in value or len(value) > 512:
        raise ValueError("writer_id is invalid")
    return value


def _digest(value: str, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != _DIGEST_HEX_LENGTH
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
    "SQLiteWorkspaceLiveStore",
    "WorkspaceLiveConflict",
    "WorkspaceLiveHead",
    "WorkspaceLiveStore",
    "WorkspaceWriterLease",
    "build_workspace_live_store",
    "capture_workspace_live_digest",
]
