"""Quiesced, checksum-verified SQLite-to-PostgreSQL domain migration."""

from __future__ import annotations

import hashlib
import importlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pilot107.core.postgres_control_repository import (
    PostgresControlRepository,
    PostgresDriverUnavailable,
)
from pilot107.core.postgres_domain_schema import (
    initialize_postgres_domain_schema,
    persisted_table_names,
    serial_primary_keys,
)


class DomainDataMigrationError(RuntimeError):
    """Raised if a migration cannot prove its source and target are identical."""


_TIMESTAMPTZ_COLUMNS: dict[str, frozenset[str]] = {
    "control_leases": frozenset({"expires_at", "updated_at"}),
    "control_outbox": frozenset(
        {"available_at", "lease_expires_at", "created_at", "updated_at"}
    ),
    "control_traces": frozenset({"created_at"}),
    "agent_sessions": frozenset({"created_at", "updated_at"}),
    "agent_turns": frozenset(
        {"lease_expires_at", "created_at", "started_at", "finished_at"}
    ),
    "agent_turn_events": frozenset({"created_at"}),
    "agent_tool_invocations": frozenset({"created_at", "updated_at"}),
    "agent_experiment_projects": frozenset({"created_at", "updated_at"}),
    "agent_workspaces": frozenset({"created_at", "updated_at"}),
}

_JSONB_COLUMNS: dict[str, frozenset[str]] = {
    "control_outbox": frozenset({"payload_json"}),
    "agent_sessions": frozenset(
        {
            "source_json",
            "context_checkpoint_json",
            "resource_usage_json",
            "outcome_json",
        }
    ),
    "agent_turns": frozenset({"final_checkpoint_json", "error_json"}),
    "agent_turn_events": frozenset({"payload_json"}),
    "agent_tool_invocations": frozenset({"result_json", "error_json"}),
    "agent_experiment_projects": frozenset({"source_json", "blueprint_json"}),
    "agent_workspaces": frozenset({"payload_json"}),
}


@dataclass(frozen=True)
class DomainDataMigrationReport:
    source_tables: dict[str, int]
    target_tables: dict[str, int]
    source_digest: str
    target_digest: str
    transferred: bool
    already_complete: bool


def migrate_sqlite_domain_to_postgres(
    *,
    sqlite_path: Path,
    postgres_dsn: str,
    source_quiesced: bool,
) -> DomainDataMigrationReport:
    """Copy all business records to an empty PG database and verify every row.

    The caller must stop API and Worker writers first.  This function never
    truncates PostgreSQL and never mutates the SQLite source, so returning to
    SQLite is a configuration rollback rather than a data-recovery event.
    """

    if not source_quiesced:
        raise DomainDataMigrationError(
            "source_quiesced must be true after API and Worker writers are stopped"
        )
    if not sqlite_path.is_file():
        raise DomainDataMigrationError("SQLite source database does not exist")

    _initialize_target_schema(postgres_dsn)
    psycopg, dict_row = _load_psycopg()
    source = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    source.row_factory = sqlite3.Row
    try:
        source_tables = _existing_source_tables(source)
        source_counts, source_digest = _fingerprint_sqlite(source, source_tables)
        with psycopg.connect(postgres_dsn, row_factory=dict_row) as target, target.transaction():
            target.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                ("pilot107:sqlite-to-postgres",),
            )
            target_counts, target_digest = _fingerprint_postgres(target, source_tables)
            if any(target_counts.values()):
                if target_counts == source_counts and target_digest == source_digest:
                    return DomainDataMigrationReport(
                        source_tables=source_counts,
                        target_tables=target_counts,
                        source_digest=source_digest,
                        target_digest=target_digest,
                        transferred=False,
                        already_complete=True,
                    )
                raise DomainDataMigrationError(
                    "PostgreSQL target is not empty and does not exactly match the SQLite source"
                )

            for table in persisted_table_names():
                if table not in source_tables:
                    continue
                _copy_table(source, target, table)
            _set_serial_sequences(target)
            target_counts, target_digest = _fingerprint_postgres(target, source_tables)
            if target_counts != source_counts or target_digest != source_digest:
                mismatched_tables = _mismatched_table_names(
                    source,
                    target,
                    source_tables,
                )
                raise DomainDataMigrationError(
                    "PostgreSQL verification mismatch after transfer for tables: "
                    + ", ".join(mismatched_tables)
                    + "; transaction was rolled back"
                )
    finally:
        source.close()

    return DomainDataMigrationReport(
        source_tables=source_counts,
        target_tables=target_counts,
        source_digest=source_digest,
        target_digest=target_digest,
        transferred=True,
        already_complete=False,
    )


