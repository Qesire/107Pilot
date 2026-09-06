"""Workspace live-head domain types and explicit SQLite test durability.

Production composition is PostgreSQL-only. The SQLite implementation is retained
solely for explicitly injected development/test Project stores and is never selected
by the production live-store factory.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from pilot107.agent.workspace import AgentWorkspaceRecord
from pilot107.core.identity import is_safe_username
from pilot107.core.schema_migrations import SchemaMigration, apply_schema_migrations

_DIGEST_HEX_LENGTH = 64
_MAX_MANIFEST_ENTRIES = 10_000

WORKSPACE_LIVE_HEAD_MIGRATION = SchemaMigration(
    migration_id="006b.006.agent_workspace_live_heads",
    statements=(
        """
        CREATE TABLE agent_workspace_live_heads (
            workspace_id TEXT PRIMARY KEY REFERENCES agent_workspaces(workspace_id),
            project_id TEXT NOT NULL,
            owner TEXT NOT NULL,
            base_snapshot_digest TEXT NOT NULL,
            live_revision INTEGER NOT NULL,
            live_digest TEXT NOT NULL,
            writer_id TEXT,
            writer_lease_expires_at TEXT,
            fencing_token INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            CHECK (live_revision > 0),
            CHECK (fencing_token >= 0),
            CHECK (
                (writer_id IS NULL AND writer_lease_expires_at IS NULL)
                OR
                (writer_id IS NOT NULL AND writer_lease_expires_at IS NOT NULL)
            )
        )
        """,
        """
        CREATE INDEX idx_agent_workspace_live_heads_owner_updated
        ON agent_workspace_live_heads(owner, updated_at DESC, workspace_id DESC)
        """,
    ),
)


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
        self, lease: WorkspaceWriterLease, *, lease_seconds: int
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


def build_workspace_live_store(
    project_store: object,
    *,
    clock: Callable[[], datetime] | None = None,
) -> WorkspaceLiveStore:
    """Build the PostgreSQL live-head authority or fail closed."""

    dsn = getattr(project_store, "dsn", None)
    if not isinstance(dsn, str) or not dsn:
        raise RuntimeError(
            "Workspace live-head authority requires PostgreSQL; "
            "SQLite Workspace live authority has been retired from runtime composition"
        )
    from pilot107.agent.postgres_workspace_durability import PostgresWorkspaceLiveStore

    return PostgresWorkspaceLiveStore(dsn, clock=clock)


class SQLiteWorkspaceLiveStore:
    def __init__(
        self,
        db_path: Path,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.db_path = db_path
        self._clock = clock or (lambda: datetime.now(UTC))
        with self.connect() as connection:
            apply_schema_migrations(connection, (WORKSPACE_LIVE_HEAD_MIGRATION,))

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def ensure_head(self, workspace: AgentWorkspaceRecord) -> WorkspaceLiveHead:
        if not isinstance(workspace, AgentWorkspaceRecord):
            raise TypeError("workspace must be AgentWorkspaceRecord")
        try:
            existing = self.get_head(workspace.workspace_id, owner=workspace.owner)
        except KeyError:
            existing = None
        if existing is not None:
            if (
                existing.project_id != workspace.project_id
                or existing.base_snapshot_digest != workspace.snapshot.digest
            ):
                raise WorkspaceLiveConflict(
                    "Workspace live head refers to different immutable content",
                    current=existing,
                )
            return existing

        # Capture only while bootstrapping a legacy/imported Workspace.  AC4-B
        # will make every later mutation pass through a journal/fence rather
        # than rescanning on ordinary live-head reads.
        live_digest = capture_workspace_live_digest(workspace)
        now = self._now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            parent = connection.execute(
                """
                SELECT project_id, owner, snapshot_digest
                FROM agent_workspaces
                WHERE workspace_id = ? AND owner = ?
                """,
                (workspace.workspace_id, workspace.owner),
            ).fetchone()
            if parent is None:
                raise KeyError(workspace.workspace_id)
            if (
                str(parent["project_id"]) != workspace.project_id
                or str(parent["snapshot_digest"]) != workspace.snapshot.digest
            ):
                raise WorkspaceLiveConflict("Workspace immutable snapshot binding changed")
            connection.execute(
                """
                INSERT OR IGNORE INTO agent_workspace_live_heads (
                    workspace_id, project_id, owner, base_snapshot_digest,
                    live_revision, live_digest, writer_id,
                    writer_lease_expires_at, fencing_token, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 1, ?, NULL, NULL, 0, ?, ?)
                """,
                (
                    workspace.workspace_id,
                    workspace.project_id,
                    workspace.owner,
                    workspace.snapshot.digest,
                    live_digest,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM agent_workspace_live_heads WHERE workspace_id = ? AND owner = ?",
                (workspace.workspace_id, workspace.owner),
            ).fetchone()
        if row is None:
            raise RuntimeError("Workspace live-head insert did not produce a row")
        head = _row_to_head(row)
        if (
            head.project_id != workspace.project_id
            or head.base_snapshot_digest != workspace.snapshot.digest
        ):
            raise WorkspaceLiveConflict(
                "Workspace live head refers to different immutable content",
                current=head,
            )
        return head

    def get_head(self, workspace_id: str, *, owner: str) -> WorkspaceLiveHead:
        _key(workspace_id, "workspace_id")
        if not is_safe_username(owner):
            raise ValueError("Workspace live-head owner is invalid")
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM agent_workspace_live_heads WHERE workspace_id = ? AND owner = ?",
                (workspace_id, owner),
            ).fetchone()
        if row is None:
            raise KeyError(workspace_id)
        return _row_to_head(row)

    def claim_writer(
        self,
        workspace_id: str,
        *,
        owner: str,
        writer_id: str,
        lease_seconds: int,
    ) -> WorkspaceWriterLease:
        _key(workspace_id, "workspace_id")
        if not is_safe_username(owner):
            raise ValueError("Workspace writer owner is invalid")
        _writer_id(writer_id)
        _lease_seconds(lease_seconds)
        now_dt = self._clock_now()
        now = _timestamp(now_dt)
        expires_at = _timestamp(now_dt + timedelta(seconds=lease_seconds))
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE agent_workspace_live_heads
                SET writer_id = ?, writer_lease_expires_at = ?,
                    fencing_token = fencing_token + 1, updated_at = ?
                WHERE workspace_id = ? AND owner = ?
                  AND (
                    writer_id IS NULL
                    OR writer_lease_expires_at <= ?
                    OR writer_id = ?
                  )
                """,
                (writer_id, expires_at, now, workspace_id, owner, now, writer_id),
            )
            row = connection.execute(
                "SELECT * FROM agent_workspace_live_heads WHERE workspace_id = ? AND owner = ?",
                (workspace_id, owner),
            ).fetchone()
        if row is None:
            raise KeyError(workspace_id)
        head = _row_to_head(row)
        if cursor.rowcount != 1 or head.writer_id != writer_id:
            raise WorkspaceLiveConflict("Workspace is leased by another writer", current=head)
        return WorkspaceWriterLease(
            workspace_id=workspace_id,
            owner=owner,
            writer_id=writer_id,
            fencing_token=head.fencing_token,
            expires_at=expires_at,
        )

    def renew_writer(
        self, lease: WorkspaceWriterLease, *, lease_seconds: int
    ) -> WorkspaceWriterLease:
        if not isinstance(lease, WorkspaceWriterLease):
            raise TypeError("lease must be WorkspaceWriterLease")
        _lease_seconds(lease_seconds)
        now_dt = self._clock_now()
        now = _timestamp(now_dt)
        expires_at = _timestamp(now_dt + timedelta(seconds=lease_seconds))
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE agent_workspace_live_heads
                SET writer_lease_expires_at = ?, updated_at = ?
                WHERE workspace_id = ? AND owner = ? AND writer_id = ?
                  AND fencing_token = ? AND writer_lease_expires_at > ?
                """,
                (
                    expires_at,
                    now,
                    lease.workspace_id,
                    lease.owner,
                    lease.writer_id,
                    lease.fencing_token,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM agent_workspace_live_heads WHERE workspace_id = ? AND owner = ?",
                (lease.workspace_id, lease.owner),
            ).fetchone()
        if row is None:
            raise KeyError(lease.workspace_id)
        if cursor.rowcount != 1:
            raise WorkspaceLiveConflict(
                "Workspace writer lease is stale or fenced",
                current=_row_to_head(row),
            )
        return WorkspaceWriterLease(
            workspace_id=lease.workspace_id,
            owner=lease.owner,
            writer_id=lease.writer_id,
            fencing_token=lease.fencing_token,
            expires_at=expires_at,
        )

    def release_writer(self, lease: WorkspaceWriterLease) -> WorkspaceLiveHead:
        if not isinstance(lease, WorkspaceWriterLease):
            raise TypeError("lease must be WorkspaceWriterLease")
        now = self._now()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE agent_workspace_live_heads
                SET writer_id = NULL, writer_lease_expires_at = NULL, updated_at = ?
                WHERE workspace_id = ? AND owner = ? AND writer_id = ?
                  AND fencing_token = ?
                """,
                (
                    now,
                    lease.workspace_id,
                    lease.owner,
                    lease.writer_id,
                    lease.fencing_token,
                ),
            )
            row = connection.execute(
                "SELECT * FROM agent_workspace_live_heads WHERE workspace_id = ? AND owner = ?",
                (lease.workspace_id, lease.owner),
            ).fetchone()
        if row is None:
            raise KeyError(lease.workspace_id)
        head = _row_to_head(row)
        if cursor.rowcount != 1:
            raise WorkspaceLiveConflict("Workspace writer lease is stale or fenced", current=head)
        return head

    def compare_and_swap(
        self,
        lease: WorkspaceWriterLease,
        *,
        expected_revision: int,
        expected_digest: str,
        new_digest: str,
    ) -> WorkspaceLiveHead:
        if not isinstance(lease, WorkspaceWriterLease):
            raise TypeError("lease must be WorkspaceWriterLease")
        _positive(expected_revision, "expected_revision")
        _digest(expected_digest, "expected_digest")
        _digest(new_digest, "new_digest")
        if new_digest == expected_digest:
            raise ValueError("Workspace CAS must advance to different live content")
        now = self._now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE agent_workspace_live_heads
                SET live_revision = live_revision + 1, live_digest = ?, updated_at = ?
                WHERE workspace_id = ? AND owner = ? AND writer_id = ?
                  AND fencing_token = ? AND writer_lease_expires_at > ?
                  AND live_revision = ? AND live_digest = ?
                """,
                (
                    new_digest,
                    now,
                    lease.workspace_id,
                    lease.owner,
                    lease.writer_id,
                    lease.fencing_token,
                    now,
                    expected_revision,
                    expected_digest,
                ),
            )
            row = connection.execute(
                "SELECT * FROM agent_workspace_live_heads WHERE workspace_id = ? AND owner = ?",
                (lease.workspace_id, lease.owner),
            ).fetchone()
        if row is None:
            raise KeyError(lease.workspace_id)
        head = _row_to_head(row)
        if cursor.rowcount != 1:
            raise WorkspaceLiveConflict(
                "Workspace live revision, digest, or writer fence changed",
                current=head,
            )
        return head

    def _clock_now(self) -> datetime:
        current = self._clock()
        if current.tzinfo is None:
            raise ValueError("Workspace live store clock must be timezone-aware")
        return current.astimezone(UTC)

    def _now(self) -> str:
        return _timestamp(self._clock_now())


def capture_workspace_live_digest(
    workspace: AgentWorkspaceRecord,
    *,
    max_entries: int = _MAX_MANIFEST_ENTRIES,
) -> str:
    """Digest the current isolated local tree without mutating it.

    The manifest intentionally excludes mtime and absolute paths.  It includes
    each relative path, entry kind, permission mode and regular-file content
    digest/size.  Absolute deployment paths therefore do not affect the digest,
    while permission changes remain observable.
    """

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
            children = sorted(os.scandir(directory), key=lambda entry: entry.name, reverse=True)
        except OSError as exc:
            raise WorkspaceLiveConflict("Workspace live manifest could not be read") from exc
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
                raise WorkspaceLiveConflict("Workspace live manifest entry disappeared") from exc
            if stat.S_ISLNK(info.st_mode):
                raise WorkspaceLiveConflict("Workspace live manifest contains a symlink")
            mode = stat.S_IMODE(info.st_mode)
            if stat.S_ISDIR(info.st_mode):
                manifest.append({"path": relative, "kind": "directory", "mode": mode})
                stack.append(path)
                continue
            if not stat.S_ISREG(info.st_mode):
                raise WorkspaceLiveConflict(
                    "Workspace live manifest contains unsupported file type"
                )
            digest = _file_sha256(path)
            manifest.append(
                {
                    "path": relative,
                    "kind": "file",
                    "mode": mode,
                    "size_bytes": info.st_size,
                    "sha256": digest,
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


def _row_to_head(row: Mapping[str, Any]) -> WorkspaceLiveHead:
    return WorkspaceLiveHead(
        workspace_id=str(row["workspace_id"]),
        project_id=str(row["project_id"]),
        owner=str(row["owner"]),
        base_snapshot_digest=str(row["base_snapshot_digest"]),
        live_revision=int(row["live_revision"]),
        live_digest=str(row["live_digest"]),
        writer_id=None if row["writer_id"] is None else str(row["writer_id"]),
        writer_lease_expires_at=(
            None
            if row["writer_lease_expires_at"] is None
            else _timestamp_value(row["writer_lease_expires_at"], "writer_lease_expires_at")
        ),
        fencing_token=int(row["fencing_token"]),
        created_at=_timestamp_value(row["created_at"], "created_at"),
        updated_at=_timestamp_value(row["updated_at"], "updated_at"),
    )


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


def _lease_seconds(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 86_400:
        raise ValueError("lease_seconds must be between 1 and 86400")
    return value


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("Workspace live timestamp must be timezone-aware")
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _timestamp_value(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or "\0" in value:
        raise ValueError(f"{label} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} is invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{label} must be timezone-aware")
    return parsed.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
