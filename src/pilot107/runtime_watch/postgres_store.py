"""PostgreSQL Runtime Watch store sharing the SQLite behavioral contract."""

from __future__ import annotations

import importlib
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pilot107.core.postgres_control_repository import PostgresDriverUnavailable
from pilot107.core.postgres_domain_schema import initialize_postgres_domain_schema
from pilot107.runtime_watch.store import SQLiteRuntimeWatchStore


class PostgresRuntimeWatchStore(SQLiteRuntimeWatchStore):
    """Run the same fenced store operations against PostgreSQL.

    SQL used by the Runtime Watch store deliberately stays in the portable
    SQLite/PostgreSQL subset.  The small connection adapter translates bind
    markers and upgrades the lease validation read to a row lock so concurrent
    segment commits serialize on the Watch fencing record.
    """

    def __init__(
        self,
        dsn: str,
        *,
        segment_root: Path,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not dsn or any(character in dsn for character in "\r\n\0"):
            raise ValueError("PostgreSQL DSN is invalid")
        try:
            self._psycopg = importlib.import_module("psycopg")
            self._dict_row = importlib.import_module("psycopg.rows").dict_row
        except ModuleNotFoundError as exc:
            raise PostgresDriverUnavailable(
                "install pilot107[postgres] to use PostgreSQL Runtime Watch stores"
            ) from exc
        self.dsn = dsn
        self.segment_root = segment_root.resolve()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._before_segment_transaction = None
        if segment_root.is_symlink():
            raise ValueError("Runtime segment root cannot be a symlink")
        self.segment_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        initialize_postgres_domain_schema(dsn)

    def connect(self) -> Any:
        raw = self._psycopg.connect(self.dsn, row_factory=self._dict_row)
        return _PostgresConnection(raw)


class _PostgresConnection:
    def __init__(self, raw: Any) -> None:
        self.raw = raw

    def __enter__(self) -> _PostgresConnection:
        self.raw.__enter__()
        return self

    def __exit__(self, *arguments: object) -> object:
        return self.raw.__exit__(*arguments)

    def execute(
        self,
        statement: str,
        parameters: tuple[object, ...] = (),
    ) -> Any:
        normalized = statement.strip()
        if normalized == "BEGIN IMMEDIATE":
            return self.raw.execute("BEGIN")
        translated = statement.replace("?", "%s")
        if (
            normalized.startswith("SELECT 1 FROM runtime_watches")
            and "lease_owner" in normalized
            and "FOR UPDATE" not in normalized
        ):
            translated = f"{translated.rstrip()} FOR UPDATE"
        return self.raw.execute(translated, parameters)
