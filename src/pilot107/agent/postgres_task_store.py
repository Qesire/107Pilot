"""PostgreSQL AgentTask store sharing the SQLite behavioral contract."""

from __future__ import annotations

import importlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from pilot107.agent.task_store import SQLiteAgentTaskStore
from pilot107.core.postgres_control_repository import PostgresDriverUnavailable
from pilot107.core.postgres_domain_schema import initialize_postgres_domain_schema


class PostgresAgentTaskStore(SQLiteAgentTaskStore):
    def __init__(
        self,
        dsn: str,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not dsn or any(character in dsn for character in "\r\n\0"):
            raise ValueError("PostgreSQL DSN is invalid")
        try:
            self._psycopg = importlib.import_module("psycopg")
            self._dict_row = importlib.import_module("psycopg.rows").dict_row
            self._jsonb = importlib.import_module("psycopg.types.json").Jsonb
        except ModuleNotFoundError as exc:
            raise PostgresDriverUnavailable(
                "install pilot107[postgres] to use PostgreSQL AgentTask stores"
            ) from exc
        self.dsn = dsn
        self._clock = clock or (lambda: datetime.now(UTC))
        self._integrity_errors = (self._psycopg.IntegrityError,)
        initialize_postgres_domain_schema(dsn)

    def connect(self) -> Any:
        raw = self._psycopg.connect(self.dsn, row_factory=self._dict_row)
        return _PostgresConnection(raw, jsonb=self._jsonb)


class _PostgresConnection:
    def __init__(self, raw: Any, *, jsonb: Any) -> None:
        self.raw = raw
        self._jsonb = jsonb

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
        values = list(parameters)
        if normalized.startswith("INSERT INTO agent_tasks"):
            values[8] = self._jsonb(json.loads(str(values[8])))
            values[9] = self._jsonb(json.loads(str(values[9])))
        elif normalized.startswith("UPDATE agent_tasks SET state = ?, result_json = ?"):
            values[1] = self._jsonb(json.loads(str(values[1])))
        elif normalized.startswith(
            "UPDATE agent_tasks SET state = 'cancelled', cancel_requested = 1"
        ):
            values[0] = self._jsonb(json.loads(str(values[0])))
        translated = statement.replace("?", "%s")
        return self.raw.execute(translated, tuple(values))
