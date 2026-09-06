"""PostgreSQL AC4 live-head and mutation-journal persistence.

The filesystem mutation algorithm is backend-neutral, while authoritative
revision state, writer fencing and write-ahead receipts live in the same
PostgreSQL transaction domain as ``PostgresProjectStore``.
"""

from __future__ import annotations

import hashlib
import importlib
import json
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from pilot107.agent.workspace import AgentWorkspaceRecord
from pilot107.agent.workspace_journal import (
    WorkspaceMutationFile,
    WorkspaceMutationJournal,
    WorkspaceMutationState,
)
from pilot107.agent.workspace_live import (
    WorkspaceLiveConflict,
    WorkspaceLiveHead,
    WorkspaceWriterLease,
    capture_workspace_live_digest,
)
from pilot107.core.identity import is_safe_username
from pilot107.core.postgres_domain_schema import initialize_postgres_domain_schema

_POSTGRES_MIGRATION_ID = "006c.001.postgres_workspace_durability"
_POSTGRES_STATEMENTS = (
    """
    CREATE TABLE agent_workspace_live_heads (
        workspace_id TEXT PRIMARY KEY REFERENCES agent_workspaces(workspace_id) ON DELETE CASCADE,
        project_id TEXT NOT NULL,
        owner TEXT NOT NULL,
        base_snapshot_digest TEXT NOT NULL,
        live_revision BIGINT NOT NULL,
        live_digest TEXT NOT NULL,
        writer_id TEXT,
        writer_lease_expires_at TIMESTAMPTZ,
        fencing_token BIGINT NOT NULL DEFAULT 0,
        created_at TIMESTAMPTZ NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL,
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
    """
    CREATE TABLE agent_workspace_mutation_journal (
        mutation_id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL REFERENCES agent_workspace_live_heads(workspace_id)
            ON DELETE CASCADE,
        project_id TEXT NOT NULL,
        owner TEXT NOT NULL,
        request_key TEXT NOT NULL,
        intent_digest TEXT NOT NULL,
        change_set_id TEXT REFERENCES agent_workspace_changesets(change_set_id),
        from_revision BIGINT NOT NULL,
        from_digest TEXT NOT NULL,
        to_revision BIGINT,
        to_digest TEXT,
        writer_id TEXT NOT NULL,
        fencing_token BIGINT NOT NULL,
        state TEXT NOT NULL,
        files_json JSONB NOT NULL,
        backup_ref TEXT NOT NULL,
        error_code TEXT,
        created_at TIMESTAMPTZ NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL,
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
)


class PostgresWorkspaceDurabilitySchema:
    """Install the isolated AC4 PostgreSQL migration under the shared lock."""

    def __init__(self, dsn: str) -> None:
        _dsn(dsn)
        self.dsn = dsn
        self._psycopg = importlib.import_module("psycopg")
        self._dict_row = importlib.import_module("psycopg.rows").dict_row
        initialize_postgres_domain_schema(dsn)
        self.ensure()

    def connect(self) -> Any:
        return self._psycopg.connect(self.dsn, row_factory=self._dict_row)

    def ensure(self) -> None:
        checksum = _migration_checksum(_POSTGRES_STATEMENTS)
        with self.connect() as connection, connection.transaction():
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                ("pilot107:migrations",),
            )
            existing = connection.execute(
                "SELECT checksum FROM schema_migrations WHERE migration_id = %s",
                (_POSTGRES_MIGRATION_ID,),
            ).fetchone()
            if existing is not None:
                if str(existing["checksum"]) != checksum:
                    raise RuntimeError(
                        f"migration checksum changed: {_POSTGRES_MIGRATION_ID}"
                    )
                return
            for statement in _POSTGRES_STATEMENTS:
                connection.execute(statement)
            connection.execute(
                """
                INSERT INTO schema_migrations (migration_id, checksum, applied_at)
                VALUES (%s, %s, %s)
                """,
                (_POSTGRES_MIGRATION_ID, checksum, datetime.now(UTC)),
            )