def verify_sqlite_domain_matches_postgres(
    *,
    sqlite_path: Path,
    postgres_dsn: str,
) -> DomainDataMigrationReport:
    """Read-only source/target comparison used before and after cutover."""

    if not sqlite_path.is_file():
        raise DomainDataMigrationError("SQLite source database does not exist")
    _initialize_target_schema(postgres_dsn)
    psycopg, dict_row = _load_psycopg()
    source = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    source.row_factory = sqlite3.Row
    try:
        source_tables = _existing_source_tables(source)
        source_counts, source_digest = _fingerprint_sqlite(source, source_tables)
        with psycopg.connect(postgres_dsn, row_factory=dict_row) as target:
            target_counts, target_digest = _fingerprint_postgres(target, source_tables)
    finally:
        source.close()
    if source_counts != target_counts or source_digest != target_digest:
        raise DomainDataMigrationError("SQLite and PostgreSQL domain data do not match")
    return DomainDataMigrationReport(
        source_tables=source_counts,
        target_tables=target_counts,
        source_digest=source_digest,
        target_digest=target_digest,
        transferred=False,
        already_complete=True,
    )


def _existing_source_tables(conn: sqlite3.Connection) -> frozenset[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    names = {str(row["name"]) for row in rows}
    return frozenset(table for table in persisted_table_names() if table in names)


def _copy_table(source: sqlite3.Connection, target: Any, table: str) -> None:
    cursor = source.execute(f"SELECT * FROM {table}")
    rows = cursor.fetchall()
    if not rows:
        return
    # sqlite3.Row iteration yields *values*, not column names.  Cursor
    # metadata preserves the source table's declared field order.
    columns = tuple(str(column[0]) for column in cursor.description or ())
    placeholders = ", ".join("%s" for _ in columns)
    statement = f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})"
    values = [tuple(row[column] for column in columns) for row in rows]
    target.cursor().executemany(statement, values)


def _set_serial_sequences(target: Any) -> None:
    for table, column in serial_primary_keys():
        target.execute(
            "SELECT setval(pg_get_serial_sequence(%s, %s), "
            f"COALESCE((SELECT MAX({column}) FROM {table}), 1), "
            f"(SELECT COUNT(*) > 0 FROM {table}))",
            (table, column),
        )


def _fingerprint_sqlite(
    conn: sqlite3.Connection,
    tables: frozenset[str],
) -> tuple[dict[str, int], str]:
    return _fingerprint_rows(
        (
            table,
            (
                _rows_as_dicts(conn.execute(f"SELECT * FROM {table}").fetchall())
                if table in tables
                else []
            ),
        )
        for table in persisted_table_names()
    )


def _fingerprint_postgres(
    conn: Any,
    tables: frozenset[str],
) -> tuple[dict[str, int], str]:
    del tables  # PostgreSQL has the complete native schema after initialization.
    return _fingerprint_rows(
        (table, _rows_as_dicts(conn.execute(f"SELECT * FROM {table}").fetchall()))
        for table in persisted_table_names()
    )


