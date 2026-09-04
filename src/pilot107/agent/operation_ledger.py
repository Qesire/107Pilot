"""Durable, provider-call-independent operation identity and receipt ledger.

The Agent model/tool loop is intentionally not involved here.  This module owns
control-plane facts that answer one question after a crash: did a typed domain
operation happen, and can its result be replayed without repeating the side
effect?
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pilot107.core.schema_migrations import SchemaMigration, apply_schema_migrations

_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,255}$")
_OPTIONAL_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/@+-]{0,511}$")


class AgentOperationConflict(RuntimeError):
    """Raised when a durable operation identity is reused with different intent."""


class AgentOperationState(StrEnum):
    RESERVED = "reserved"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    STALE = "stale"
    RECONCILING = "reconciling"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class DurableOperationIdentity:
    operation_key: str
    owner: str
    session_id: str
    tool_name: str
    intent_digest: str
    target_type: str | None
    target_id: str | None
    target_revision: str | None
    user_request_key: str


@dataclass(frozen=True)
class AgentOperationReceiptRecord:
    operation_key: str
    owner: str
    session_id: str
    tool_name: str
    intent_digest: str
    target_type: str | None
    target_id: str | None
    target_revision: str | None
    user_request_key: str
    state: AgentOperationState
    latest_invocation_id: str | None
    result_digest: str | None
    result_ref: str | None
    side_effect_receipt_ref: str | None
    error_code: str | None
    created_at: str
    updated_at: str


SQLITE_AGENT_OPERATION_MIGRATIONS = (
    SchemaMigration(
        migration_id="006a.002.agent_operation_receipts",
        statements=(
            """
            CREATE TABLE agent_operation_receipts (
                operation_key TEXT PRIMARY KEY,
                owner TEXT NOT NULL,
                session_id TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                intent_digest TEXT NOT NULL,
                target_type TEXT,
                target_id TEXT,
                target_revision TEXT,
                user_request_key TEXT NOT NULL,
                state TEXT NOT NULL,
                latest_invocation_id TEXT,
                result_digest TEXT,
                result_ref TEXT,
                side_effect_receipt_ref TEXT,
                error_code TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                CHECK (state IN (
                    'reserved', 'running', 'completed', 'failed',
                    'stale', 'reconciling', 'unknown'
                ))
            )
            """,
            """
            CREATE INDEX idx_agent_operation_owner_state
            ON agent_operation_receipts(owner, state, updated_at, operation_key)
            """,
            """
            CREATE INDEX idx_agent_operation_session
            ON agent_operation_receipts(owner, session_id, created_at, operation_key)
            """,
        ),
    ),
)

# Kept beside the SQLite contract so parity cannot be designed independently.
# A later integration step registers these statements with the PostgreSQL
# checksum migration runner before PostgresAgentOperationLedger is enabled.
POSTGRES_AGENT_OPERATION_SCHEMA = (
    """
    CREATE TABLE agent_operation_receipts (
        operation_key TEXT PRIMARY KEY,
        owner TEXT NOT NULL,
        session_id TEXT NOT NULL,
        tool_name TEXT NOT NULL,
        intent_digest TEXT NOT NULL,
        target_type TEXT,
        target_id TEXT,
        target_revision TEXT,
        user_request_key TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN (
            'reserved', 'running', 'completed', 'failed',
            'stale', 'reconciling', 'unknown'
        )),
        latest_invocation_id TEXT,
        result_digest TEXT,
        result_ref TEXT,
        side_effect_receipt_ref TEXT,
        error_code TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """.strip(),
    """
    CREATE INDEX idx_agent_operation_owner_state
    ON agent_operation_receipts(owner, state, updated_at, operation_key)
    """.strip(),
    """
    CREATE INDEX idx_agent_operation_session
    ON agent_operation_receipts(owner, session_id, created_at, operation_key)
    """.strip(),
)


def durable_operation_identity(
    *,
    owner: str,
    session_id: str,
    tool_name: str,
    arguments: Mapping[str, object],
    user_request_key: str,
    target_type: str | None = None,
    target_id: str | None = None,
    target_revision: str | None = None,
) -> DurableOperationIdentity:
    """Build a stable identity that deliberately excludes provider call IDs."""

    _key(owner, "owner")
    _key(session_id, "session_id")
    _key(tool_name, "tool_name")
    _key(user_request_key, "user_request_key")
    _optional_key(target_type, "target_type")
    _optional_key(target_id, "target_id")
    _optional_key(target_revision, "target_revision")
    arguments_bytes = _canonical(arguments)
    intent_payload = {
        "tool_name": tool_name,
        "arguments": json.loads(arguments_bytes),
        "target_type": target_type,
        "target_id": target_id,
        "target_revision": target_revision,
    }
    intent_digest = "sha256:" + hashlib.sha256(_canonical(intent_payload)).hexdigest()
    key_payload = {
        "owner": owner,
        "session_id": session_id,
        "intent_digest": intent_digest,
        "user_request_key": user_request_key,
    }
    operation_key = "op-v1:" + hashlib.sha256(_canonical(key_payload)).hexdigest()
    return DurableOperationIdentity(
        operation_key=operation_key,
        owner=owner,
        session_id=session_id,
        tool_name=tool_name,
        intent_digest=intent_digest,
        target_type=target_type,
        target_id=target_id,
        target_revision=target_revision,
        user_request_key=user_request_key,
    )


class SQLiteAgentOperationLedger:
    """Reference operation ledger with explicit unknown/reconciliation states."""

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
            apply_schema_migrations(conn, SQLITE_AGENT_OPERATION_MIGRATIONS)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 10000")
        return conn

    def reserve(
        self,
        identity: DurableOperationIdentity,
        *,
        invocation_id: str,
    ) -> tuple[AgentOperationReceiptRecord, bool]:
        _key(invocation_id, "invocation_id")
        now = self._now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM agent_operation_receipts WHERE operation_key = ?",
                (identity.operation_key,),
            ).fetchone()
            if existing is not None:
                record = _row(existing)
                _assert_same_identity(record, identity)
                return record, False
            conn.execute(
                """
                INSERT INTO agent_operation_receipts (
                    operation_key, owner, session_id, tool_name, intent_digest,
                    target_type, target_id, target_revision, user_request_key,
                    state, latest_invocation_id, result_digest, result_ref,
                    side_effect_receipt_ref, error_code, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'reserved', ?, NULL, NULL,
                          NULL, NULL, ?, ?)
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
            )
            row = conn.execute(
                "SELECT * FROM agent_operation_receipts WHERE operation_key = ?",
                (identity.operation_key,),
            ).fetchone()
        if row is None:
            raise RuntimeError("operation reservation did not produce a row")
        return _row(row), True

    def get(self, operation_key: str, *, owner: str) -> AgentOperationReceiptRecord:
        _key(operation_key, "operation_key")
        _key(owner, "owner")
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM agent_operation_receipts
                WHERE operation_key = ? AND owner = ?
                """,
                (operation_key, owner),
            ).fetchone()
        if row is None:
            raise KeyError(operation_key)
        return _row(row)

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
        _digest(result_digest, "result_digest")
        _optional_key(result_ref, "result_ref")
        _optional_key(side_effect_receipt_ref, "side_effect_receipt_ref")
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
        _key(error_code, "error_code")
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
        _key(owner, "owner")
        if limit < 1 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000")
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM agent_operation_receipts
                WHERE owner = ? AND state IN ('stale', 'reconciling', 'unknown')
                ORDER BY updated_at, operation_key LIMIT ?
                """,
                (owner, limit),
            ).fetchall()
        return [_row(row) for row in rows]

    def _transition(
        self,
        operation_key: str,
        *,
        owner: str,
        invocation_id: str | None,
        allowed: set[AgentOperationState],
        target: AgentOperationState,
    ) -> AgentOperationReceiptRecord:
        _key(operation_key, "operation_key")
        _key(owner, "owner")
        if invocation_id is not None:
            _key(invocation_id, "invocation_id")
        now = self._now()
        placeholders = ",".join("?" for _ in allowed)
        values = [state.value for state in sorted(allowed, key=lambda state: state.value)]
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute(
                "SELECT * FROM agent_operation_receipts WHERE operation_key = ? AND owner = ?",
                (operation_key, owner),
            ).fetchone()
            if current is None:
                raise KeyError(operation_key)
            current_record = _row(current)
            if current_record.state == target:
                return current_record
            updated = conn.execute(
                f"""
                UPDATE agent_operation_receipts
                SET state = ?, latest_invocation_id = COALESCE(?, latest_invocation_id),
                    updated_at = ?
                WHERE operation_key = ? AND owner = ? AND state IN ({placeholders})
                """,
                (target.value, invocation_id, now, operation_key, owner, *values),
            )
            if updated.rowcount != 1:
                raise AgentOperationConflict(
                    f"operation cannot transition from {current_record.state} to {target}"
                )
            row = conn.execute(
                "SELECT * FROM agent_operation_receipts WHERE operation_key = ?",
                (operation_key,),
            ).fetchone()
        if row is None:
            raise RuntimeError("operation transition lost its row")
        return _row(row)

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
        _key(operation_key, "operation_key")
        _key(owner, "owner")
        _key(invocation_id, "invocation_id")
        now = self._now()
        placeholders = ",".join("?" for _ in allowed)
        values = [state.value for state in sorted(allowed, key=lambda state: state.value)]
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute(
                "SELECT * FROM agent_operation_receipts WHERE operation_key = ? AND owner = ?",
                (operation_key, owner),
            ).fetchone()
            if current is None:
                raise KeyError(operation_key)
            current_record = _row(current)
            if current_record.state == target:
                if (
                    current_record.result_digest != result_digest
                    or current_record.result_ref != result_ref
                    or current_record.side_effect_receipt_ref != side_effect_receipt_ref
                    or current_record.error_code != error_code
                ):
                    raise AgentOperationConflict("terminal receipt content changed")
                return current_record
            updated = conn.execute(
                f"""
                UPDATE agent_operation_receipts
                SET state = ?, latest_invocation_id = ?, result_digest = ?,
                    result_ref = ?, side_effect_receipt_ref = ?, error_code = ?,
                    updated_at = ?
                WHERE operation_key = ? AND owner = ? AND state IN ({placeholders})
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
                    *values,
                ),
            )
            if updated.rowcount != 1:
                raise AgentOperationConflict(
                    f"operation cannot transition from {current_record.state} to {target}"
                )
            row = conn.execute(
                "SELECT * FROM agent_operation_receipts WHERE operation_key = ?",
                (operation_key,),
            ).fetchone()
        if row is None:
            raise RuntimeError("terminal operation transition lost its row")
        return _row(row)

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


def _row(row: sqlite3.Row) -> AgentOperationReceiptRecord:
    return AgentOperationReceiptRecord(
        operation_key=str(row["operation_key"]),
        owner=str(row["owner"]),
        session_id=str(row["session_id"]),
        tool_name=str(row["tool_name"]),
        intent_digest=str(row["intent_digest"]),
        target_type=_optional(row["target_type"]),
        target_id=_optional(row["target_id"]),
        target_revision=_optional(row["target_revision"]),
        user_request_key=str(row["user_request_key"]),
        state=AgentOperationState(str(row["state"])),
        latest_invocation_id=_optional(row["latest_invocation_id"]),
        result_digest=_optional(row["result_digest"]),
        result_ref=_optional(row["result_ref"]),
        side_effect_receipt_ref=_optional(row["side_effect_receipt_ref"]),
        error_code=_optional(row["error_code"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("operation intent must be canonical JSON") from exc


def _key(value: str, label: str) -> None:
    if not isinstance(value, str) or not _KEY.fullmatch(value):
        raise ValueError(f"{label} is invalid")


def _optional_key(value: str | None, label: str) -> None:
    if value is not None and (
        not isinstance(value, str) or not _OPTIONAL_KEY.fullmatch(value)
    ):
        raise ValueError(f"{label} is invalid")


def _digest(value: str, label: str) -> None:
    if not isinstance(value, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", value):
        raise ValueError(f"{label} must be a sha256 digest")


def _optional(value: Any) -> str | None:
    return None if value is None else str(value)
