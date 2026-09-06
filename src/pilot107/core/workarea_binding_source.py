"""Durable provenance for WorkArea membership edges.

The historical WorkArea migrations (006c.002-004) predate the delivery design's
USER/INHERITED distinction and may already be checksum-frozen in deployed
PostgreSQL databases.  This additive authority records provenance without
rewriting those migrations or changing their edge primary keys.
"""

from __future__ import annotations

import hashlib
import importlib
from datetime import UTC, datetime
from typing import Any

from pilot107.core.postgres_domain_schema import initialize_postgres_domain_schema

_BINDING_KINDS = frozenset({"asset", "contract", "run", "agent_project"})
_BINDING_SOURCES = frozenset({"user", "inherited"})

_MIGRATION_ID = "006c.005.workarea_binding_sources"
_MIGRATION_STATEMENTS = (
    """
    CREATE TABLE workarea_binding_sources (
        workarea_id TEXT NOT NULL REFERENCES workareas(workarea_id) ON DELETE CASCADE,
        binding_kind TEXT NOT NULL,
        target_ref TEXT NOT NULL,
        source TEXT NOT NULL,
        linked_at TIMESTAMPTZ NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL,
        PRIMARY KEY (workarea_id, binding_kind, target_ref),
        CHECK (binding_kind IN ('asset', 'contract', 'run', 'agent_project')),
        CHECK (source IN ('user', 'inherited'))
    )
    """,
    """
    CREATE INDEX idx_workarea_binding_sources_target
    ON workarea_binding_sources(binding_kind, target_ref, workarea_id)
    """,
    """
    INSERT INTO workarea_binding_sources (
        workarea_id, binding_kind, target_ref, source, linked_at, updated_at
    )
    SELECT workarea_id, 'contract', contract_id, 'user', linked_at, linked_at
    FROM workarea_contracts
    ON CONFLICT DO NOTHING
    """,
    """
    INSERT INTO workarea_binding_sources (
        workarea_id, binding_kind, target_ref, source, linked_at, updated_at
    )
    SELECT workarea_id, 'run', run_id, 'user', linked_at, linked_at
    FROM workarea_runs
    ON CONFLICT DO NOTHING
    """,
    """
    INSERT INTO workarea_binding_sources (
        workarea_id, binding_kind, target_ref, source, linked_at, updated_at
    )
    SELECT workarea_id, 'agent_project', project_id, 'user', linked_at, linked_at
    FROM workarea_agent_projects
    ON CONFLICT DO NOTHING
    """,
    """
    INSERT INTO workarea_binding_sources (
        workarea_id, binding_kind, target_ref, source, linked_at, updated_at
    )
    SELECT workarea_id, 'asset', asset_ref, 'user', linked_at, linked_at
    FROM workarea_assets
    ON CONFLICT DO NOTHING
    """,
)


class PostgresWorkAreaBindingSourceStore:
    """PostgreSQL authority for how a WorkArea edge entered the context."""

    def __init__(self, dsn: str) -> None:
        if not dsn or any(character in dsn for character in "\r\n\0"):
            raise ValueError("PostgreSQL DSN is invalid")
        self.dsn = dsn
        try:
            self._psycopg = importlib.import_module("psycopg")
            self._dict_row = importlib.import_module("psycopg.rows").dict_row
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "install pilot107[postgres] to use WorkArea binding provenance"
            ) from exc
        initialize_postgres_domain_schema(dsn)
        self._ensure_schema()

    def connect(self) -> Any:
        return self._psycopg.connect(self.dsn, row_factory=self._dict_row)

    def mark(
        self,
        *,
        workarea_id: str,
        binding_kind: str,
        target_ref: str,
        source: str,
    ) -> str:
        """Record provenance, with explicit user selection taking precedence.

        An inherited edge can later be promoted to ``user`` when the person
        explicitly binds the same object.  Automatic inheritance can never
        downgrade an already-explicit edge.
        """

        _binding(binding_kind, target_ref, source)
        now = datetime.now(UTC)
        with self.connect() as connection, connection.transaction():
            row = connection.execute(
                """
                INSERT INTO workarea_binding_sources (
                    workarea_id, binding_kind, target_ref, source, linked_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (workarea_id, binding_kind, target_ref) DO UPDATE
                SET source = CASE
                        WHEN workarea_binding_sources.source = 'user' THEN 'user'
                        WHEN EXCLUDED.source = 'user' THEN 'user'
                        ELSE 'inherited'
                    END,
                    updated_at = EXCLUDED.updated_at
                RETURNING source
                """,
                (workarea_id, binding_kind, target_ref, source, now, now),
            ).fetchone()
        if row is None:
            raise RuntimeError("WorkArea binding provenance write disappeared")
        return str(row["source"])

    def source_for(
        self,
        *,
        workarea_id: str,
        binding_kind: str,
        target_ref: str,
    ) -> str:
        _binding(binding_kind, target_ref, "user")
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT source FROM workarea_binding_sources
                WHERE workarea_id = %s AND binding_kind = %s AND target_ref = %s
                """,
                (workarea_id, binding_kind, target_ref),
            ).fetchone()
        # Rows that somehow predate the additive migration are conservatively
        # treated as explicit historical bindings, matching the migration backfill.
        return "user" if row is None else str(row["source"])

    def sources_for_workarea(self, workarea_id: str) -> dict[tuple[str, str], str]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT binding_kind, target_ref, source
                FROM workarea_binding_sources
                WHERE workarea_id = %s
                """,
                (workarea_id,),
            ).fetchall()
        return {
            (str(row["binding_kind"]), str(row["target_ref"])): str(row["source"])
            for row in rows
        }

    def _ensure_schema(self) -> None:
        checksum = _migration_checksum(_MIGRATION_STATEMENTS)
        with self.connect() as connection, connection.transaction():
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                ("pilot107:migrations",),
            )
            existing = connection.execute(
                "SELECT checksum FROM schema_migrations WHERE migration_id = %s",
                (_MIGRATION_ID,),
            ).fetchone()
            if existing is not None:
                if str(existing["checksum"]) != checksum:
                    raise RuntimeError(f"migration checksum changed: {_MIGRATION_ID}")
                return
            for statement in _MIGRATION_STATEMENTS:
                connection.execute(statement)
            connection.execute(
                """
                INSERT INTO schema_migrations (migration_id, checksum, applied_at)
                VALUES (%s, %s, %s)
                """,
                (_MIGRATION_ID, checksum, datetime.now(UTC)),
            )


def _binding(kind: str, target_ref: str, source: str) -> None:
    if kind not in _BINDING_KINDS:
        raise ValueError(f"unsupported WorkArea binding kind: {kind}")
    if source not in _BINDING_SOURCES:
        raise ValueError("WorkArea binding source must be user or inherited")
    if not isinstance(target_ref, str) or not target_ref.strip() or "\0" in target_ref:
        raise ValueError("WorkArea binding target_ref is invalid")
    if len(target_ref) > 4_096:
        raise ValueError("WorkArea binding target_ref is too long")


def _migration_checksum(statements: tuple[str, ...]) -> str:
    digest = hashlib.sha256()
    for statement in statements:
        digest.update(statement.strip().encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


__all__ = ["PostgresWorkAreaBindingSourceStore"]
