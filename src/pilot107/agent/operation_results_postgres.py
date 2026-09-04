"""PostgreSQL replay store for durable Agent operation ToolResults."""

from __future__ import annotations

import hashlib
import importlib
import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from pilot107.agent.operation_results import (
    POSTGRES_AGENT_OPERATION_RESULT_SCHEMA,
    AgentOperationResultConflict,
    AgentOperationResultRecord,
    operation_result_ref,
    replay_payload,
)
from pilot107.agent.protocol import ToolResult
from pilot107.core.postgres_control_repository import PostgresDriverUnavailable

_MIGRATION_ID = "006a.003.agent_operation_results"


class PostgresAgentOperationResultStore:
    """Native PostgreSQL authority for replayable operation results."""

    def __init__(
        self,
        dsn: str,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not dsn or any(character in dsn for character in "\r\n\0"):
            raise ValueError("PostgreSQL DSN is invalid")
        self.dsn = dsn
        self._clock = clock or (lambda: datetime.now(UTC))
        try:
            self._psycopg = importlib.import_module("psycopg")
            self._dict_row = importlib.import_module("psycopg.rows").dict_row
            self._jsonb = importlib.import_module("psycopg.types.json").Jsonb
        except ModuleNotFoundError as exc:
            raise PostgresDriverUnavailable(
                "install pilot107[postgres] to use PostgreSQL Agent repositories"
            ) from exc
        self._ensure_schema()

    def connect(self) -> Any:
        return self._psycopg.connect(self.dsn, row_factory=self._dict_row)

    def put(
        self,
        *,
        operation_key: str,
        owner: str,
        result: ToolResult,
    ) -> AgentOperationResultRecord:
        payload = replay_payload(result)
        digest = "sha256:" + hashlib.sha256(_canonical(payload)).hexdigest()
        result_ref = operation_result_ref(operation_key)
        now = self._now()
        with self.connect() as conn:
            row = conn.execute(
                """
                INSERT INTO agent_operation_results (
                    result_ref, operation_key, owner, payload_json,
                    result_digest, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (operation_key) DO NOTHING
                RETURNING *
                """,
                (
                    result_ref,
                    operation_key,
                    owner,
                    self._jsonb(payload),
                    digest,
                    now,
                ),
            ).fetchone()
            if row is None:
                row = conn.execute(
                    """
                    SELECT * FROM agent_operation_results
                    WHERE operation_key = %s FOR UPDATE
                    """,
                    (operation_key,),
                ).fetchone()
        if row is None:
            raise RuntimeError("operation result insert did not produce a row")
        record = _record(row)
        if (
            record.owner != owner
            or record.result_ref != result_ref
            or record.result_digest != digest
            or record.payload != payload
        ):
            raise AgentOperationResultConflict(
                "operation already has a different replay result"
            )
        return record

    def get(self, result_ref: str, *, owner: str) -> AgentOperationResultRecord:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM agent_operation_results
                WHERE result_ref = %s AND owner = %s
                """,
                (result_ref, owner),
            ).fetchone()
        if row is None:
            raise KeyError(result_ref)
        return _record(row)

    def get_for_operation(
        self,
        operation_key: str,
        *,
        owner: str,
    ) -> AgentOperationResultRecord:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM agent_operation_results
                WHERE operation_key = %s AND owner = %s
                """,
                (operation_key, owner),
            ).fetchone()
        if row is None:
            raise KeyError(operation_key)
        return _record(row)

    def _ensure_schema(self) -> None:
        checksum = hashlib.sha256(
            "\n-- statement\n".join(POSTGRES_AGENT_OPERATION_RESULT_SCHEMA).encode("utf-8")
        ).hexdigest()
        with self.connect() as conn, conn.transaction():
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
                    raise RuntimeError(
                        "PostgreSQL Agent operation result migration checksum mismatch"
                    )
                return
            for statement in POSTGRES_AGENT_OPERATION_RESULT_SCHEMA:
                conn.execute(statement)
            conn.execute(
                """
                INSERT INTO schema_migrations (migration_id, checksum, applied_at)
                VALUES (%s, %s, %s)
                """,
                (_MIGRATION_ID, checksum, datetime.now(UTC)),
            )

    def _now(self) -> str:
        value = self._clock()
        if value.tzinfo is None:
            raise ValueError("operation result clock must be timezone-aware")
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _record(row: Mapping[str, Any]) -> AgentOperationResultRecord:
    payload = row["payload_json"]
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise AgentOperationResultConflict("stored operation result JSON is invalid") from exc
    if not isinstance(payload, dict):
        raise AgentOperationResultConflict("stored operation result payload is invalid")
    return AgentOperationResultRecord(
        result_ref=str(row["result_ref"]),
        operation_key=str(row["operation_key"]),
        owner=str(row["owner"]),
        payload=payload,
        result_digest=str(row["result_digest"]),
        created_at=str(row["created_at"]),
    )


def _canonical(value: Mapping[str, object] | dict[str, object]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


__all__ = ["PostgresAgentOperationResultStore"]
