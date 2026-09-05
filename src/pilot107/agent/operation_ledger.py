"""Provider-independent durable identities and receipts for Agent side effects.

The operation ledger deliberately does not redefine Workspace state. It records
whether a typed Agent mutation intent has crossed a side-effect boundary. Live
Workspace revisions and journals remain a separate Workspace concern.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import re
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from pilot107.agent.protocol import ToolInvocation
from pilot107.agent.store import AgentSessionStore
from pilot107.core.postgres_domain_schema import initialize_postgres_domain_schema
from pilot107.core.schema_migrations import SchemaMigration, apply_schema_migrations

_OPERATION_ID = re.compile(r"^operation-[a-f0-9]{64}$")
_DIGEST = re.compile(r"^[a-f0-9]{64}$")
_SIDE_EFFECT_TOOLS = frozenset(
    {
        "project_blueprint_save",
        "workspace_patch",
        "sandbox_exec",
        "validation_schedule",
        "builder_build_submit",
    }
)


class AgentOperationState(StrEnum):
    RESERVED = "reserved"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    STALE = "stale"
    RECONCILING = "reconciling"
    UNKNOWN = "unknown"


class AgentOperationConflict(RuntimeError):
    """The durable operation identity conflicts or its Turn is stale/fenced."""


@dataclass(frozen=True)
class AgentOperationIntent:
    operation_key: str
    owner: str
    session_id: str
    origin_turn_id: str
    request_key: str
    tool_name: str
    intent_digest: str
    target_ref: str | None
    target_revision: str | None

    def __post_init__(self) -> None:
        if _OPERATION_ID.fullmatch(self.operation_key) is None:
            raise ValueError("operation_key is invalid")
        if _DIGEST.fullmatch(self.intent_digest) is None:
            raise ValueError("intent_digest is invalid")
        for value, label in (
            (self.owner, "owner"),
            (self.session_id, "session_id"),
            (self.origin_turn_id, "origin_turn_id"),
            (self.request_key, "request_key"),
            (self.tool_name, "tool_name"),
        ):
            _nonempty(value, label)
        for value, label in (
            (self.target_ref, "target_ref"),
            (self.target_revision, "target_revision"),
        ):
            if value is not None:
                _nonempty(value, label)


@dataclass(frozen=True)
class AgentOperationRecord:
    operation_key: str
    owner: str
    session_id: str
    origin_turn_id: str
    request_key: str
    tool_name: str
    intent_digest: str
    target_ref: str | None
    target_revision: str | None
    state: AgentOperationState
    origin_invocation_id: str
    last_invocation_id: str
    result: dict[str, Any] | None
    error: dict[str, Any] | None
    receipt_ref: str | None
    result_digest: str | None
    side_effect_ref: str | None
    reconciliation_attempt: int
    created_at: str
    updated_at: str


class AgentOperationLedger(Protocol):
    def reserve(
        self,
        intent: AgentOperationIntent,
        *,
        invocation_id: str,
        expected_state_version: int,
        expected_fencing_token: int,
    ) -> tuple[AgentOperationRecord, bool]: ...

    def start(
        self,
        operation_key: str,
        *,
        owner: str,
        invocation_id: str,
        expected_state_version: int,
        expected_fencing_token: int,
    ) -> AgentOperationRecord: ...

    def complete(
        self,
        operation_key: str,
        *,
        owner: str,
        expected_state_version: int,
        expected_fencing_token: int,
        result: Mapping[str, object],
        side_effect_ref: str | None,
    ) -> AgentOperationRecord: ...

    def fail(
        self,
        operation_key: str,
        *,
        owner: str,
        expected_state_version: int,
        expected_fencing_token: int,
        error: Mapping[str, object],
    ) -> AgentOperationRecord: ...

    def mark_unknown(
        self,
        operation_key: str,
        *,
        owner: str,
        expected_state_version: int,
        expected_fencing_token: int,
        error: Mapping[str, object],
    ) -> AgentOperationRecord: ...

    def get(self, operation_key: str, *, owner: str) -> AgentOperationRecord: ...


def operation_intent_for_invocation(
    store: AgentSessionStore,
    invocation: ToolInvocation,
    *,
    arguments_digest: str,
) -> AgentOperationIntent | None:
    """Return a stable side-effect identity independent of provider/Turn call IDs.

    ``intent_digest`` is intentionally *not* part of ``operation_key``. The key
    identifies the durable domain request; the digest is compared separately so
    changed canonical content under the same request identity fails closed.
    """

    if invocation.tool_name not in _SIDE_EFFECT_TOOLS:
        return None
    if _DIGEST.fullmatch(arguments_digest) is None:
        raise ValueError("arguments_digest is invalid")
    turn = store.get_turn(invocation.turn_id, owner=invocation.owner)
    if turn.session_id != invocation.session_id:
        raise AgentOperationConflict("operation Turn does not belong to its Session")
    request_key = _domain_request_key(invocation.arguments, fallback=turn.request_key)
    target_ref = _target_ref(invocation.arguments)
    target_revision = _target_revision(invocation.arguments)
    identity = {
        "owner": invocation.owner,
        "session_id": invocation.session_id,
        "request_key": request_key,
        "tool_name": invocation.tool_name,
        "target_ref": target_ref,
        "target_revision": target_revision,
    }
    operation_key = "operation-" + hashlib.sha256(_canonical(identity)).hexdigest()
    return AgentOperationIntent(
        operation_key=operation_key,
        owner=invocation.owner,
        session_id=invocation.session_id,
        origin_turn_id=invocation.turn_id,
        request_key=request_key,
        tool_name=invocation.tool_name,
        intent_digest=arguments_digest,
        target_ref=target_ref,
        target_revision=target_revision,
    )


def build_agent_operation_ledger(
    store: AgentSessionStore,
    *,
    clock: Callable[[], datetime] | None = None,
) -> AgentOperationLedger | None:
    """Select a ledger matching the existing durable Session store.

    Unknown test doubles retain the legacy invocation ledger. Production
    SQLite/PostgreSQL stores gain operation receipts without changing service or
    Workspace construction.
    """

    db_path = getattr(store, "db_path", None)
    if isinstance(db_path, Path):
        return SQLiteAgentOperationLedger(db_path, clock=clock)
    dsn = getattr(store, "dsn", None)
    if isinstance(dsn, str) and dsn:
        return PostgresAgentOperationLedger(dsn, clock=clock)
    return None


_SQLITE_MIGRATIONS = (
    SchemaMigration(
        migration_id="006a.002.agent_operations",
        statements=(
            """
            CREATE TABLE agent_operations (
                operation_key TEXT PRIMARY KEY,
                owner TEXT NOT NULL,
                session_id TEXT NOT NULL REFERENCES agent_sessions(session_id),
                origin_turn_id TEXT NOT NULL REFERENCES agent_turns(turn_id),
                request_key TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                intent_digest TEXT NOT NULL,
                target_ref TEXT,
                target_revision TEXT,
                state TEXT NOT NULL,
                origin_invocation_id TEXT NOT NULL,
                last_invocation_id TEXT NOT NULL,
                result_json TEXT,
                error_json TEXT,
                receipt_ref TEXT,
                result_digest TEXT,
                side_effect_ref TEXT,
                reconciliation_attempt INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                CHECK (state IN (
                    'reserved', 'running', 'completed', 'failed',
                    'stale', 'reconciling', 'unknown'
                )),
                CHECK (reconciliation_attempt >= 0)
            )
            """,
            """
            CREATE INDEX idx_agent_operations_owner_state
            ON agent_operations(owner, state, updated_at, operation_key)
            """,
            """
            CREATE INDEX idx_agent_operations_session
            ON agent_operations(owner, session_id, created_at, operation_key)
            """,
        ),
    ),
)

_POSTGRES_MIGRATION_ID = "004a.034.agent_operations"
_POSTGRES_STATEMENTS = (
    """
    CREATE TABLE agent_operations (
        operation_key TEXT PRIMARY KEY,
        owner TEXT NOT NULL,
        session_id TEXT NOT NULL REFERENCES agent_sessions(session_id),
        origin_turn_id TEXT NOT NULL REFERENCES agent_turns(turn_id),
        request_key TEXT NOT NULL,
        tool_name TEXT NOT NULL,
        intent_digest TEXT NOT NULL,
        target_ref TEXT,
        target_revision TEXT,
        state TEXT NOT NULL,
        origin_invocation_id TEXT NOT NULL,
        last_invocation_id TEXT NOT NULL,
        result_json JSONB,
        error_json JSONB,
        receipt_ref TEXT,
        result_digest TEXT,
        side_effect_ref TEXT,
        reconciliation_attempt BIGINT NOT NULL DEFAULT 0,
        created_at TIMESTAMPTZ NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL,
        CHECK (state IN (
            'reserved', 'running', 'completed', 'failed',
            'stale', 'reconciling', 'unknown'
        )),
        CHECK (reconciliation_attempt >= 0)
    )
    """,
    """
    CREATE INDEX idx_agent_operations_owner_state
    ON agent_operations(owner, state, updated_at, operation_key)
    """,
    """
    CREATE INDEX idx_agent_operations_session
    ON agent_operations(owner, session_id, created_at, operation_key)
    """,
)


class SQLiteAgentOperationLedger:
    def __init__(
        self,
        db_path: Path,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.db_path = db_path
        self._clock = clock or (lambda: datetime.now(UTC))
        with self.connect() as conn:
            apply_schema_migrations(conn, _SQLITE_MIGRATIONS)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 10000")
        return conn

    def reserve(
        self,
        intent: AgentOperationIntent,
        *,
        invocation_id: str,
        expected_state_version: int,
        expected_fencing_token: int,
    ) -> tuple[AgentOperationRecord, bool]:
        _operation_request(intent, invocation_id, expected_state_version, expected_fencing_token)
        now = self._now_text()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._assert_current_turn(
                conn,
                intent,
                expected_state_version=expected_state_version,
                expected_fencing_token=expected_fencing_token,
                now=now,
            )
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO agent_operations (
                    operation_key, owner, session_id, origin_turn_id, request_key,
                    tool_name, intent_digest, target_ref, target_revision, state,
                    origin_invocation_id, last_invocation_id, result_json, error_json,
                    receipt_ref, result_digest, side_effect_ref, reconciliation_attempt,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'reserved', ?, ?, NULL, NULL,
                          NULL, NULL, NULL, 0, ?, ?)
                """,
                (
                    intent.operation_key,
                    intent.owner,
                    intent.session_id,
                    intent.origin_turn_id,
                    intent.request_key,
                    intent.tool_name,
                    intent.intent_digest,
                    intent.target_ref,
                    intent.target_revision,
                    invocation_id,
                    invocation_id,
                    now,
                    now,
                ),
            )
            created = cursor.rowcount == 1
            row = conn.execute(
                "SELECT * FROM agent_operations WHERE operation_key = ? AND owner = ?",
                (intent.operation_key, intent.owner),
            ).fetchone()
            if row is None:
                raise RuntimeError("operation insert did not produce a row")
            record = _row_to_operation(row)
            _ensure_intent(record, intent)
            if not created:
                conn.execute(
                    """
                    UPDATE agent_operations SET last_invocation_id = ?, updated_at = ?
                    WHERE operation_key = ? AND owner = ?
                    """,
                    (invocation_id, now, intent.operation_key, intent.owner),
                )
                row = conn.execute(
                    "SELECT * FROM agent_operations WHERE operation_key = ? AND owner = ?",
                    (intent.operation_key, intent.owner),
                ).fetchone()
                assert row is not None
                record = _row_to_operation(row)
        return record, created

    def start(
        self,
        operation_key: str,
        *,
        owner: str,
        invocation_id: str,
        expected_state_version: int,
        expected_fencing_token: int,
    ) -> AgentOperationRecord:
        return self._transition(
            operation_key,
            owner=owner,
            invocation_id=invocation_id,
            expected_state_version=expected_state_version,
            expected_fencing_token=expected_fencing_token,
            from_states=(AgentOperationState.RESERVED,),
            to_state=AgentOperationState.RUNNING,
        )

    def complete(
        self,
        operation_key: str,
        *,
        owner: str,
        expected_state_version: int,
        expected_fencing_token: int,
        result: Mapping[str, object],
        side_effect_ref: str | None,
    ) -> AgentOperationRecord:
        payload = _json_object(result, "result")
        digest = hashlib.sha256(_canonical(payload)).hexdigest()
        return self._finish(
            operation_key,
            owner=owner,
            expected_state_version=expected_state_version,
            expected_fencing_token=expected_fencing_token,
            state=AgentOperationState.COMPLETED,
            result=payload,
            error=None,
            receipt_ref=f"agent-operation:{operation_key}:sha256:{digest}",
            result_digest=digest,
            side_effect_ref=side_effect_ref,
        )

    def fail(
        self,
        operation_key: str,
        *,
        owner: str,
        expected_state_version: int,
        expected_fencing_token: int,
        error: Mapping[str, object],
    ) -> AgentOperationRecord:
        payload = _json_object(error, "error")
        digest = hashlib.sha256(_canonical({"error": payload})).hexdigest()
        return self._finish(
            operation_key,
            owner=owner,
            expected_state_version=expected_state_version,
            expected_fencing_token=expected_fencing_token,
            state=AgentOperationState.FAILED,
            result=None,
            error=payload,
            receipt_ref=f"agent-operation:{operation_key}:sha256:{digest}",
            result_digest=digest,
            side_effect_ref=None,
        )

    def mark_unknown(
        self,
        operation_key: str,
        *,
        owner: str,
        expected_state_version: int,
        expected_fencing_token: int,
        error: Mapping[str, object],
    ) -> AgentOperationRecord:
        return self._finish(
            operation_key,
            owner=owner,
            expected_state_version=expected_state_version,
            expected_fencing_token=expected_fencing_token,
            state=AgentOperationState.UNKNOWN,
            result=None,
            error=_json_object(error, "error"),
            receipt_ref=None,
            result_digest=None,
            side_effect_ref=None,
        )

    def get(self, operation_key: str, *, owner: str) -> AgentOperationRecord:
        _operation_key(operation_key)
        _nonempty(owner, "owner")
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM agent_operations WHERE operation_key = ? AND owner = ?",
                (operation_key, owner),
            ).fetchone()
        if row is None:
            raise KeyError(operation_key)
        return _row_to_operation(row)

    def _assert_current_turn(
        self,
        conn: sqlite3.Connection,
        intent: AgentOperationIntent,
        *,
        expected_state_version: int,
        expected_fencing_token: int,
        now: str,
    ) -> None:
        row = conn.execute(
            """
            SELECT 1 FROM agent_turns
            WHERE turn_id = ? AND session_id = ? AND owner = ?
              AND state = 'running' AND cancel_requested = 0
              AND state_version = ? AND fencing_token = ?
              AND lease_expires_at > ?
            """,
            (
                intent.origin_turn_id,
                intent.session_id,
                intent.owner,
                expected_state_version,
                expected_fencing_token,
                now,
            ),
        ).fetchone()
        if row is None:
            raise AgentOperationConflict("operation Turn capability is stale or fenced")

    def _transition(
        self,
        operation_key: str,
        *,
        owner: str,
        invocation_id: str,
        expected_state_version: int,
        expected_fencing_token: int,
        from_states: tuple[AgentOperationState, ...],
        to_state: AgentOperationState,
    ) -> AgentOperationRecord:
        _operation_key(operation_key)
        _nonempty(owner, "owner")
        _nonempty(invocation_id, "invocation_id")
        _positive(expected_state_version, "expected_state_version")
        _positive(expected_fencing_token, "expected_fencing_token")
        now = self._now_text()
        placeholders = ",".join("?" for _ in from_states)
        with self.connect() as conn:
            updated = conn.execute(
                f"""
                UPDATE agent_operations
                SET state = ?, last_invocation_id = ?, updated_at = ?
                WHERE operation_key = ? AND owner = ? AND state IN ({placeholders})
                  AND EXISTS (
                      SELECT 1 FROM agent_turns
                      WHERE agent_turns.turn_id = agent_operations.origin_turn_id
                        AND agent_turns.owner = agent_operations.owner
                        AND agent_turns.state = 'running'
                        AND agent_turns.cancel_requested = 0
                        AND agent_turns.state_version = ?
                        AND agent_turns.fencing_token = ?
                        AND agent_turns.lease_expires_at > ?
                  )
                """,
                (
                    to_state.value,
                    invocation_id,
                    now,
                    operation_key,
                    owner,
                    *(state.value for state in from_states),
                    expected_state_version,
                    expected_fencing_token,
                    now,
                ),
            )
            if updated.rowcount != 1:
                raise AgentOperationConflict("operation is active, terminal, stale, or fenced")
            row = conn.execute(
                "SELECT * FROM agent_operations WHERE operation_key = ? AND owner = ?",
                (operation_key, owner),
            ).fetchone()
        assert row is not None
        return _row_to_operation(row)

    def _finish(
        self,
        operation_key: str,
        *,
        owner: str,
        expected_state_version: int,
        expected_fencing_token: int,
        state: AgentOperationState,
        result: Mapping[str, object] | None,
        error: Mapping[str, object] | None,
        receipt_ref: str | None,
        result_digest: str | None,
        side_effect_ref: str | None,
    ) -> AgentOperationRecord:
        _operation_key(operation_key)
        _nonempty(owner, "owner")
        _positive(expected_state_version, "expected_state_version")
        _positive(expected_fencing_token, "expected_fencing_token")
        result_json = (
            None if result is None else json.dumps(result, ensure_ascii=False, sort_keys=True)
        )
        error_json = (
            None if error is None else json.dumps(error, ensure_ascii=False, sort_keys=True)
        )
        now = self._now_text()
        with self.connect() as conn:
            updated = conn.execute(
                """
                UPDATE agent_operations
                SET state = ?, result_json = ?, error_json = ?, receipt_ref = ?,
                    result_digest = ?, side_effect_ref = ?, updated_at = ?
                WHERE operation_key = ? AND owner = ? AND state = 'running'
                  AND EXISTS (
                      SELECT 1 FROM agent_turns
                      WHERE agent_turns.turn_id = agent_operations.origin_turn_id
                        AND agent_turns.owner = agent_operations.owner
                        AND agent_turns.state = 'running'
                        AND agent_turns.cancel_requested = 0
                        AND agent_turns.state_version = ?
                        AND agent_turns.fencing_token = ?
                        AND agent_turns.lease_expires_at > ?
                  )
                """,
                (
                    state.value,
                    result_json,
                    error_json,
                    receipt_ref,
                    result_digest,
                    side_effect_ref,
                    now,
                    operation_key,
                    owner,
                    expected_state_version,
                    expected_fencing_token,
                    now,
                ),
            )
            if updated.rowcount != 1:
                existing = conn.execute(
                    "SELECT * FROM agent_operations WHERE operation_key = ? AND owner = ?",
                    (operation_key, owner),
                ).fetchone()
                if existing is not None:
                    record = _row_to_operation(existing)
                    if (
                        record.state is state
                        and record.result == (None if result is None else dict(result))
                        and record.error == (None if error is None else dict(error))
                        and record.receipt_ref == receipt_ref
                        and record.result_digest == result_digest
                    ):
                        return record
                raise AgentOperationConflict("operation is terminal, stale, or fenced")
            row = conn.execute(
                "SELECT * FROM agent_operations WHERE operation_key = ? AND owner = ?",
                (operation_key, owner),
            ).fetchone()
        assert row is not None
        return _row_to_operation(row)

    def _now_text(self) -> str:
        return _timestamp(self._clock())


