"""PostgreSQL parity for the user-selected Research Workspace authority."""

from __future__ import annotations

import hashlib
import importlib
import sqlite3
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from pilot107.core.postgres_control_repository import PostgresDriverUnavailable
from pilot107.core.postgres_domain_stores import _PostgresDomainStore
from pilot107.core.research_workspace import SQLiteResearchWorkspaceStore

_POSTGRES_RESEARCH_WORKSPACE_SCHEMA = (
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
    """.strip(),
    """
    CREATE INDEX idx_research_workspaces_owner_updated
    ON research_workspaces(owner, updated_at DESC, workspace_id DESC)
    """.strip(),
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
    """.strip(),
    """
    CREATE INDEX idx_research_workspace_bindings_workspace
    ON research_workspace_bindings(workspace_id, created_at, binding_id)
    """.strip(),
    """
    CREATE INDEX idx_research_workspace_bindings_object
    ON research_workspace_bindings(owner, object_type, object_id, created_at)
    """.strip(),
)

_MIGRATION_ID = "007a.101.pg_research_workspaces"


class PostgresResearchWorkspaceStore(_PostgresDomainStore, SQLiteResearchWorkspaceStore):
    """Run the same binding rules over the native PostgreSQL domain adapter."""

    def __init__(
        self,
        dsn: str,
        *,
        compatibility_path: Path,
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._clock = clock or (lambda: datetime.now(UTC))
        self._id_factory = id_factory or (lambda: str(uuid.uuid4()))
        self._configure_postgres(dsn=dsn, compatibility_path=compatibility_path)
        _initialize_research_workspace_schema(dsn)

    def connect(self) -> sqlite3.Connection:
        return cast(sqlite3.Connection, self._postgres_connect())


def _initialize_research_workspace_schema(dsn: str) -> None:
    try:
        psycopg = importlib.import_module("psycopg")
        dict_row = importlib.import_module("psycopg.rows").dict_row
    except ModuleNotFoundError as exc:
        raise PostgresDriverUnavailable(
            "install pilot107[postgres] to use PostgreSQL Research Workspaces"
        ) from exc

    checksum = hashlib.sha256(
        "\n-- statement\n".join(_POSTGRES_RESEARCH_WORKSPACE_SCHEMA).encode("utf-8")
    ).hexdigest()
    with psycopg.connect(dsn, row_factory=dict_row) as conn, conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("pilot107:migrations",))
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                migration_id TEXT PRIMARY KEY,
                checksum TEXT NOT NULL,
                applied_at TIMESTAMPTZ NOT NULL
            )
            """
        )
        existing = conn.execute(
            "SELECT checksum FROM schema_migrations WHERE migration_id = %s",
            (_MIGRATION_ID,),
        ).fetchone()
        if existing is not None:
            if str(existing["checksum"]) != checksum:
                raise RuntimeError("Research Workspace PostgreSQL migration checksum drift")
            return
        for statement in _POSTGRES_RESEARCH_WORKSPACE_SCHEMA:
            conn.execute(statement)
        conn.execute(
            """
            INSERT INTO schema_migrations (migration_id, checksum, applied_at)
            VALUES (%s, %s, %s)
            """,
            (_MIGRATION_ID, checksum, datetime.now(UTC)),
        )


__all__ = ["PostgresResearchWorkspaceStore"]
