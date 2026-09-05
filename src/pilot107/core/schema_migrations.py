"""Small, checksum-verified SQLite schema migration runner."""

from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

_MIGRATION_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")


class SchemaMigrationError(RuntimeError):
    """Raised when a migration history is invalid or cannot be applied."""


@dataclass(frozen=True)
class SchemaMigration:
    migration_id: str
    statements: tuple[str, ...]

    def __post_init__(self) -> None:
        if not _MIGRATION_ID.fullmatch(self.migration_id):
            raise ValueError("migration_id is invalid")
        if not self.statements or any(not statement.strip() for statement in self.statements):
            raise ValueError("migration statements must not be empty")

    @property
    def checksum(self) -> str:
        digest = hashlib.sha256()
        for statement in self.statements:
            digest.update(statement.strip().encode("utf-8"))
            digest.update(b"\0")
        return digest.hexdigest()


def apply_schema_migrations(
    conn: sqlite3.Connection,
    migrations: tuple[SchemaMigration, ...],
) -> tuple[str, ...]:
    """Apply ordered migrations and return IDs newly applied by this call."""

    migration_ids = [migration.migration_id for migration in migrations]
    if len(migration_ids) != len(set(migration_ids)):
        raise SchemaMigrationError("migration IDs must be unique")
    if migration_ids != sorted(migration_ids):
        raise SchemaMigrationError("migrations must be ordered by migration_id")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            migration_id TEXT PRIMARY KEY,
            checksum TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )
        """
    )
    conn.commit()

    applied_now: list[str] = []
    for migration in migrations:
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT checksum FROM schema_migrations WHERE migration_id = ?",
                (migration.migration_id,),
            ).fetchone()
            if row is not None:
                existing_checksum = str(row[0])
                if existing_checksum != migration.checksum:
                    raise SchemaMigrationError(
                        f"migration checksum changed: {migration.migration_id}"
                    )
                conn.commit()
                continue
            for statement in migration.statements:
                conn.execute(statement)
            conn.execute(
                "INSERT INTO schema_migrations (migration_id, checksum, applied_at) "
                "VALUES (?, ?, ?)",
                (
                    migration.migration_id,
                    migration.checksum,
                    datetime.now(UTC).isoformat(),
                ),
            )
            conn.commit()
            applied_now.append(migration.migration_id)
        except Exception:
            conn.rollback()
            raise
    return tuple(applied_now)