def _mismatched_table_names(
    source: sqlite3.Connection,
    target: Any,
    source_tables: frozenset[str],
) -> tuple[str, ...]:
    """Identify divergent tables without exposing migrated row values."""

    mismatched: list[str] = []
    for table in persisted_table_names():
        source_rows = (
            _rows_as_dicts(source.execute(f"SELECT * FROM {table}").fetchall())
            if table in source_tables
            else []
        )
        target_rows = _rows_as_dicts(target.execute(f"SELECT * FROM {table}").fetchall())
        source_fingerprint = _fingerprint_rows(((table, source_rows),))
        target_fingerprint = _fingerprint_rows(((table, target_rows),))
        if source_fingerprint != target_fingerprint:
            columns = _mismatched_column_names(table, source_rows, target_rows)
            detail = f"({','.join(columns)})" if columns else ""
            mismatched.append(f"{table}{detail}")
    return tuple(mismatched)


def _mismatched_column_names(
    table: str,
    source_rows: list[dict[str, Any]],
    target_rows: list[dict[str, Any]],
) -> tuple[str, ...]:
    """Report representation differences without including row values in errors."""

    source_canonical = [_canonical_row(table, row) for row in source_rows]
    target_canonical = [_canonical_row(table, row) for row in target_rows]
    columns = set().union(*(row.keys() for row in [*source_canonical, *target_canonical]))
    mismatched: list[str] = []
    for column in sorted(columns):
        source_values = sorted(_canonical_json(row.get(column)) for row in source_canonical)
        target_values = sorted(_canonical_json(row.get(column)) for row in target_canonical)
        if source_values != target_values:
            mismatched.append(column)
    return tuple(mismatched)


def _fingerprint_rows(
    table_rows: Any,
) -> tuple[dict[str, int], str]:
    digest = hashlib.sha256()
    counts: dict[str, int] = {}
    for table, rows in table_rows:
        encoded_rows = sorted(_canonical_json(_canonical_row(table, row)) for row in rows)
        counts[table] = len(encoded_rows)
        digest.update(table.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(len(encoded_rows)).encode("ascii"))
        digest.update(b"\0")
        for row in encoded_rows:
            digest.update(row.encode("utf-8"))
            digest.update(b"\n")
    return counts, digest.hexdigest()


def _rows_as_dicts(rows: list[Any]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    )


def _canonical_row(table: str, row: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(row)
    # SQLite stores JSON as text while psycopg returns JSONB as Python values.
    for column in _JSONB_COLUMNS.get(table, frozenset()):
        if isinstance(normalized.get(column), str):
            try:
                normalized[column] = json.loads(normalized[column])
            except json.JSONDecodeError as exc:
                raise DomainDataMigrationError(
                    f"{table}.{column} is invalid JSON"
                ) from exc
    for column in _TIMESTAMPTZ_COLUMNS.get(table, frozenset()):
        if normalized.get(column) is not None:
            normalized[column] = _canonical_timestamptz(normalized[column], column=column)
    return normalized


def _canonical_timestamptz(value: object, *, column: str) -> str:
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise DomainDataMigrationError(f"{column} is not a valid ISO-8601 timestamp") from exc
    elif isinstance(value, datetime):
        parsed = value
    else:
        raise DomainDataMigrationError(f"{column} is not a timestamp")
    if parsed.tzinfo is None:
        raise DomainDataMigrationError(f"{column} must include a UTC offset")
    return parsed.astimezone(UTC).isoformat()


def _json_default(value: object) -> object:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return str(value)


def _load_psycopg() -> tuple[Any, Any]:
    try:
        return (
            importlib.import_module("psycopg"),
            importlib.import_module("psycopg.rows").dict_row,
        )
    except ModuleNotFoundError as exc:
        raise PostgresDriverUnavailable(
            "install pilot107[postgres] to migrate domain data"
        ) from exc


def _initialize_target_schema(postgres_dsn: str) -> None:
    # Both migration registries share the same advisory lock/history table and
    # use non-overlapping IDs.  Initializing both makes data cutover complete:
    # durable outbox messages and traces are not silently left in SQLite.
    initialize_postgres_domain_schema(postgres_dsn)
    PostgresControlRepository(postgres_dsn)
