"""User-owned Research Workspace and explicit experiment-context bindings.

A Research Workspace is a logical context chosen by the user.  It does not
infer scientific identity from paths, Contract similarity, or Agent output.
Only explicit user binding, user-approved Agent suggestion, or deterministic
lineage inheritance can attach an object to a workspace.
"""

from __future__ import annotations

import re
import sqlite3
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from pilot107.core.schema_migrations import SchemaMigration, apply_schema_migrations

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/@+-]{0,511}$")


class ResearchWorkspaceConflict(RuntimeError):
    """Raised when a workspace binding violates ownership or provenance rules."""


class WorkspaceObjectType(StrEnum):
    FILE_REFERENCE = "file_reference"
    CONTRACT = "contract"
    RUN = "run"
    REMEDIATION_SESSION = "remediation_session"
    AGENT_SESSION = "agent_session"
    AGENT_PROJECT = "agent_project"


class WorkspaceBindingSource(StrEnum):
    USER = "user"
    INHERITED = "inherited"
    APPROVED_AGENT_SUGGESTION = "approved_agent_suggestion"


@dataclass(frozen=True)
class ResearchWorkspaceRecord:
    workspace_id: str
    owner: str
    title: str
    description: str
    request_key: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class WorkspaceBindingRecord:
    binding_id: str
    workspace_id: str
    owner: str
    object_type: WorkspaceObjectType
    object_id: str
    source: WorkspaceBindingSource
    source_ref: str | None
    parent_binding_id: str | None
    created_by: str
    created_at: str


RESEARCH_WORKSPACE_MIGRATIONS = (
    SchemaMigration(
        migration_id="007a.001.research_workspaces",
        statements=(
            """
            CREATE TABLE research_workspaces (
                workspace_id TEXT PRIMARY KEY,
                owner TEXT NOT NULL,
                title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                request_key TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(owner, request_key)
            )
            """,
            """
            CREATE INDEX idx_research_workspaces_owner_updated
            ON research_workspaces(owner, updated_at DESC, workspace_id DESC)
            """,
            """
            CREATE TABLE research_workspace_bindings (
                binding_id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL REFERENCES research_workspaces(workspace_id)
                    ON DELETE CASCADE,
                owner TEXT NOT NULL,
                object_type TEXT NOT NULL,
                object_id TEXT NOT NULL,
                source TEXT NOT NULL,
                source_ref TEXT,
                parent_binding_id TEXT REFERENCES research_workspace_bindings(binding_id),
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(workspace_id, object_type, object_id),
                CHECK (object_type IN (
                    'file_reference', 'contract', 'run', 'remediation_session',
                    'agent_session', 'agent_project'
                )),
                CHECK (source IN ('user', 'inherited', 'approved_agent_suggestion'))
            )
            """,
            """
            CREATE INDEX idx_research_workspace_bindings_workspace
            ON research_workspace_bindings(workspace_id, created_at, binding_id)
            """,
            """
            CREATE INDEX idx_research_workspace_bindings_object
            ON research_workspace_bindings(owner, object_type, object_id, created_at)
            """,
        ),
    ),
)


_INHERITANCE: dict[WorkspaceObjectType, frozenset[WorkspaceObjectType]] = {
    WorkspaceObjectType.CONTRACT: frozenset(
        {WorkspaceObjectType.CONTRACT, WorkspaceObjectType.RUN}
    ),
    WorkspaceObjectType.RUN: frozenset(
        {
            WorkspaceObjectType.RUN,
            WorkspaceObjectType.REMEDIATION_SESSION,
            WorkspaceObjectType.AGENT_SESSION,
            WorkspaceObjectType.AGENT_PROJECT,
        }
    ),
    WorkspaceObjectType.REMEDIATION_SESSION: frozenset(
        {
            WorkspaceObjectType.AGENT_SESSION,
            WorkspaceObjectType.AGENT_PROJECT,
            WorkspaceObjectType.RUN,
        }
    ),
    WorkspaceObjectType.AGENT_SESSION: frozenset({WorkspaceObjectType.AGENT_PROJECT}),
    WorkspaceObjectType.AGENT_PROJECT: frozenset({WorkspaceObjectType.RUN}),
    WorkspaceObjectType.FILE_REFERENCE: frozenset(),
}


def can_inherit_workspace_binding(
    parent_type: WorkspaceObjectType,
    child_type: WorkspaceObjectType,
) -> bool:
    return child_type in _INHERITANCE[parent_type]