class PostgresAgentOperationLedger:
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
        self._psycopg = importlib.import_module("psycopg")
        self._dict_row = importlib.import_module("psycopg.rows").dict_row
        self._jsonb = importlib.import_module("psycopg.types.json").Jsonb
        initialize_postgres_domain_schema(dsn)
        self._ensure_schema()

    def connect(self) -> Any:
        return self._psycopg.connect(self.dsn, row_factory=self._dict_row)

    def reserve(
        self,
        intent: AgentOperationIntent,
        *,
        invocation_id: str,
        expected_state_version: int,
        expected_fencing_token: int,
    ) -> tuple[AgentOperationRecord, bool]:
        _operation_request(intent, invocation_id, expected_state_version, expected_fencing_token)
        now = self._now()
        with self.connect() as conn:
            valid = conn.execute(
                """
                SELECT 1 FROM agent_turns
                WHERE turn_id = %s AND session_id = %s AND owner = %s
                  AND state = 'running' AND cancel_requested = 0
                  AND state_version = %s AND fencing_token = %s
                  AND lease_expires_at > %s
                """,
                (
                    intent.origin_turn_id,
                    intent.session_id,
                    intent.owner,
                    expected_state_version,
                    expected_fencing_token,
                    now,
                ),
            ).fetchone()
            if valid is None:
                raise AgentOperationConflict("operation Turn capability is stale or fenced")
            row = conn.execute(
                """
                INSERT INTO agent_operations (
                    operation_key, owner, session_id, origin_turn_id, request_key,
                    tool_name, intent_digest, target_ref, target_revision, state,
                    origin_invocation_id, last_invocation_id, result_json, error_json,
                    receipt_ref, result_digest, side_effect_ref, reconciliation_attempt,
                    created_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'reserved', %s, %s,
                          NULL, NULL, NULL, NULL, NULL, 0, %s, %s)
                ON CONFLICT DO NOTHING
                RETURNING *
                """,
                (
                    intent.operation_key,
                    intent.owner,
                    intent.session_id,
                    intent.origin_turn_id,
                    intent.request_key,
                    intent.tool_name,
                    intent.intent_digest,
                    intent.target_ref,
                    intent.target_revision,
                    invocation_id,
                    invocation_id,
                    now,
                    now,
                ),
            ).fetchone()
            created = row is not None
            if row is None:
                row = conn.execute(
                    "SELECT * FROM agent_operations WHERE operation_key = %s AND owner = %s",
                    (intent.operation_key, intent.owner),
                ).fetchone()
            if row is None:
                raise RuntimeError("operation insert did not produce a row")
            record = _row_to_operation(row)
            _ensure_intent(record, intent)
            if not created:
                row = conn.execute(
                    """
                    UPDATE agent_operations SET last_invocation_id = %s, updated_at = %s
                    WHERE operation_key = %s AND owner = %s RETURNING *
                    """,
                    (invocation_id, now, intent.operation_key, intent.owner),
                ).fetchone()
                assert row is not None
                record = _row_to_operation(row)
        return record, created

    def start(
        self,
        operation_key: str,
        *,
        owner: str,
        invocation_id: str,
        expected_state_version: int,
        expected_fencing_token: int,
    ) -> AgentOperationRecord:
        return self._transition(
            operation_key,
            owner=owner,
            invocation_id=invocation_id,
            expected_state_version=expected_state_version,
            expected_fencing_token=expected_fencing_token,
            from_states=(AgentOperationState.RESERVED,),
            to_state=AgentOperationState.RUNNING,
        )

    def complete(
        self,
        operation_key: str,
        *,
        owner: str,
        expected_state_version: int,
        expected_fencing_token: int,
        result: Mapping[str, object],
        side_effect_ref: str | None,
    ) -> AgentOperationRecord:
        payload = _json_object(result, "result")
        digest = hashlib.sha256(_canonical(payload)).hexdigest()
        return self._finish(
            operation_key,
            owner=owner,
            expected_state_version=expected_state_version,
            expected_fencing_token=expected_fencing_token,
            state=AgentOperationState.COMPLETED,
            result=payload,
            error=None,
            receipt_ref=f"agent-operation:{operation_key}:sha256:{digest}",
            result_digest=digest,
            side_effect_ref=side_effect_ref,
        )

    def fail(
        self,
        operation_key: str,
        *,
        owner: str,
        expected_state_version: int,
        expected_fencing_token: int,
        error: Mapping[str, object],
    ) -> AgentOperationRecord:
        payload = _json_object(error, "error")
        digest = hashlib.sha256(_canonical({"error": payload})).hexdigest()
        return self._finish(
            operation_key,
            owner=owner,
            expected_state_version=expected_state_version,
            expected_fencing_token=expected_fencing_token,
            state=AgentOperationState.FAILED,
            result=None,
            error=payload,
            receipt_ref=f"agent-operation:{operation_key}:sha256:{digest}",
            result_digest=digest,
            side_effect_ref=None,
        )

    def mark_unknown(
        self,
        operation_key: str,
        *,
        owner: str,
        expected_state_version: int,
        expected_fencing_token: int,
        error: Mapping[str, object],
    ) -> AgentOperationRecord:
        return self._finish(
            operation_key,
            owner=owner,
            expected_state_version=expected_state_version,
            expected_fencing_token=expected_fencing_token,
            state=AgentOperationState.UNKNOWN,
            result=None,
            error=_json_object(error, "error"),
            receipt_ref=None,
            result_digest=None,
            side_effect_ref=None,
        )

    def get(self, operation_key: str, *, owner: str) -> AgentOperationRecord:
        _operation_key(operation_key)
        _nonempty(owner, "owner")
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM agent_operations WHERE operation_key = %s AND owner = %s",
                (operation_key, owner),
            ).fetchone()
        if row is None:
            raise KeyError(operation_key)
        return _row_to_operation(row)

    def _transition(
        self,
        operation_key: str,
        *,
        owner: str,
        invocation_id: str,
        expected_state_version: int,
        expected_fencing_token: int,
        from_states: tuple[AgentOperationState, ...],
        to_state: AgentOperationState,
    ) -> AgentOperationRecord:
        _operation_key(operation_key)
        _nonempty(owner, "owner")
        _nonempty(invocation_id, "invocation_id")
        _positive(expected_state_version, "expected_state_version")
        _positive(expected_fencing_token, "expected_fencing_token")
        now = self._now()
        with self.connect() as conn:
            row = conn.execute(
                """
                UPDATE agent_operations
                SET state = %s, last_invocation_id = %s, updated_at = %s
                WHERE operation_key = %s AND owner = %s AND state = ANY(%s)
                  AND EXISTS (
                      SELECT 1 FROM agent_turns
                      WHERE agent_turns.turn_id = agent_operations.origin_turn_id
                        AND agent_turns.owner = agent_operations.owner
                        AND agent_turns.state = 'running'
                        AND agent_turns.cancel_requested = 0
                        AND agent_turns.state_version = %s
                        AND agent_turns.fencing_token = %s
                        AND agent_turns.lease_expires_at > %s
                  )
                RETURNING *
                """,
                (
                    to_state.value,
                    invocation_id,
                    now,
                    operation_key,
                    owner,
                    [state.value for state in from_states],
                    expected_state_version,
                    expected_fencing_token,
                    now,
                ),
            ).fetchone()
        if row is None:
            raise AgentOperationConflict("operation is active, terminal, stale, or fenced")
        return _row_to_operation(row)

    def _finish(
        self,
        operation_key: str,
        *,
        owner: str,
        expected_state_version: int,
        expected_fencing_token: int,
        state: AgentOperationState,
        result: Mapping[str, object] | None,
        error: Mapping[str, object] | None,
        receipt_ref: str | None,
        result_digest: str | None,
        side_effect_ref: str | None,
    ) -> AgentOperationRecord:
        _operation_key(operation_key)
        _nonempty(owner, "owner")
        _positive(expected_state_version, "expected_state_version")
        _positive(expected_fencing_token, "expected_fencing_token")
        now = self._now()
        with self.connect() as conn:
            row = conn.execute(
                """
                UPDATE agent_operations
                SET state = %s, result_json = %s, error_json = %s, receipt_ref = %s,
                    result_digest = %s, side_effect_ref = %s, updated_at = %s
                WHERE operation_key = %s AND owner = %s AND state = 'running'
                  AND EXISTS (
                      SELECT 1 FROM agent_turns
                      WHERE agent_turns.turn_id = agent_operations.origin_turn_id
                        AND agent_turns.owner = agent_operations.owner
                        AND agent_turns.state = 'running'
                        AND agent_turns.cancel_requested = 0
                        AND agent_turns.state_version = %s
                        AND agent_turns.fencing_token = %s
                        AND agent_turns.lease_expires_at > %s
                  )
                RETURNING *
                """,
                (
                    state.value,
                    None if result is None else self._jsonb(dict(result)),
                    None if error is None else self._jsonb(dict(error)),
                    receipt_ref,
                    result_digest,
                    side_effect_ref,
                    now,
                    operation_key,
                    owner,
                    expected_state_version,
                    expected_fencing_token,
                    now,
                ),
            ).fetchone()
            if row is None:
                existing = conn.execute(
                    "SELECT * FROM agent_operations WHERE operation_key = %s AND owner = %s",
                    (operation_key, owner),
                ).fetchone()
                if existing is not None:
                    record = _row_to_operation(existing)
                    if (
                        record.state is state
                        and record.result == (None if result is None else dict(result))
                        and record.error == (None if error is None else dict(error))
                        and record.receipt_ref == receipt_ref
                        and record.result_digest == result_digest
                    ):
                        return record
                raise AgentOperationConflict("operation is terminal, stale, or fenced")
        return _row_to_operation(row)

    def _ensure_schema(self) -> None:
        checksum = _migration_checksum(_POSTGRES_STATEMENTS)
        with self.connect() as conn, conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("pilot107:migrations",))
            existing = conn.execute(
                "SELECT checksum FROM schema_migrations WHERE migration_id = %s",
                (_POSTGRES_MIGRATION_ID,),
            ).fetchone()
            if existing is not None:
                if str(existing["checksum"]) != checksum:
                    raise RuntimeError(f"migration checksum changed: {_POSTGRES_MIGRATION_ID}")
                return
            for statement in _POSTGRES_STATEMENTS:
                conn.execute(statement)
            conn.execute(
                """
                INSERT INTO schema_migrations (migration_id, checksum, applied_at)
                VALUES (%s, %s, %s)
                """,
                (_POSTGRES_MIGRATION_ID, checksum, datetime.now(UTC)),
            )

    def _now(self) -> datetime:
        current = self._clock()
        if current.tzinfo is None:
            raise ValueError("operation ledger clock must be timezone-aware")
        return current.astimezone(UTC)


