"""Write-ahead journal for fenced Workspace live mutations.

The journal is the database half of AC4's filesystem/DB two-phase boundary.
Preparing a journal does not touch files.  ``mark_files_applied`` records a
verified post-write digest, and ``commit`` advances the Workspace live head and
the journal to COMMITTED in the same SQLite transaction.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pilot107.agent.workspace_live import (
    WorkspaceLiveConflict,
    WorkspaceLiveHead,
    WorkspaceWriterLease,
)
from pilot107.core.identity import is_safe_username
from pilot107.core.schema_migrations import SchemaMigration, apply_schema_migrations


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
                raise ValueError("Workspace mutation target revision must advance by one")
            assert self.to_digest is not None
            _digest(self.to_digest, "to_digest")
        _key(self.writer_id, "writer_id")
        _positive(self.fencing_token, "fencing_token")
        object.__setattr__(self, "files", tuple(self.files))
        if not self.files or len(self.files) > 256:
            raise ValueError("Workspace mutation files must contain 1 to 256 entries")
        if len({item.path for item in self.files}) != len(self.files):
            raise ValueError("Workspace mutation files contain duplicate paths")
        if any(not isinstance(item, WorkspaceMutationFile) for item in self.files):
            raise TypeError("Workspace mutation contains an invalid file plan")
        _key(self.backup_ref, "backup_ref")
        if self.error_code is not None:
            _key(self.error_code, "error_code")
        _timestamp_value(self.created_at, "created_at")
        _timestamp_value(self.updated_at, "updated_at")


WORKSPACE_MUTATION_JOURNAL_MIGRATION = SchemaMigration(
    migration_id="006b.007.agent_workspace_mutation_journal",
    statements=(
        """
        CREATE TABLE agent_workspace_mutation_journal (
            mutation_id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL
                REFERENCES agent_workspace_live_heads(workspace_id),
            project_id TEXT NOT NULL,
            owner TEXT NOT NULL,
            request_key TEXT NOT NULL,
            intent_digest TEXT NOT NULL,
            change_set_id TEXT REFERENCES agent_workspace_changesets(change_set_id),
            from_revision INTEGER NOT NULL,
            from_digest TEXT NOT NULL,
            to_revision INTEGER,
            to_digest TEXT,
            writer_id TEXT NOT NULL,
            fencing_token INTEGER NOT NULL,
            state TEXT NOT NULL,
            files_json TEXT NOT NULL,
            backup_ref TEXT NOT NULL,
            error_code TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE (workspace_id, request_key),
            CHECK (from_revision > 0),
            CHECK (fencing_token > 0),
            CHECK (state IN (
                'prepared', 'files_applied', 'committed', 'conflicted', 'rolled_back'
            )),
            CHECK (
                (to_revision IS NULL AND to_digest IS NULL)
                OR
                (to_revision = from_revision + 1 AND to_digest IS NOT NULL)
            )
        )
        """,
        """
        CREATE INDEX idx_agent_workspace_mutation_journal_open
        ON agent_workspace_mutation_journal(owner, workspace_id, state, updated_at)
        """,
    ),
)


class SQLiteWorkspaceMutationJournalStore:
    def __init__(
        self,
        db_path: Path,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.db_path = db_path
        self._clock = clock or (lambda: datetime.now(UTC))
        with self.connect() as connection:
            apply_schema_migrations(connection, (WORKSPACE_MUTATION_JOURNAL_MIGRATION,))

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def prepare(
        self,
        *,
        head: WorkspaceLiveHead,
        lease: WorkspaceWriterLease,
        request_key: str,
        files: Sequence[WorkspaceMutationFile],
        backup_ref: str,
        change_set_id: str | None = None,
    ) -> WorkspaceMutationJournal:
        if not isinstance(head, WorkspaceLiveHead):
            raise TypeError("head must be WorkspaceLiveHead")
        if not isinstance(lease, WorkspaceWriterLease):
            raise TypeError("lease must be WorkspaceWriterLease")
        _key(request_key, "request_key")
        _key(backup_ref, "backup_ref")
        normalized_files = _normalize_files(files)
        if change_set_id is not None:
            _key(change_set_id, "change_set_id")
        if (
            lease.workspace_id != head.workspace_id
            or lease.owner != head.owner
            or lease.writer_id != head.writer_id
            or lease.fencing_token != head.fencing_token
        ):
            raise WorkspaceLiveConflict("Workspace journal writer does not own the live head")
        mutation_id = _mutation_id(head.workspace_id, request_key)
        intent_digest = _intent_digest(
            workspace_id=head.workspace_id,
            project_id=head.project_id,
            owner=head.owner,
            request_key=request_key,
            change_set_id=change_set_id,
            from_revision=head.live_revision,
            from_digest=head.live_digest,
            files=normalized_files,
        )
        now = self._now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT * FROM agent_workspace_mutation_journal
                WHERE workspace_id = ? AND request_key = ?
                """,
                (head.workspace_id, request_key),
            ).fetchone()
            if existing is not None:
                record = _row_to_journal(existing)
                if record.intent_digest != intent_digest:
                    raise WorkspaceLiveConflict(
                        "Workspace mutation request_key refers to different intent"
                    )
                return record
            live = connection.execute(
                """
                SELECT * FROM agent_workspace_live_heads
                WHERE workspace_id = ? AND owner = ?
                """,
                (head.workspace_id, head.owner),
            ).fetchone()
            if live is None:
                raise KeyError(head.workspace_id)
            if not _live_authority_matches(
                live,
                lease=lease,
                expected_revision=head.live_revision,
                expected_digest=head.live_digest,
                now=now,
            ):
                raise WorkspaceLiveConflict("Workspace live head changed before journal prepare")
            connection.execute(
                """
                INSERT INTO agent_workspace_mutation_journal (
                    mutation_id, workspace_id, project_id, owner, request_key,
                    intent_digest, change_set_id, from_revision, from_digest,
                    to_revision, to_digest, writer_id, fencing_token, state,
                    files_json, backup_ref, error_code, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, 'prepared', ?, ?, NULL, ?, ?)
                """,
                (
                    mutation_id,
                    head.workspace_id,
                    head.project_id,
                    head.owner,
                    request_key,
                    intent_digest,
                    change_set_id,
                    head.live_revision,
                    head.live_digest,
                    lease.writer_id,
                    lease.fencing_token,
                    _files_json(normalized_files),
                    backup_ref,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM agent_workspace_mutation_journal WHERE mutation_id = ?",
                (mutation_id,),
            ).fetchone()
        if row is None:
            raise RuntimeError("Workspace mutation journal insert disappeared")
        return _row_to_journal(row)

    def get(self, mutation_id: str, *, owner: str) -> WorkspaceMutationJournal:
        _key(mutation_id, "mutation_id")
        if not is_safe_username(owner):
            raise ValueError("Workspace mutation owner is invalid")
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM agent_workspace_mutation_journal
                WHERE mutation_id = ? AND owner = ?
                """,
                (mutation_id, owner),
            ).fetchone()
        if row is None:
            raise KeyError(mutation_id)
        return _row_to_journal(row)

    def list_open(
        self, workspace_id: str, *, owner: str, limit: int = 100
    ) -> list[WorkspaceMutationJournal]:
        _key(workspace_id, "workspace_id")
        if not is_safe_username(owner):
            raise ValueError("Workspace mutation owner is invalid")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM agent_workspace_mutation_journal
                WHERE workspace_id = ? AND owner = ?
                  AND state IN ('prepared', 'files_applied')
                ORDER BY created_at, mutation_id
                LIMIT ?
                """,
                (workspace_id, owner, limit),
            ).fetchall()
        return [_row_to_journal(row) for row in rows]

    def mark_files_applied(
        self,
        mutation_id: str,
        *,
        owner: str,
        lease: WorkspaceWriterLease,
        to_digest: str,
    ) -> WorkspaceMutationJournal:
        _key(mutation_id, "mutation_id")
        if not is_safe_username(owner):
            raise ValueError("Workspace mutation owner is invalid")
        if not isinstance(lease, WorkspaceWriterLease):
            raise TypeError("lease must be WorkspaceWriterLease")
        _digest(to_digest, "to_digest")
        now = self._now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM agent_workspace_mutation_journal
                WHERE mutation_id = ? AND owner = ?
                """,
                (mutation_id, owner),
            ).fetchone()
            if row is None:
                raise KeyError(mutation_id)
            current = _row_to_journal(row)
            if current.state is WorkspaceMutationState.FILES_APPLIED:
                if current.to_digest != to_digest:
                    raise WorkspaceLiveConflict(
                        "Workspace mutation was applied with different live content"
                    )
                return current
            if current.state is not WorkspaceMutationState.PREPARED:
                raise WorkspaceLiveConflict("Workspace mutation is no longer prepared")
            live = connection.execute(
                """
                SELECT * FROM agent_workspace_live_heads
                WHERE workspace_id = ? AND owner = ?
                """,
                (current.workspace_id, owner),
            ).fetchone()
            if live is None:
                raise KeyError(current.workspace_id)
            if not _live_authority_matches(
                live,
                lease=lease,
                expected_revision=current.from_revision,
                expected_digest=current.from_digest,
                now=now,
            ):
                raise WorkspaceLiveConflict(
                    "Workspace live head changed before files-applied checkpoint"
                )
            connection.execute(
                """
                UPDATE agent_workspace_mutation_journal
                SET state = 'files_applied', to_revision = from_revision + 1,
                    to_digest = ?, updated_at = ?
                WHERE mutation_id = ? AND owner = ? AND state = 'prepared'
                """,
                (to_digest, now, mutation_id, owner),
            )
            updated = connection.execute(
                "SELECT * FROM agent_workspace_mutation_journal WHERE mutation_id = ?",
                (mutation_id,),
            ).fetchone()
        if updated is None:
            raise RuntimeError("Workspace mutation journal disappeared")
        return _row_to_journal(updated)

    def commit(
        self,
        mutation_id: str,
        *,
        owner: str,
        lease: WorkspaceWriterLease,
    ) -> WorkspaceMutationJournal:
        _key(mutation_id, "mutation_id")
        if not is_safe_username(owner):
            raise ValueError("Workspace mutation owner is invalid")
        if not isinstance(lease, WorkspaceWriterLease):
            raise TypeError("lease must be WorkspaceWriterLease")
        now = self._now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM agent_workspace_mutation_journal
                WHERE mutation_id = ? AND owner = ?
                """,
                (mutation_id, owner),
            ).fetchone()
            if row is None:
                raise KeyError(mutation_id)
            current = _row_to_journal(row)
            if current.state is WorkspaceMutationState.COMMITTED:
                return current
            if (
                current.state is not WorkspaceMutationState.FILES_APPLIED
                or current.to_revision is None
                or current.to_digest is None
            ):
                raise WorkspaceLiveConflict("Workspace mutation is not ready to commit")
            cursor = connection.execute(
                """
                UPDATE agent_workspace_live_heads
                SET live_revision = ?, live_digest = ?, updated_at = ?
                WHERE workspace_id = ? AND owner = ? AND writer_id = ?
                  AND fencing_token = ? AND writer_lease_expires_at > ?
                  AND live_revision = ? AND live_digest = ?
                """,
                (
                    current.to_revision,
                    current.to_digest,
                    now,
                    current.workspace_id,
                    owner,
                    lease.writer_id,
                    lease.fencing_token,
                    now,
                    current.from_revision,
                    current.from_digest,
                ),
            )
            if cursor.rowcount != 1:
                raise WorkspaceLiveConflict("Workspace live CAS failed while committing journal")
            journal_cursor = connection.execute(
                """
                UPDATE agent_workspace_mutation_journal
                SET state = 'committed', updated_at = ?
                WHERE mutation_id = ? AND owner = ? AND state = 'files_applied'
                  AND writer_id = ? AND fencing_token = ?
                """,
                (
                    now,
                    mutation_id,
                    owner,
                    lease.writer_id,
                    lease.fencing_token,
                ),
            )
            if journal_cursor.rowcount != 1:
                raise WorkspaceLiveConflict("Workspace journal fence changed during commit")
            committed = connection.execute(
                "SELECT * FROM agent_workspace_mutation_journal WHERE mutation_id = ?",
                (mutation_id,),
            ).fetchone()
        if committed is None:
            raise RuntimeError("Workspace mutation journal disappeared")
        return _row_to_journal(committed)

    def mark_conflicted(
        self,
        mutation_id: str,
        *,
        owner: str,
        error_code: str,
    ) -> WorkspaceMutationJournal:
        _key(mutation_id, "mutation_id")
        if not is_safe_username(owner):
            raise ValueError("Workspace mutation owner is invalid")
        _key(error_code, "error_code")
        now = self._now()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE agent_workspace_mutation_journal
                SET state = 'conflicted', error_code = ?, updated_at = ?
                WHERE mutation_id = ? AND owner = ?
                  AND state IN ('prepared', 'files_applied')
                """,
                (error_code, now, mutation_id, owner),
            )
            row = connection.execute(
                """
                SELECT * FROM agent_workspace_mutation_journal
                WHERE mutation_id = ? AND owner = ?
                """,
                (mutation_id, owner),
            ).fetchone()
        if row is None:
            raise KeyError(mutation_id)
        record = _row_to_journal(row)
        if cursor.rowcount != 1 and record.state is not WorkspaceMutationState.CONFLICTED:
            raise WorkspaceLiveConflict("Workspace mutation is already terminal")
        return record

    def mark_rolled_back(
        self,
        mutation_id: str,
        *,
        owner: str,
    ) -> WorkspaceMutationJournal:
        _key(mutation_id, "mutation_id")
        if not is_safe_username(owner):
            raise ValueError("Workspace mutation owner is invalid")
        now = self._now()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE agent_workspace_mutation_journal
                SET state = 'rolled_back', updated_at = ?
                WHERE mutation_id = ? AND owner = ? AND state = 'prepared'
                  AND EXISTS (
                    SELECT 1 FROM agent_workspace_live_heads
                    WHERE workspace_id = agent_workspace_mutation_journal.workspace_id
                      AND owner = agent_workspace_mutation_journal.owner
                      AND live_revision = agent_workspace_mutation_journal.from_revision
                      AND live_digest = agent_workspace_mutation_journal.from_digest
                  )
                """,
                (now, mutation_id, owner),
            )
            row = connection.execute(
                """
                SELECT * FROM agent_workspace_mutation_journal
                WHERE mutation_id = ? AND owner = ?
                """,
                (mutation_id, owner),
            ).fetchone()
        if row is None:
            raise KeyError(mutation_id)
        record = _row_to_journal(row)
        if cursor.rowcount != 1 and record.state is not WorkspaceMutationState.ROLLED_BACK:
            raise WorkspaceLiveConflict("Workspace mutation cannot be marked rolled back")
        return record

    def _clock_now(self) -> datetime:
        current = self._clock()
        if current.tzinfo is None:
            raise ValueError("Workspace journal clock must be timezone-aware")
        return current.astimezone(UTC)

    def _now(self) -> str:
        return _timestamp(self._clock_now())


