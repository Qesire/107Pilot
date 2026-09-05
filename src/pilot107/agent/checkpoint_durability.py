"""Database invariants for durable Agent checkpoint pointers.

A checkpoint event is the durable commit record. Recovery must therefore be able
to read the same checkpoint from ``agent_turns.final_checkpoint_json`` even when
the worker dies immediately after appending the event. PostgreSQL installs these
invariants as additive checksum-verified migrations; existing domain migration
checksums remain untouched.
"""

from __future__ import annotations

import hashlib
import importlib
from datetime import UTC, datetime
from typing import Any

from pilot107.core.postgres_control_repository import PostgresDriverUnavailable

POSTGRES_CHECKPOINT_POINTER_MIGRATION_ID = "006a.002.agent_checkpoint_pointer"
POSTGRES_CHECKPOINT_POINTER_IDENTITY_MIGRATION_ID = "006a.003.agent_checkpoint_pointer_identity"

# Frozen after 006a.002 shipped. Do not alter these statements: deployed
# databases verify this checksum before any later durability migration runs.
_POSTGRES_CHECKPOINT_POINTER_STATEMENTS = (
    """
    UPDATE agent_turns AS target
    SET final_checkpoint_json = (
        SELECT event.payload_json -> 'checkpoint'
        FROM agent_turn_events AS event
        WHERE event.turn_id = target.turn_id
          AND event.event_type = 'checkpoint'
          AND jsonb_typeof(event.payload_json -> 'checkpoint') = 'object'
        ORDER BY event.sequence DESC
        LIMIT 1
    )
    WHERE target.state IN ('queued', 'running', 'interrupted')
      AND EXISTS (
          SELECT 1
          FROM agent_turn_events AS event
          WHERE event.turn_id = target.turn_id
            AND event.event_type = 'checkpoint'
            AND jsonb_typeof(event.payload_json -> 'checkpoint') = 'object'
      )
    """,
    """
    CREATE OR REPLACE FUNCTION pilot107_agent_checkpoint_pointer()
    RETURNS TRIGGER
    LANGUAGE plpgsql
    AS $$
    BEGIN
        IF jsonb_typeof(NEW.payload_json -> 'checkpoint') IS DISTINCT FROM 'object' THEN
            RAISE EXCEPTION 'checkpoint event must contain checkpoint object';
        END IF;
        UPDATE agent_turns
        SET final_checkpoint_json = NEW.payload_json -> 'checkpoint'
        WHERE turn_id = NEW.turn_id;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'checkpoint event refers to missing Turn';
        END IF;
        RETURN NEW;
    END;
    $$
    """,
    "DROP TRIGGER IF EXISTS agent_turn_checkpoint_pointer ON agent_turn_events",
    """
    CREATE TRIGGER agent_turn_checkpoint_pointer
    AFTER INSERT ON agent_turn_events
    FOR EACH ROW
    WHEN (NEW.event_type = 'checkpoint')
    EXECUTE FUNCTION pilot107_agent_checkpoint_pointer()
    """,
)

_POSTGRES_CHECKPOINT_POINTER_IDENTITY_STATEMENTS = (
    """
    UPDATE agent_turns AS target
    SET final_checkpoint_json = (
        SELECT event.payload_json -> 'checkpoint'
        FROM agent_turn_events AS event
        WHERE event.turn_id = target.turn_id
          AND event.session_id = target.session_id
          AND event.owner = target.owner
          AND event.event_type = 'checkpoint'
          AND jsonb_typeof(event.payload_json -> 'checkpoint') = 'object'
        ORDER BY event.sequence DESC
        LIMIT 1
    )
    WHERE target.state IN ('queued', 'running', 'interrupted')
      AND EXISTS (
          SELECT 1
          FROM agent_turn_events AS event
          WHERE event.turn_id = target.turn_id
            AND event.event_type = 'checkpoint'
      )
    """,
    """
    CREATE OR REPLACE FUNCTION pilot107_agent_checkpoint_pointer()
    RETURNS TRIGGER
    LANGUAGE plpgsql
    AS $$
    BEGIN
        IF jsonb_typeof(NEW.payload_json -> 'checkpoint') IS DISTINCT FROM 'object' THEN
            RAISE EXCEPTION 'checkpoint event must contain checkpoint object';
        END IF;
        UPDATE agent_turns
        SET final_checkpoint_json = NEW.payload_json -> 'checkpoint'
        WHERE turn_id = NEW.turn_id
          AND session_id = NEW.session_id
          AND owner = NEW.owner;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'checkpoint event identity does not match Turn';
        END IF;
        RETURN NEW;
    END;
    $$
    """,
    "DROP TRIGGER IF EXISTS agent_turn_checkpoint_pointer ON agent_turn_events",
    """
    CREATE TRIGGER agent_turn_checkpoint_pointer
    AFTER INSERT ON agent_turn_events
    FOR EACH ROW
    WHEN (NEW.event_type = 'checkpoint')
    EXECUTE FUNCTION pilot107_agent_checkpoint_pointer()
    """,
)

_POSTGRES_CHECKPOINT_POINTER_MIGRATIONS = (
    (POSTGRES_CHECKPOINT_POINTER_MIGRATION_ID, _POSTGRES_CHECKPOINT_POINTER_STATEMENTS),
    (
        POSTGRES_CHECKPOINT_POINTER_IDENTITY_MIGRATION_ID,
        _POSTGRES_CHECKPOINT_POINTER_IDENTITY_STATEMENTS,
    ),
)


class CheckpointDurabilityMigrationError(RuntimeError):
    """Raised when the checkpoint-pointer migration history is not trustworthy."""


def ensure_postgres_checkpoint_pointer(dsn: str) -> bool:
    """Install/verify all checkpoint-pointer invariants and report any application."""

    if not dsn or any(character in dsn for character in "\r\n\0"):
        raise ValueError("PostgreSQL DSN is invalid")
    psycopg, dict_row = _load_psycopg()
    applied = False
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
        for migration_id, statements in _POSTGRES_CHECKPOINT_POINTER_MIGRATIONS:
            checksum = _migration_checksum(statements)
            existing = conn.execute(
                "SELECT checksum FROM schema_migrations WHERE migration_id = %s",
                (migration_id,),
            ).fetchone()
            if existing is not None:
                if str(existing["checksum"]) != checksum:
                    raise CheckpointDurabilityMigrationError(
                        f"migration checksum changed: {migration_id}"
                    )
                continue
            for statement in statements:
                conn.execute(statement)
            conn.execute(
                """
                INSERT INTO schema_migrations (migration_id, checksum, applied_at)
                VALUES (%s, %s, %s)
                """,
                (
                    migration_id,
                    checksum,
                    datetime.now(UTC),
                ),
            )
            applied = True
    return applied


def _load_psycopg() -> tuple[Any, Any]:
    try:
        return (
            importlib.import_module("psycopg"),
            importlib.import_module("psycopg.rows").dict_row,
        )
    except ModuleNotFoundError as exc:
        raise PostgresDriverUnavailable(
            "install pilot107[postgres] to use PostgreSQL Agent repositories"
        ) from exc


def _migration_checksum(statements: tuple[str, ...]) -> str:
    digest = hashlib.sha256()
    for statement in statements:
        digest.update(statement.strip().encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()