class PostgresWorkspaceLiveStore:
    def __init__(
        self,
        dsn: str,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        schema = PostgresWorkspaceDurabilitySchema(dsn)
        self.dsn = dsn
        self._psycopg = schema._psycopg
        self._dict_row = schema._dict_row
        self._clock = clock or (lambda: datetime.now(UTC))

    def connect(self) -> Any:
        return self._psycopg.connect(self.dsn, row_factory=self._dict_row)

    def ensure_head(self, workspace: AgentWorkspaceRecord) -> WorkspaceLiveHead:
        if not isinstance(workspace, AgentWorkspaceRecord):
            raise TypeError("workspace must be AgentWorkspaceRecord")
        try:
            existing = self.get_head(workspace.workspace_id, owner=workspace.owner)
        except KeyError:
            existing = None
        if existing is not None:
            _assert_workspace_binding(existing, workspace)
            return existing

        live_digest = capture_workspace_live_digest(workspace)
        now = self._now()
        with self.connect() as connection, connection.transaction():
            parent = connection.execute(
                """
                SELECT project_id, owner, snapshot_digest
                FROM agent_workspaces
                WHERE workspace_id = %s AND owner = %s
                FOR SHARE
                """,
                (workspace.workspace_id, workspace.owner),
            ).fetchone()
            if parent is None:
                raise KeyError(workspace.workspace_id)
            if (
                str(parent["project_id"]) != workspace.project_id
                or str(parent["owner"]) != workspace.owner
                or str(parent["snapshot_digest"]) != workspace.snapshot.digest
            ):
                raise WorkspaceLiveConflict(
                    "Workspace persistence does not match the immutable snapshot"
                )
            row = connection.execute(
                """
                INSERT INTO agent_workspace_live_heads (
                    workspace_id, project_id, owner, base_snapshot_digest,
                    live_revision, live_digest, writer_id, writer_lease_expires_at,
                    fencing_token, created_at, updated_at
                ) VALUES (%s, %s, %s, %s, 1, %s, NULL, NULL, 0, %s, %s)
                ON CONFLICT (workspace_id) DO NOTHING
                RETURNING *
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
            ).fetchone()
            if row is None:
                row = connection.execute(
                    """
                    SELECT * FROM agent_workspace_live_heads
                    WHERE workspace_id = %s AND owner = %s
                    """,
                    (workspace.workspace_id, workspace.owner),
                ).fetchone()
        if row is None:
            raise RuntimeError("Workspace live-head bootstrap disappeared")
        head = _row_to_head(row)
        _assert_workspace_binding(head, workspace)
        return head

    def get_head(self, workspace_id: str, *, owner: str) -> WorkspaceLiveHead:
        _key(workspace_id, "workspace_id")
        _owner(owner)
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM agent_workspace_live_heads
                WHERE workspace_id = %s AND owner = %s
                """,
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
        _owner(owner)
        _key(writer_id, "writer_id")
        _lease_seconds(lease_seconds)
        now = self._now()
        expires_at = now + timedelta(seconds=lease_seconds)
        with self.connect() as connection, connection.transaction():
            row = connection.execute(
                """
                UPDATE agent_workspace_live_heads
                SET writer_id = %s, writer_lease_expires_at = %s,
                    fencing_token = fencing_token + 1, updated_at = %s
                WHERE workspace_id = %s AND owner = %s
                  AND (
                    writer_id IS NULL
                    OR writer_lease_expires_at <= %s
                    OR writer_id = %s
                  )
                RETURNING *
                """,
                (writer_id, expires_at, now, workspace_id, owner, now, writer_id),
            ).fetchone()
            if row is None:
                current = connection.execute(
                    """
                    SELECT * FROM agent_workspace_live_heads
                    WHERE workspace_id = %s AND owner = %s
                    """,
                    (workspace_id, owner),
                ).fetchone()
                if current is None:
                    raise KeyError(workspace_id)
                raise WorkspaceLiveConflict(
                    "Workspace is leased by another writer",
                    current=_row_to_head(current),
                )
        head = _row_to_head(row)
        return WorkspaceWriterLease(
            workspace_id=workspace_id,
            owner=owner,
            writer_id=writer_id,
            fencing_token=head.fencing_token,
            expires_at=_timestamp_text(expires_at),
        )

    def renew_writer(
        self,
        lease: WorkspaceWriterLease,
        *,
        lease_seconds: int,
    ) -> WorkspaceWriterLease:
        if not isinstance(lease, WorkspaceWriterLease):
            raise TypeError("lease must be WorkspaceWriterLease")
        _lease_seconds(lease_seconds)
        now = self._now()
        expires_at = now + timedelta(seconds=lease_seconds)
        with self.connect() as connection, connection.transaction():
            row = connection.execute(
                """
                UPDATE agent_workspace_live_heads
                SET writer_lease_expires_at = %s, updated_at = %s
                WHERE workspace_id = %s AND owner = %s AND writer_id = %s
                  AND fencing_token = %s AND writer_lease_expires_at > %s
                RETURNING *
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
            ).fetchone()
            if row is None:
                current = connection.execute(
                    """
                    SELECT * FROM agent_workspace_live_heads
                    WHERE workspace_id = %s AND owner = %s
                    """,
                    (lease.workspace_id, lease.owner),
                ).fetchone()
                if current is None:
                    raise KeyError(lease.workspace_id)
                raise WorkspaceLiveConflict(
                    "Workspace writer lease is stale or fenced",
                    current=_row_to_head(current),
                )
        return WorkspaceWriterLease(
            workspace_id=lease.workspace_id,
            owner=lease.owner,
            writer_id=lease.writer_id,
            fencing_token=lease.fencing_token,
            expires_at=_timestamp_text(expires_at),
        )

    def release_writer(self, lease: WorkspaceWriterLease) -> WorkspaceLiveHead:
        if not isinstance(lease, WorkspaceWriterLease):
            raise TypeError("lease must be WorkspaceWriterLease")
        now = self._now()
        with self.connect() as connection, connection.transaction():
            row = connection.execute(
                """
                UPDATE agent_workspace_live_heads
                SET writer_id = NULL, writer_lease_expires_at = NULL, updated_at = %s
                WHERE workspace_id = %s AND owner = %s AND writer_id = %s
                  AND fencing_token = %s
                RETURNING *
                """,
                (
                    now,
                    lease.workspace_id,
                    lease.owner,
                    lease.writer_id,
                    lease.fencing_token,
                ),
            ).fetchone()
            if row is None:
                current = connection.execute(
                    """
                    SELECT * FROM agent_workspace_live_heads
                    WHERE workspace_id = %s AND owner = %s
                    """,
                    (lease.workspace_id, lease.owner),
                ).fetchone()
                if current is None:
                    raise KeyError(lease.workspace_id)
                raise WorkspaceLiveConflict(
                    "Workspace writer lease is stale or fenced",
                    current=_row_to_head(current),
                )
        return _row_to_head(row)

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
        if expected_digest == new_digest:
            raise ValueError("Workspace CAS must advance to different live content")
        now = self._now()
        with self.connect() as connection, connection.transaction():
            row = connection.execute(
                """
                UPDATE agent_workspace_live_heads
                SET live_revision = live_revision + 1, live_digest = %s, updated_at = %s
                WHERE workspace_id = %s AND owner = %s AND writer_id = %s
                  AND fencing_token = %s AND writer_lease_expires_at > %s
                  AND live_revision = %s AND live_digest = %s
                RETURNING *
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
            ).fetchone()
            if row is None:
                current = connection.execute(
                    """
                    SELECT * FROM agent_workspace_live_heads
                    WHERE workspace_id = %s AND owner = %s
                    """,
                    (lease.workspace_id, lease.owner),
                ).fetchone()
                if current is None:
                    raise KeyError(lease.workspace_id)
                raise WorkspaceLiveConflict(
                    "Workspace live CAS failed",
                    current=_row_to_head(current),
                )
        return _row_to_head(row)

    def _now(self) -> datetime:
        current = self._clock()
        if current.tzinfo is None:
            raise ValueError("Workspace live clock must be timezone-aware")
        return current.astimezone(UTC)


class PostgresWorkspaceMutationJournalStore:
    def __init__(
        self,
        dsn: str,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        schema = PostgresWorkspaceDurabilitySchema(dsn)
        self.dsn = dsn
        self._psycopg = schema._psycopg
        self._dict_row = schema._dict_row
        self._jsonb = importlib.import_module("psycopg.types.json").Jsonb
        self._clock = clock or (lambda: datetime.now(UTC))

    def connect(self) -> Any:
        return self._psycopg.connect(self.dsn, row_factory=self._dict_row)

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
            raise WorkspaceLiveConflict(
                "Workspace journal writer does not own the live head"
            )
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
        with self.connect() as connection, connection.transaction():
            existing = connection.execute(
                """
                SELECT * FROM agent_workspace_mutation_journal
                WHERE workspace_id = %s AND request_key = %s
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
                WHERE workspace_id = %s AND owner = %s
                FOR UPDATE
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
                raise WorkspaceLiveConflict(
                    "Workspace live head changed before journal prepare"
                )
            connection.execute(
                """
                INSERT INTO agent_workspace_mutation_journal (
                    mutation_id, workspace_id, project_id, owner, request_key,
                    intent_digest, change_set_id, from_revision, from_digest,
                    to_revision, to_digest, writer_id, fencing_token, state,
                    files_json, backup_ref, error_code, created_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                          NULL, NULL, %s, %s, 'prepared', %s, %s, NULL, %s, %s)
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
                    self._jsonb(
                        [_file_payload(item) for item in normalized_files]
                    ),
                    backup_ref,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                """
                SELECT * FROM agent_workspace_mutation_journal
                WHERE mutation_id = %s
                """,
                (mutation_id,),
            ).fetchone()
        if row is None:
            raise RuntimeError("Workspace mutation journal insert disappeared")
        return _row_to_journal(row)

    def get(self, mutation_id: str, *, owner: str) -> WorkspaceMutationJournal:
        _key(mutation_id, "mutation_id")
        _owner(owner)
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM agent_workspace_mutation_journal
                WHERE mutation_id = %s AND owner = %s
                """,
                (mutation_id, owner),
            ).fetchone()
        if row is None:
            raise KeyError(mutation_id)
        return _row_to_journal(row)

    def list_open(
        self,
        workspace_id: str,
        *,
        owner: str,
        limit: int = 100,
    ) -> list[WorkspaceMutationJournal]:
        _key(workspace_id, "workspace_id")
        _owner(owner)
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 1000
        ):
            raise ValueError("limit must be between 1 and 1000")
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM agent_workspace_mutation_journal
                WHERE workspace_id = %s AND owner = %s
                  AND state IN ('prepared', 'files_applied')
                ORDER BY created_at, mutation_id
                LIMIT %s
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
        _owner(owner)
        if not isinstance(lease, WorkspaceWriterLease):
            raise TypeError("lease must be WorkspaceWriterLease")
        _digest(to_digest, "to_digest")
        now = self._now()
        with self.connect() as connection, connection.transaction():
            row = connection.execute(
                """
                SELECT * FROM agent_workspace_mutation_journal
                WHERE mutation_id = %s AND owner = %s
                FOR UPDATE
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
                raise WorkspaceLiveConflict(
                    "Workspace mutation is no longer prepared"
                )
            live = connection.execute(
                """
                SELECT * FROM agent_workspace_live_heads
                WHERE workspace_id = %s AND owner = %s
                FOR UPDATE
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
            updated = connection.execute(
                """
                UPDATE agent_workspace_mutation_journal
                SET state = 'files_applied', to_revision = from_revision + 1,
                    to_digest = %s, updated_at = %s
                WHERE mutation_id = %s AND owner = %s AND state = 'prepared'
                RETURNING *
                """,
                (to_digest, now, mutation_id, owner),
            ).fetchone()
        if updated is None:
            raise WorkspaceLiveConflict(
                "Workspace mutation files-applied CAS failed"
            )
        return _row_to_journal(updated)

    def commit(
        self,
        mutation_id: str,
        *,
        owner: str,
        lease: WorkspaceWriterLease,
    ) -> WorkspaceMutationJournal:
        _key(mutation_id, "mutation_id")
        _owner(owner)
        if not isinstance(lease, WorkspaceWriterLease):
            raise TypeError("lease must be WorkspaceWriterLease")
        now = self._now()
        with self.connect() as connection, connection.transaction():
            row = connection.execute(
                """
                SELECT * FROM agent_workspace_mutation_journal
                WHERE mutation_id = %s AND owner = %s
                FOR UPDATE
                """,
                (mutation_id, owner),
            ).fetchone()
            if row is None:
                raise KeyError(mutation_id)
            current = _row_to_journal(row)
            if current.state is WorkspaceMutationState.COMMITTED:
                return current
            if lease.workspace_id != current.workspace_id or lease.owner != owner:
                raise WorkspaceLiveConflict(
                    "Workspace journal lease does not own this mutation"
                )
            if (
                current.state is not WorkspaceMutationState.FILES_APPLIED
                or current.to_revision is None
                or current.to_digest is None
            ):
                raise WorkspaceLiveConflict(
                    "Workspace mutation is not ready to commit"
                )
            live = connection.execute(
                """
                UPDATE agent_workspace_live_heads
                SET live_revision = %s, live_digest = %s, updated_at = %s
                WHERE workspace_id = %s AND owner = %s AND writer_id = %s
                  AND fencing_token = %s AND writer_lease_expires_at > %s
                  AND live_revision = %s AND live_digest = %s
                RETURNING *
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
            ).fetchone()
            if live is None:
                raise WorkspaceLiveConflict(
                    "Workspace live CAS failed while committing journal"
                )
            committed = connection.execute(
                """
                UPDATE agent_workspace_mutation_journal
                SET state = 'committed', updated_at = %s
                WHERE mutation_id = %s AND owner = %s AND state = 'files_applied'
                  AND writer_id = %s AND fencing_token = %s
                RETURNING *
                """,
                (
                    now,
                    mutation_id,
                    owner,
                    lease.writer_id,
                    lease.fencing_token,
                ),
            ).fetchone()
            if committed is None:
                raise WorkspaceLiveConflict(
                    "Workspace journal fence changed during commit"
                )
        return _row_to_journal(committed)

    def mark_conflicted(
        self,
        mutation_id: str,
        *,
        owner: str,
        error_code: str,
    ) -> WorkspaceMutationJournal:
        _key(mutation_id, "mutation_id")
        _owner(owner)
        _key(error_code, "error_code")
        now = self._now()
        with self.connect() as connection, connection.transaction():
            row = connection.execute(
                """
                UPDATE agent_workspace_mutation_journal
                SET state = 'conflicted', error_code = %s, updated_at = %s
                WHERE mutation_id = %s AND owner = %s
                  AND state IN ('prepared', 'files_applied')
                RETURNING *
                """,
                (error_code, now, mutation_id, owner),
            ).fetchone()
            if row is None:
                row = connection.execute(
                    """
                    SELECT * FROM agent_workspace_mutation_journal
                    WHERE mutation_id = %s AND owner = %s
                    """,
                    (mutation_id, owner),
                ).fetchone()
                if row is None:
                    raise KeyError(mutation_id)
                record = _row_to_journal(row)
                if record.state is not WorkspaceMutationState.CONFLICTED:
                    raise WorkspaceLiveConflict(
                        "Workspace mutation is already terminal"
                    )
                return record
        return _row_to_journal(row)

    def mark_rolled_back(
        self,
        mutation_id: str,
        *,
        owner: str,
        observed_live_digest: str,
    ) -> WorkspaceMutationJournal:
        _key(mutation_id, "mutation_id")
        _owner(owner)
        _digest(observed_live_digest, "observed_live_digest")
        now = self._now()
        with self.connect() as connection, connection.transaction():
            current_row = connection.execute(
                """
                SELECT * FROM agent_workspace_mutation_journal
                WHERE mutation_id = %s AND owner = %s
                FOR UPDATE
                """,
                (mutation_id, owner),
            ).fetchone()
            if current_row is None:
                raise KeyError(mutation_id)
            current = _row_to_journal(current_row)
            if current.state is WorkspaceMutationState.ROLLED_BACK:
                return current
            if observed_live_digest != current.from_digest:
                raise WorkspaceLiveConflict(
                    "Workspace observed live digest does not prove rollback"
                )
            row = connection.execute(
                """
                UPDATE agent_workspace_mutation_journal AS journal
                SET state = 'rolled_back', updated_at = %s
                WHERE mutation_id = %s AND owner = %s AND state = 'prepared'
                  AND EXISTS (
                    SELECT 1 FROM agent_workspace_live_heads AS live
                    WHERE live.workspace_id = journal.workspace_id
                      AND live.owner = journal.owner
                      AND live.live_revision = journal.from_revision
                      AND live.live_digest = journal.from_digest
                  )
                RETURNING *
                """,
                (now, mutation_id, owner),
            ).fetchone()
            if row is None:
                raise WorkspaceLiveConflict(
                    "Workspace mutation cannot be marked rolled back"
                )
        return _row_to_journal(row)

    def _now(self) -> datetime:
        current = self._clock()
        if current.tzinfo is None:
            raise ValueError("Workspace journal clock must be timezone-aware")
        return current.astimezone(UTC)


def _assert_workspace_binding(
    head: WorkspaceLiveHead,
    workspace: AgentWorkspaceRecord,
) -> None:
    if (
        head.project_id != workspace.project_id
        or head.owner != workspace.owner
        or head.base_snapshot_digest != workspace.snapshot.digest
    ):
        raise WorkspaceLiveConflict(
            "Workspace live head refers to different immutable content",
            current=head,
        )


def _live_authority_matches(
    row: Mapping[str, object],
    *,
    lease: WorkspaceWriterLease,
    expected_revision: int,
    expected_digest: str,
    now: datetime,
) -> bool:
    expiry = _datetime_value(
        row["writer_lease_expires_at"], "writer_lease_expires_at"
    )
    return (
        str(row["workspace_id"]) == lease.workspace_id
        and str(row["owner"]) == lease.owner
        and row["writer_id"] == lease.writer_id
        and _integer_value(row["fencing_token"], "fencing_token")
        == lease.fencing_token
        and expiry > now
        and _integer_value(row["live_revision"], "live_revision")
        == expected_revision
        and str(row["live_digest"]) == expected_digest
    )


def _row_to_head(row: Mapping[str, object]) -> WorkspaceLiveHead:
    expiry = row["writer_lease_expires_at"]
    return WorkspaceLiveHead(
        workspace_id=str(row["workspace_id"]),
        project_id=str(row["project_id"]),
        owner=str(row["owner"]),
        base_snapshot_digest=str(row["base_snapshot_digest"]),
        live_revision=_integer_value(row["live_revision"], "live_revision"),
        live_digest=str(row["live_digest"]),
        writer_id=(
            None if row["writer_id"] is None else str(row["writer_id"])
        ),
        writer_lease_expires_at=(
            None if expiry is None else _timestamp_text(expiry)
        ),
        fencing_token=_integer_value(row["fencing_token"], "fencing_token"),
        created_at=_timestamp_text(row["created_at"]),
        updated_at=_timestamp_text(row["updated_at"]),
    )


def _row_to_journal(row: Mapping[str, object]) -> WorkspaceMutationJournal:
    raw_files = row["files_json"]
    if isinstance(raw_files, str):
        raw_files = json.loads(raw_files)
    if not isinstance(raw_files, list):
        raise TypeError("Workspace mutation files_json is invalid")
    files: list[WorkspaceMutationFile] = []
    for item in raw_files:
        if not isinstance(item, Mapping):
            raise TypeError(
                "Workspace mutation files_json contains invalid entries"
            )
        files.append(
            WorkspaceMutationFile(
                path=str(item["path"]),
                operation=str(item["operation"]),  # type: ignore[arg-type]
                before_sha256=(
                    None
                    if item.get("before_sha256") is None
                    else str(item["before_sha256"])
                ),
                after_sha256=(
                    None
                    if item.get("after_sha256") is None
                    else str(item["after_sha256"])
                ),
            )
        )
    return WorkspaceMutationJournal(
        mutation_id=str(row["mutation_id"]),
        workspace_id=str(row["workspace_id"]),
        project_id=str(row["project_id"]),
        owner=str(row["owner"]),
        request_key=str(row["request_key"]),
        intent_digest=str(row["intent_digest"]),
        change_set_id=(
            None
            if row["change_set_id"] is None
            else str(row["change_set_id"])
        ),
        from_revision=_integer_value(row["from_revision"], "from_revision"),
        from_digest=str(row["from_digest"]),
        to_revision=(
            None
            if row["to_revision"] is None
            else _integer_value(row["to_revision"], "to_revision")
        ),
        to_digest=(
            None if row["to_digest"] is None else str(row["to_digest"])
        ),
        writer_id=str(row["writer_id"]),
        fencing_token=_integer_value(row["fencing_token"], "fencing_token"),
        state=WorkspaceMutationState(str(row["state"])),
        files=tuple(files),
        backup_ref=str(row["backup_ref"]),
        error_code=(
            None if row["error_code"] is None else str(row["error_code"])
        ),
        created_at=_timestamp_text(row["created_at"]),
        updated_at=_timestamp_text(row["updated_at"]),
    )


def _normalize_files(
    files: Sequence[WorkspaceMutationFile],
) -> tuple[WorkspaceMutationFile, ...]:
    if isinstance(files, (str, bytes)):
        raise TypeError("files must be a sequence of WorkspaceMutationFile")
    normalized = tuple(files)
    if not normalized or len(normalized) > 256:
        raise ValueError(
            "Workspace mutation files must contain 1 to 256 entries"
        )
    if any(not isinstance(item, WorkspaceMutationFile) for item in normalized):
        raise TypeError("files must contain WorkspaceMutationFile")
    if len({item.path for item in normalized}) != len(normalized):
        raise ValueError("Workspace mutation files contain duplicate paths")
    return tuple(sorted(normalized, key=lambda item: item.path))


def _mutation_id(workspace_id: str, request_key: str) -> str:
    digest = hashlib.sha256(
        f"{workspace_id}\0{request_key}".encode()
    ).hexdigest()
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
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _file_payload(item: WorkspaceMutationFile) -> dict[str, object]:
    return {
        "path": item.path,
        "operation": item.operation,
        "before_sha256": item.before_sha256,
        "after_sha256": item.after_sha256,
    }


def _migration_checksum(statements: tuple[str, ...]) -> str:
    digest = hashlib.sha256()
    for statement in statements:
        digest.update(statement.strip().encode())
        digest.update(b"\0")
    return digest.hexdigest()


def _dsn(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or any(character in value for character in "\r\n\0")
    ):
        raise ValueError("PostgreSQL DSN is invalid")
    return value


def _owner(value: str) -> str:
    if not is_safe_username(value):
        raise ValueError("Workspace owner is invalid")
    return value


def _key(value: str, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or "\0" in value
        or len(value) > 512
    ):
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


def _lease_seconds(value: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= 86_400
    ):
        raise ValueError("lease_seconds must be between 1 and 86400")
    return value


def _integer_value(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{label} is not an integer PostgreSQL value")
    return value


def _datetime_value(value: object, label: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{label} is invalid") from exc
    else:
        raise ValueError(f"{label} is invalid")
    if parsed.tzinfo is None:
        raise ValueError(f"{label} must be timezone-aware")
    return parsed.astimezone(UTC)


def _timestamp_text(value: object) -> str:
    return _datetime_value(value, "timestamp").isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")