def _normalize_files(
    files: Sequence[WorkspaceMutationFile],
) -> tuple[WorkspaceMutationFile, ...]:
    if isinstance(files, (str, bytes)):
        raise TypeError("files must be a sequence of WorkspaceMutationFile")
    normalized = tuple(files)
    if not normalized or len(normalized) > 256:
        raise ValueError("Workspace mutation files must contain 1 to 256 entries")
    if any(not isinstance(item, WorkspaceMutationFile) for item in normalized):
        raise TypeError("files must contain WorkspaceMutationFile")
    if len({item.path for item in normalized}) != len(normalized):
        raise ValueError("Workspace mutation files contain duplicate paths")
    return tuple(sorted(normalized, key=lambda item: item.path))


def _mutation_id(workspace_id: str, request_key: str) -> str:
    digest = hashlib.sha256(f"{workspace_id}\0{request_key}".encode()).hexdigest()
    return f"workspace-mutation-{digest}"


def _intent_digest(
    *,
    workspace_id: str,
    project_id: str,
    owner: str,
    request_key: str,
    change_set_id: str | None,
    from_revision: int,
    from_digest: str,
    files: tuple[WorkspaceMutationFile, ...],
) -> str:
    payload = {
        "workspace_id": workspace_id,
        "project_id": project_id,
        "owner": owner,
        "request_key": request_key,
        "change_set_id": change_set_id,
        "from_revision": from_revision,
        "from_digest": from_digest,
        "files": [_file_payload(item) for item in files],
    }
    return hashlib.sha256(_canonical_json(payload).encode()).hexdigest()