class SQLiteResearchWorkspaceStore:
    """SQLite authority for user-selected Research Workspace membership."""

    def __init__(
        self,
        db_path: Path,
        *,
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._id_factory = id_factory or (lambda: str(uuid.uuid4()))
        with self.connect() as conn:
            apply_schema_migrations(conn, RESEARCH_WORKSPACE_MIGRATIONS)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 10000")
        return conn

    def create_workspace(
        self,
        *,
        owner: str,
        request_key: str,
        title: str,
        description: str = "",
    ) -> tuple[ResearchWorkspaceRecord, bool]:
        _identifier(owner, "owner")
        _identifier(request_key, "request_key")
        _title(title)
        if len(description) > 4096:
            raise ValueError("description is too long")
        workspace_id = f"research-{self._id_factory()}"
        now = self._now()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO research_workspaces (
                    workspace_id, owner, title, description, request_key,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (workspace_id, owner, title.strip(), description, request_key, now, now),
            )
            row = conn.execute(
                """
                SELECT * FROM research_workspaces
                WHERE owner = ? AND request_key = ?
                """,
                (owner, request_key),
            ).fetchone()
        if row is None:
            raise RuntimeError("workspace creation did not produce a row")
        record = _workspace(row)
        if record.title != title.strip() or record.description != description:
            raise ResearchWorkspaceConflict(
                "request_key refers to different Research Workspace content"
            )
        return record, cursor.rowcount == 1

    def get_workspace(
        self, workspace_id: str, *, owner: str
    ) -> ResearchWorkspaceRecord:
        _identifier(workspace_id, "workspace_id")
        _identifier(owner, "owner")
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM research_workspaces
                WHERE workspace_id = ? AND owner = ?
                """,
                (workspace_id, owner),
            ).fetchone()
        if row is None:
            raise KeyError(workspace_id)
        return _workspace(row)

    def list_workspaces(self, *, owner: str) -> list[ResearchWorkspaceRecord]:
        _identifier(owner, "owner")
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM research_workspaces
                WHERE owner = ? ORDER BY updated_at DESC, workspace_id DESC
                """,
                (owner,),
            ).fetchall()
        return [_workspace(row) for row in rows]

    def bind_user_selected(
        self,
        *,
        workspace_id: str,
        owner: str,
        object_type: WorkspaceObjectType,
        object_id: str,
        actor: str,
    ) -> tuple[WorkspaceBindingRecord, bool]:
        if actor != owner:
            raise ResearchWorkspaceConflict(
                "only the workspace owner can create an explicit binding"
            )
        return self._bind(
            workspace_id=workspace_id,
            owner=owner,
            object_type=object_type,
            object_id=object_id,
            source=WorkspaceBindingSource.USER,
            source_ref=None,
            parent_binding_id=None,
            actor=actor,
        )

    def bind_approved_agent_suggestion(
        self,
        *,
        workspace_id: str,
        owner: str,
        object_type: WorkspaceObjectType,
        object_id: str,
        actor: str,
        suggestion_ref: str,
    ) -> tuple[WorkspaceBindingRecord, bool]:
        if actor != owner:
            raise ResearchWorkspaceConflict(
                "Agent binding suggestions require owner approval"
            )
        _identifier(suggestion_ref, "suggestion_ref")
        return self._bind(
            workspace_id=workspace_id,
            owner=owner,
            object_type=object_type,
            object_id=object_id,
            source=WorkspaceBindingSource.APPROVED_AGENT_SUGGESTION,
            source_ref=suggestion_ref,
            parent_binding_id=None,
            actor=actor,
        )

    def inherit_binding(
        self,
        *,
        parent_binding_id: str,
        owner: str,
        child_type: WorkspaceObjectType,
        child_id: str,
        actor: str = "system",
    ) -> tuple[WorkspaceBindingRecord, bool]:
        _identifier(parent_binding_id, "parent_binding_id")
        with self.connect() as conn:
            parent_row = conn.execute(
                """
                SELECT * FROM research_workspace_bindings
                WHERE binding_id = ? AND owner = ?
                """,
                (parent_binding_id, owner),
            ).fetchone()
        if parent_row is None:
            raise KeyError(parent_binding_id)
        parent = _binding(parent_row)
        if not can_inherit_workspace_binding(parent.object_type, child_type):
            raise ResearchWorkspaceConflict(
                f"workspace binding cannot inherit {parent.object_type} -> {child_type}"
            )
        return self._bind(
            workspace_id=parent.workspace_id,
            owner=owner,
            object_type=child_type,
            object_id=child_id,
            source=WorkspaceBindingSource.INHERITED,
            source_ref=None,
            parent_binding_id=parent.binding_id,
            actor=actor,
        )

    def list_bindings(
        self, workspace_id: str, *, owner: str
    ) -> list[WorkspaceBindingRecord]:
        self.get_workspace(workspace_id, owner=owner)
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM research_workspace_bindings
                WHERE workspace_id = ? AND owner = ?
                ORDER BY created_at, binding_id
                """,
                (workspace_id, owner),
            ).fetchall()
        return [_binding(row) for row in rows]

    def find_object_workspaces(
        self,
        *,
        owner: str,
        object_type: WorkspaceObjectType,
        object_id: str,
    ) -> list[ResearchWorkspaceRecord]:
        _identifier(owner, "owner")
        _identifier(object_id, "object_id")
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT w.*
                FROM research_workspaces AS w
                JOIN research_workspace_bindings AS b
                  ON b.workspace_id = w.workspace_id
                WHERE b.owner = ? AND b.object_type = ? AND b.object_id = ?
                ORDER BY w.updated_at DESC, w.workspace_id DESC
                """,
                (owner, object_type.value, object_id),
            ).fetchall()
        return [_workspace(row) for row in rows]

    def _bind(
        self,
        *,
        workspace_id: str,
        owner: str,
        object_type: WorkspaceObjectType,
        object_id: str,
        source: WorkspaceBindingSource,
        source_ref: str | None,
        parent_binding_id: str | None,
        actor: str,
    ) -> tuple[WorkspaceBindingRecord, bool]:
        self.get_workspace(workspace_id, owner=owner)
        _identifier(object_id, "object_id")
        _identifier(actor, "actor")
        binding_id = f"binding-{self._id_factory()}"
        now = self._now()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO research_workspace_bindings (
                    binding_id, workspace_id, owner, object_type, object_id,
                    source, source_ref, parent_binding_id, created_by, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    binding_id,
                    workspace_id,
                    owner,
                    object_type.value,
                    object_id,
                    source.value,
                    source_ref,
                    parent_binding_id,
                    actor,
                    now,
                ),
            )
            row = conn.execute(
                """
                SELECT * FROM research_workspace_bindings
                WHERE workspace_id = ? AND object_type = ? AND object_id = ?
                """,
                (workspace_id, object_type.value, object_id),
            ).fetchone()
            if cursor.rowcount == 1:
                conn.execute(
                    "UPDATE research_workspaces SET updated_at = ? WHERE workspace_id = ?",
                    (now, workspace_id),
                )
        if row is None:
            raise RuntimeError("workspace binding did not produce a row")
        record = _binding(row)
        if (
            record.owner != owner
            or record.source != source
            or record.source_ref != source_ref
            or record.parent_binding_id != parent_binding_id
        ):
            raise ResearchWorkspaceConflict(
                "object is already bound to this workspace with different provenance"
            )
        return record, cursor.rowcount == 1

    def _now(self) -> str:
        value = self._clock()
        if value.tzinfo is None:
            raise ValueError("Research Workspace clock must be timezone-aware")
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _workspace(row: sqlite3.Row) -> ResearchWorkspaceRecord:
    return ResearchWorkspaceRecord(
        workspace_id=str(row["workspace_id"]),
        owner=str(row["owner"]),
        title=str(row["title"]),
        description=str(row["description"]),
        request_key=str(row["request_key"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _binding(row: sqlite3.Row) -> WorkspaceBindingRecord:
    return WorkspaceBindingRecord(
        binding_id=str(row["binding_id"]),
        workspace_id=str(row["workspace_id"]),
        owner=str(row["owner"]),
        object_type=WorkspaceObjectType(str(row["object_type"])),
        object_id=str(row["object_id"]),
        source=WorkspaceBindingSource(str(row["source"])),
        source_ref=None if row["source_ref"] is None else str(row["source_ref"]),
        parent_binding_id=(
            None if row["parent_binding_id"] is None else str(row["parent_binding_id"])
        ),
        created_by=str(row["created_by"]),
        created_at=str(row["created_at"]),
    )


def _identifier(value: str, label: str) -> None:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{label} is invalid")


def _title(value: str) -> None:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 200:
        raise ValueError("title must contain 1 to 200 characters")