def _operation_request(
    intent: AgentOperationIntent,
    invocation_id: str,
    expected_state_version: int,
    expected_fencing_token: int,
) -> None:
    if not isinstance(intent, AgentOperationIntent):
        raise TypeError("intent must be an AgentOperationIntent")
    _nonempty(invocation_id, "invocation_id")
    _positive(expected_state_version, "expected_state_version")
    _positive(expected_fencing_token, "expected_fencing_token")


def _ensure_intent(record: AgentOperationRecord, intent: AgentOperationIntent) -> None:
    if (
        record.owner != intent.owner
        or record.session_id != intent.session_id
        or record.request_key != intent.request_key
        or record.tool_name != intent.tool_name
        or record.intent_digest != intent.intent_digest
        or record.target_ref != intent.target_ref
        or record.target_revision != intent.target_revision
    ):
        raise AgentOperationConflict("operation_key refers to different intent content")


def _domain_request_key(arguments: Mapping[str, object], *, fallback: str) -> str:
    candidate = arguments.get("request_key")
    if isinstance(candidate, str) and candidate:
        return _nonempty(candidate, "request_key")
    return _nonempty(fallback, "request_key")


def _target_ref(arguments: Mapping[str, object]) -> str | None:
    workspace_id = arguments.get("workspace_id")
    if isinstance(workspace_id, str) and workspace_id:
        return f"workspace:{workspace_id}"
    project_id = arguments.get("project_id")
    if isinstance(project_id, str) and project_id:
        return f"project:{project_id}"
    run_id = arguments.get("run_id")
    if isinstance(run_id, str) and run_id:
        return f"run:{run_id}"
    return None