def _live_authority_matches(
    row: Mapping[str, object],
    *,
    lease: WorkspaceWriterLease,
    expected_revision: int,
    expected_digest: str,
    now: str,
) -> bool:
    return (
        str(row["workspace_id"]) == lease.workspace_id
        and str(row["owner"]) == lease.owner
        and row["writer_id"] == lease.writer_id
        and int(row["fencing_token"]) == lease.fencing_token
        and str(row["writer_lease_expires_at"]) > now
        and int(row["live_revision"]) == expected_revision
        and str(row["live_digest"]) == expected_digest
    )


def _files_json(files: tuple[WorkspaceMutationFile, ...]) -> str:
    return _canonical_json([_file_payload(item) for item in files])


def _file_payload(item: WorkspaceMutationFile) -> dict[str, object]:
    return {
        "path": item.path,
        "operation": item.operation,
        "before_sha256": item.before_sha256,
        "after_sha256": item.after_sha256,
    }


def _row_to_journal(row: Mapping[str, object]) -> WorkspaceMutationJournal:
    raw_files = json.loads(str(row["files_json"]))
    if not isinstance(raw_files, list):
        raise TypeError("Workspace mutation files_json is invalid")
    files = tuple(
        WorkspaceMutationFile(
            path=str(item["path"]),
            operation=str(item["operation"]),  # type: ignore[arg-type]
            before_sha256=(
                None if item.get("before_sha256") is None else str(item["before_sha256"])
            ),
            after_sha256=(
                None if item.get("after_sha256") is None else str(item["after_sha256"])
            ),
        )
        for item in raw_files
        if isinstance(item, Mapping)
    )
    if len(files) != len(raw_files):
        raise TypeError("Workspace mutation files_json contains invalid entries")
    return WorkspaceMutationJournal(
        mutation_id=str(row["mutation_id"]),
        workspace_id=str(row["workspace_id"]),
        project_id=str(row["project_id"]),
        owner=str(row["owner"]),
        request_key=str(row["request_key"]),
        intent_digest=str(row["intent_digest"]),
        change_set_id=None if row["change_set_id"] is None else str(row["change_set_id"]),
        from_revision=int(row["from_revision"]),
        from_digest=str(row["from_digest"]),
        to_revision=None if row["to_revision"] is None else int(row["to_revision"]),
        to_digest=None if row["to_digest"] is None else str(row["to_digest"]),
        writer_id=str(row["writer_id"]),
        fencing_token=int(row["fencing_token"]),
        state=WorkspaceMutationState(str(row["state"])),
        files=files,
        backup_ref=str(row["backup_ref"]),
        error_code=None if row["error_code"] is None else str(row["error_code"]),
        created_at=_timestamp_value(row["created_at"], "created_at"),
        updated_at=_timestamp_value(row["updated_at"], "updated_at"),
    )


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("Workspace mutation payload is not finite JSON") from exc


def _relative_path(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value.startswith("/")
        or "\\" in value
        or "\0" in value
        or any(part in {"", ".", ".."} for part in value.split("/"))
        or len(value) > 4096
    ):
        raise ValueError("Workspace mutation path is invalid")
    return value


def _key(value: str, label: str) -> str:
    if not isinstance(value, str) or not value or "\0" in value or len(value) > 512:
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


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("Workspace journal timestamp must be timezone-aware")
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
