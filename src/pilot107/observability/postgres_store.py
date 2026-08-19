"""PostgreSQL implementation of the Observability store contract."""

from __future__ import annotations

import importlib
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pilot107.core.postgres_control_repository import PostgresDriverUnavailable
from pilot107.core.postgres_domain_schema import initialize_postgres_domain_schema
from pilot107.observability.store import SQLiteObservabilityStore


class PostgresObservabilityStore(SQLiteObservabilityStore):
    def __init__(
        self,
        dsn: str,
        *,
        clock: Callable[[], datetime] | None = None,
        compatibility_path: Path | None = None,
    ) -> None:
        if not dsn or any(character in dsn for character in "\r\n\0"):
            raise ValueError("PostgreSQL DSN is invalid")
        try:
            self._psycopg = importlib.import_module("psycopg")
            self._dict_row = importlib.import_module("psycopg.rows").dict_row
        except ModuleNotFoundError as exc:
            raise PostgresDriverUnavailable(
                "install pilot107[postgres] to use PostgreSQL Observability stores"
            ) from exc
        self.dsn = dsn
        self.db_path = compatibility_path or Path("observability-postgres")
        self._clock = clock or (lambda: datetime.now(UTC))
        initialize_postgres_domain_schema(dsn)

    def connect(self) -> Any:
        return _PostgresConnection(
            self._psycopg.connect(self.dsn, row_factory=self._dict_row)
        )


class _PostgresConnection:
    def __init__(self, raw: Any) -> None:
        self.raw = raw

    def __enter__(self) -> _PostgresConnection:
        self.raw.__enter__()
        return self

    def __exit__(self, *arguments: object) -> object:
        return self.raw.__exit__(*arguments)

    def execute(self, statement: str, parameters: tuple[object, ...] = ()) -> Any:
        if statement.strip() == "BEGIN IMMEDIATE":
            return self.raw.execute("BEGIN")
        return self.raw.execute(statement.replace("?", "%s"), parameters)