def _target_revision(arguments: Mapping[str, object]) -> str | None:
    for key, prefix in (
        ("expected_version", "version"),
        ("expected_project_version", "project-version"),
        ("expected_workspace_snapshot_digest", "workspace-snapshot"),
        ("change_set_id", "changeset"),
        ("base_change_set_id", "base-changeset"),
    ):
        value = arguments.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return f"{prefix}:{value}"
        if isinstance(value, str) and value:
            return f"{prefix}:{value}"
    patches = arguments.get("patches")
    if isinstance(patches, list):
        source_digests: list[str | None] = []
        for patch in patches:
            if not isinstance(patch, Mapping):
                return None
            value = patch.get("expected_source_digest")
            source_digests.append(value if isinstance(value, str) else None)
        digest = hashlib.sha256(_canonical(source_digests)).hexdigest()
        return f"sources-sha256:{digest}"
    return None


def _row_to_operation(row: Mapping[str, Any]) -> AgentOperationRecord:
    return AgentOperationRecord(
        operation_key=str(row["operation_key"]),
        owner=str(row["owner"]),
        session_id=str(row["session_id"]),
        origin_turn_id=str(row["origin_turn_id"]),
        request_key=str(row["request_key"]),
        tool_name=str(row["tool_name"]),
        intent_digest=str(row["intent_digest"]),
        target_ref=_optional_text(row["target_ref"]),
        target_revision=_optional_text(row["target_revision"]),
        state=AgentOperationState(str(row["state"])),
        origin_invocation_id=str(row["origin_invocation_id"]),
        last_invocation_id=str(row["last_invocation_id"]),
        result=_json_mapping(row["result_json"]),
        error=_json_mapping(row["error_json"]),
        receipt_ref=_optional_text(row["receipt_ref"]),
        result_digest=_optional_text(row["result_digest"]),
        side_effect_ref=_optional_text(row["side_effect_ref"]),
        reconciliation_attempt=int(row["reconciliation_attempt"]),
        created_at=_timestamp_value(row["created_at"]),
        updated_at=_timestamp_value(row["updated_at"]),
    )


