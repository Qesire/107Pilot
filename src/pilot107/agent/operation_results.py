"""Replayable ToolResult envelopes for completed durable Agent operations.

A completed operation receipt is only authoritative when its result can be
reconstructed without re-running the side effect.  This module persists the
ToolResult envelope independently from provider invocation IDs and exposes a
stable ``result_ref`` that the operation ledger can bind to its receipt.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pilot107.agent.protocol import TOOL_RESULT_PROTOCOL_VERSION, ToolResult
from pilot107.core.schema_migrations import SchemaMigration, apply_schema_migrations


class AgentOperationResultConflict(RuntimeError):
    """Raised when a result_ref or operation key is reused with different content."""


@dataclass(frozen=True)
class AgentOperationResultRecord:
    result_ref: str
    operation_key: str
    owner: str
    payload: dict[str, object]
    result_digest: str
    created_at: str


SQLITE_AGENT_OPERATION_RESULT_MIGRATIONS = (
    SchemaMigration(
        migration_id="006a.003.agent_operation_results",
        statements=(
            """
            CREATE TABLE agent_operation_results (
                result_ref TEXT PRIMARY KEY,
                operation_key TEXT NOT NULL UNIQUE,
                owner TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                result_digest TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """,
            """
            CREATE INDEX idx_agent_operation_results_owner_operation
            ON agent_operation_results(owner, operation_key)
            """,
        ),
    ),
)

POSTGRES_AGENT_OPERATION_RESULT_SCHEMA = (
    """
    CREATE TABLE agent_operation_results (
        result_ref TEXT PRIMARY KEY,
        operation_key TEXT NOT NULL UNIQUE,
        owner TEXT NOT NULL,
        payload_json JSONB NOT NULL,
        result_digest TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """.strip(),
    """
    CREATE INDEX idx_agent_operation_results_owner_operation
    ON agent_operation_results(owner, operation_key)
    """.strip(),
)


class SQLiteAgentOperationResultStore:
    """SQLite authority for replayable, invocation-ID-independent ToolResults."""

    def __init__(
        self,
        db_path: Path,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock or (lambda: datetime.now(UTC))
        with self.connect() as conn:
            apply_schema_migrations(conn, SQLITE_AGENT_OPERATION_RESULT_MIGRATIONS)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 10000")
        return conn

    def put(
        self,
        *,
        operation_key: str,
        owner: str,
        result: ToolResult,
    ) -> AgentOperationResultRecord:
        payload = replay_payload(result)
        encoded = _canonical(payload)
        digest = "sha256:" + hashlib.sha256(encoded).hexdigest()
        result_ref = operation_result_ref(operation_key)
        now = self._now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                """
                SELECT * FROM agent_operation_results
                WHERE operation_key = ?
                """,
                (operation_key,),
            ).fetchone()
            if existing is not None:
                record = _row(existing)
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
            conn.execute(
                """
                INSERT INTO agent_operation_results (
                    result_ref, operation_key, owner, payload_json,
                    result_digest, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (result_ref, operation_key, owner, encoded.decode("utf-8"), digest, now),
            )
            row = conn.execute(
                "SELECT * FROM agent_operation_results WHERE result_ref = ?",
                (result_ref,),
            ).fetchone()
        if row is None:
            raise RuntimeError("operation result insert did not produce a row")
        return _row(row)

    def get(self, result_ref: str, *, owner: str) -> AgentOperationResultRecord:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM agent_operation_results
                WHERE result_ref = ? AND owner = ?
                """,
                (result_ref, owner),
            ).fetchone()
        if row is None:
            raise KeyError(result_ref)
        return _row(row)

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
                WHERE operation_key = ? AND owner = ?
                """,
                (operation_key, owner),
            ).fetchone()
        if row is None:
            raise KeyError(operation_key)
        return _row(row)

    def _now(self) -> str:
        value = self._clock()
        if value.tzinfo is None:
            raise ValueError("Agent operation result clock must be timezone-aware")
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def operation_result_ref(operation_key: str) -> str:
    digest = hashlib.sha256(operation_key.encode("utf-8")).hexdigest()
    return f"op-result-v1:{digest}"


def replay_payload(result: ToolResult) -> dict[str, object]:
    """Drop provider invocation identity while preserving the public result envelope."""

    return {
        "schema_version": TOOL_RESULT_PROTOCOL_VERSION,
        "result": result.result,
        "error": result.error,
        "evidence_refs": list(result.evidence_refs),
        "bytes_returned": result.bytes_returned,
    }


def replay_tool_result(
    record: AgentOperationResultRecord,
    *,
    invocation_id: str,
) -> ToolResult:
    payload = record.payload
    schema_version = payload.get("schema_version")
    evidence_refs = payload.get("evidence_refs")
    bytes_returned = payload.get("bytes_returned")
    result = payload.get("result")
    error = payload.get("error")
    if schema_version != TOOL_RESULT_PROTOCOL_VERSION:
        raise AgentOperationResultConflict("stored ToolResult schema version is invalid")
    if not isinstance(evidence_refs, list) or not all(
        isinstance(item, str) for item in evidence_refs
    ):
        raise AgentOperationResultConflict("stored ToolResult evidence_refs are invalid")
    if not isinstance(bytes_returned, int) or isinstance(bytes_returned, bool) or bytes_returned < 0:
        raise AgentOperationResultConflict("stored ToolResult byte count is invalid")
    if result is not None and not isinstance(result, dict):
        raise AgentOperationResultConflict("stored ToolResult result branch is invalid")
    if error is not None and not isinstance(error, dict):
        raise AgentOperationResultConflict("stored ToolResult error branch is invalid")
    if (result is None) == (error is None):
        raise AgentOperationResultConflict("stored ToolResult must contain exactly one branch")
    return ToolResult(
        schema_version=TOOL_RESULT_PROTOCOL_VERSION,
        invocation_id=invocation_id,
        result=result,
        error=error,
        evidence_refs=tuple(evidence_refs),
        bytes_returned=bytes_returned,
    )


def _row(row: sqlite3.Row) -> AgentOperationResultRecord:
    try:
        payload = json.loads(str(row["payload_json"]))
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
