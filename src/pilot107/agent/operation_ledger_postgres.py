"""PostgreSQL implementation of the durable Agent operation receipt ledger."""

from __future__ import annotations

import hashlib
import importlib
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from pilot107.agent.operation_ledger import (
    POSTGRES_AGENT_OPERATION_SCHEMA,
    AgentOperationConflict,
    AgentOperationReceiptRecord,
    AgentOperationState,
    DurableOperationIdentity,
)
from pilot107.core.postgres_control_repository import PostgresDriverUnavailable

_MIGRATION_ID = "006a.002.agent_operation_receipts"


class PostgresAgentOperationLedger:
    """Operation ledger using the shared PostgreSQL checksum migration history."""

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
        except ModuleNotFoundError as exc:
            raise PostgresDriverUnavailable(
                "install pilot107[postgres] to use PostgreSQL Agent repositories"
            ) from exc
        self._ensure_schema()

    def connect(self) -> Any:
        return self._psycopg.connect(self.dsn, row_factory=self._dict_row)

    def reserve(
        self,
        identity: DurableOperationIdentity,
        *,
        invocation_id: str,
    ) -> tuple[AgentOperationReceiptRecord, bool]:
        now = self._now()
        with self.connect() as conn:
            row = conn.execute(
                """
                INSERT INTO agent_operation_receipts (
                    operation_key, owner, session_id, tool_name, intent_digest,
                    target_type, target_id, target_revision, user_request_key,
                    state, latest_invocation_id, result_digest, result_ref,
                    side_effect_receipt_ref, error_code, created_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'reserved', %s,
                          NULL, NULL, NULL, NULL, %s, %s)
                ON CONFLICT (operation_key) DO NOTHING
                RETURNING *
                """,
                (
                    identity.operation_key,
                    identity.owner,
                    identity.session_id,
                    identity.tool_name,
                    identity.intent_digest,
                    identity.target_type,
                    identity.target_id,
                    identity.target_revision,
                    identity.user_request_key,
                    invocation_id,
                    now,
                    now,
                ),
            ).fetchone()
            created = row is not None
            if row is None:
                row = conn.execute(
                    """
                    SELECT * FROM agent_operation_receipts
                    WHERE operation_key = %s FOR UPDATE
                    """,
                    (identity.operation_key,),
                ).fetchone()
        if row is None:
            raise RuntimeError("operation reservation did not produce a row")
        record = _record(row)
        _assert_same_identity(record, identity)
        return record, created

    def get(self, operation_key: str, *, owner: str) -> AgentOperationReceiptRecord:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM agent_operation_receipts
                WHERE operation_key = %s AND owner = %s
                """,
                (operation_key, owner),
            ).fetchone()
        if row is None:
            raise KeyError(operation_key)
        return _record(row)

    def mark_running(
        self,
        operation_key: str,
        *,
        owner: str,
        invocation_id: str,
    ) -> AgentOperationReceiptRecord:
        return self._transition(
            operation_key,
            owner=owner,
            invocation_id=invocation_id,
            allowed={AgentOperationState.RESERVED, AgentOperationState.RECONCILING},
            target=AgentOperationState.RUNNING,
        )

    def complete(
        self,
        operation_key: str,
        *,
        owner: str,
        invocation_id: str,
        result_digest: str,
        result_ref: str | None,
        side_effect_receipt_ref: str | None,
    ) -> AgentOperationReceiptRecord:
        return self._terminal_transition(
            operation_key,
            owner=owner,
            invocation_id=invocation_id,
            allowed={
                AgentOperationState.RESERVED,
                AgentOperationState.RUNNING,
                AgentOperationState.RECONCILING,
            },
            target=AgentOperationState.COMPLETED,
            result_digest=result_digest,
            result_ref=result_ref,
            side_effect_receipt_ref=side_effect_receipt_ref,
            error_code=None,
        )

    def fail(
        self,
        operation_key: str,
        *,
        owner: str,
        invocation_id: str,
        error_code: str,
    ) -> AgentOperationReceiptRecord:
        return self._terminal_transition(
            operation_key,
            owner=owner,
            invocation_id=invocation_id,
            allowed={
                AgentOperationState.RESERVED,
                AgentOperationState.RUNNING,
                AgentOperationState.RECONCILING,
            },
            target=AgentOperationState.FAILED,
            result_digest=None,
            result_ref=None,
            side_effect_receipt_ref=None,
            error_code=error_code,
        )

    def mark_stale(
        self, operation_key: str, *, owner: str
    ) -> AgentOperationReceiptRecord:
        return self._transition(
            operation_key,
            owner=owner,
            invocation_id=None,
            allowed={AgentOperationState.RUNNING},
            target=AgentOperationState.STALE,
        )

    def begin_reconciliation(
        self, operation_key: str, *, owner: str, invocation_id: str
    ) -> AgentOperationReceiptRecord:
        return self._transition(
            operation_key,
            owner=owner,
            invocation_id=invocation_id,
            allowed={AgentOperationState.STALE, AgentOperationState.UNKNOWN},
            target=AgentOperationState.RECONCILING,
        )

    def mark_unknown(
        self, operation_key: str, *, owner: str
    ) -> AgentOperationReceiptRecord:
        return self._transition(
            operation_key,
            owner=owner,
            invocation_id=None,
            allowed={
                AgentOperationState.RUNNING,
                AgentOperationState.STALE,
                AgentOperationState.RECONCILING,
            },
            target=AgentOperationState.UNKNOWN,
        )

    def list_reconcilable(
        self, *, owner: str, limit: int = 100
    ) -> list[AgentOperationReceiptRecord]:
        if limit < 1 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000")
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM agent_operation_receipts
                WHERE owner = %s AND state = ANY(%s)
                ORDER BY updated_at, operation_key LIMIT %s
                """,
                (owner, ["stale", "reconciling", "unknown"], limit),
            ).fetchall()
        return [_record(row) for row in rows]

    def _transition(
        self,
        operation_key: str,
        *,
        owner: str,
        invocation_id: str | None,
        allowed: set[AgentOperationState],
        target: AgentOperationState,
    ) -> AgentOperationReceiptRecord:
        now = self._now()
        with self.connect() as conn:
            current = conn.execute(
                """
                SELECT * FROM agent_operation_receipts
                WHERE operation_key = %s AND owner = %s FOR UPDATE
                """,
                (operation_key, owner),
            ).fetchone()
            if current is None:
                raise KeyError(operation_key)
            record = _record(current)
            if record.state == target:
                return record
            row = conn.execute(
                """
                UPDATE agent_operation_receipts
                SET state = %s,
                    latest_invocation_id = COALESCE(%s, latest_invocation_id),
                    updated_at = %s
                WHERE operation_key = %s AND owner = %s AND state = ANY(%s)
                RETURNING *
                """,
                (
                    target.value,
                    invocation_id,
                    now,
                    operation_key,
                    owner,
                    [state.value for state in allowed],
                ),
            ).fetchone()
        if row is None:
            raise AgentOperationConflict(
                f"operation cannot transition from {record.state} to {target}"
            )
        return _record(row)

    def _terminal_transition(
        self,
        operation_key: str,
        *,
        owner: str,
        invocation_id: str,
        allowed: set[AgentOperationState],
        target: AgentOperationState,
        result_digest: str | None,
        result_ref: str | None,
        side_effect_receipt_ref: str | None,
        error_code: str | None,
    ) -> AgentOperationReceiptRecord:
        now = self._now()
        with self.connect() as conn:
            current = conn.execute(
                """
                SELECT * FROM agent_operation_receipts
                WHERE operation_key = %s AND owner = %s FOR UPDATE
                """,
                (operation_key, owner),
            ).fetchone()
            if current is None:
                raise KeyError(operation_key)
            record = _record(current)
            if record.state == target:
                if (
                    record.result_digest != result_digest
                    or record.result_ref != result_ref
                    or record.side_effect_receipt_ref != side_effect_receipt_ref
                    or record.error_code != error_code
                ):
                    raise AgentOperationConflict("terminal receipt content changed")
                return record
            row = conn.execute(
                """
                UPDATE agent_operation_receipts
                SET state = %s, latest_invocation_id = %s,
                    result_digest = %s, result_ref = %s,
                    side_effect_receipt_ref = %s, error_code = %s, updated_at = %s
                WHERE operation_key = %s AND owner = %s AND state = ANY(%s)
                RETURNING *
                """,
                (
                    target.value,
                    invocation_id,
                    result_digest,
                    result_ref,
                    side_effect_receipt_ref,
                    error_code,
                    now,
                    operation_key,
                    owner,
                    [state.value for state in allowed],
                ),
            ).fetchone()
        if row is None:
            raise AgentOperationConflict(
                f"operation cannot transition from {record.state} to {target}"
            )
        return _record(row)

    def _ensure_schema(self) -> None:
        checksum = hashlib.sha256(
            "\n-- statement\n".join(POSTGRES_AGENT_OPERATION_SCHEMA).encode("utf-8")
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
                    raise RuntimeError("PostgreSQL Agent operation migration checksum mismatch")
                return
            for statement in POSTGRES_AGENT_OPERATION_SCHEMA:
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
            raise ValueError("operation ledger clock must be timezone-aware")
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _assert_same_identity(
    record: AgentOperationReceiptRecord, identity: DurableOperationIdentity
) -> None:
    if (
        record.owner != identity.owner
        or record.session_id != identity.session_id
        or record.tool_name != identity.tool_name
        or record.intent_digest != identity.intent_digest
        or record.target_type != identity.target_type
        or record.target_id != identity.target_id
        or record.target_revision != identity.target_revision
        or record.user_request_key != identity.user_request_key
    ):
        raise AgentOperationConflict("operation_key refers to different canonical intent")


def _record(row: Mapping[str, Any]) -> AgentOperationReceiptRecord:
    return AgentOperationReceiptRecord(
        operation_key=str(row["operation_key"]),
        owner=str(row["owner"]),
        session_id=str(row["session_id"]),
        tool_name=str(row["tool_name"]),
        intent_digest=str(row["intent_digest"]),
        target_type=_optional(row.get("target_type")),
        target_id=_optional(row.get("target_id")),
        target_revision=_optional(row.get("target_revision")),
        user_request_key=str(row["user_request_key"]),
        state=AgentOperationState(str(row["state"])),
        latest_invocation_id=_optional(row.get("latest_invocation_id")),
        result_digest=_optional(row.get("result_digest")),
        result_ref=_optional(row.get("result_ref")),
        side_effect_receipt_ref=_optional(row.get("side_effect_receipt_ref")),
        error_code=_optional(row.get("error_code")),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _optional(value: Any) -> str | None:
    return None if value is None else str(value)