def _json_mapping(value: object) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, Mapping):
        raise TypeError("operation JSON payload is invalid")
    return dict(value)


def _json_object(value: Mapping[str, object], label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} must be an object")
    try:
        encoded = json.dumps(
            dict(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be finite JSON") from exc
    if len(encoded.encode("utf-8")) > 1_048_576:
        raise ValueError(f"{label} exceeds 1 MiB")
    decoded = json.loads(encoded)
    assert isinstance(decoded, dict)
    return decoded


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _migration_checksum(statements: tuple[str, ...]) -> str:
    digest = hashlib.sha256()
    for statement in statements:
        digest.update(statement.strip().encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def _operation_key(value: str) -> str:
    if not isinstance(value, str) or _OPERATION_ID.fullmatch(value) is None:
        raise ValueError("operation_key is invalid")
    return value


def _nonempty(value: str, label: str) -> str:
    if not isinstance(value, str) or not value or "\0" in value or len(value) > 512:
        raise ValueError(f"{label} is invalid")
    return value


def _positive(value: int, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{label} must be positive")
    return value


def _optional_text(value: object) -> str | None:
    return None if value is None else str(value)


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("operation ledger clock must be timezone-aware")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _timestamp_value(value: object) -> str:
    if isinstance(value, datetime):
        return _timestamp(value)
    return str(value)
