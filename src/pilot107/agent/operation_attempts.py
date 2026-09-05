"""PostgreSQL-backed fencing for durable Agent side effects.

Operation receipts identify *what* logical mutation happened. This companion
store identifies *which Turn lease* was allowed to start an in-flight mutation.
The competition runtime has one persistence authority: PostgreSQL. SQLite is
not a supported fallback for operation attempts.
"""

from __future__ import annotations

import hashlib
import importlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol


class AgentOperationAttemptStatus(StrEnum):
    ACTIVE = "active"
    STALE = "stale"
    MISSING = "missing"


class AgentOperationAttemptConflict(RuntimeError):
    """Attempt preparation or fencing lost its authoritative Turn capability."""


@dataclass(frozen=True)
class AgentOperationAttemptRecord:
    operation_key: str
    owner: str
    session_id: str
    active_turn_id: str
    state_version: int
    fencing_token: int
    invocation_id: str
    started_at: str
    heartbeat_at: str


class AgentOperationAttemptStore(Protocol):
    def prepare(
        self,
        operation_key: str,
        *,
        owner: str,
        session_id: str,
        turn_id: str,
        state_version: int,
        fencing_token: int,
        invocation_id: str,
    ) -> AgentOperationAttemptRecord: ...

    def classify(
        self,
        operation_key: str,
        *,
        owner: str,
        turn_id: str,
        state_version: int,
        fencing_token: int,
    ) -> AgentOperationAttemptStatus: ...

    def mark_stale(
        self,
        operation_key: str,
        *,
        owner: str,
        session_id: str,
        current_turn_id: str,
        current_state_version: int,
        current_fencing_token: int,
        invocation_id: str,
    ) -> bool: ...

    def heartbeat(
        self,
        operation_key: str,
        *,
        owner: str,
        turn_id: str,
        state_version: int,
        fencing_token: int,
    ) -> bool: ...


def build_agent_operation_attempt_store(
    session_store: object,
    *,
    clock: Callable[[], datetime] | None = None,
) -> AgentOperationAttemptStore:
    """Build the operation-attempt store from the PostgreSQL session authority."""

    dsn = getattr(session_store, "dsn", None)
    if not isinstance(dsn, str) or not dsn:
        raise RuntimeError(
            "Agent operation attempts require a PostgreSQL-backed session store"
        )
    return PostgresAgentOperationAttemptStore(dsn, clock=clock)


_POSTGRES_MIGRATION_ID = "004a.035.agent_operation_attempts"
_POSTGRES_STATEMENTS = (
    """
    CREATE TABLE agent_operation_attempts (
        operation_key TEXT PRIMARY KEY REFERENCES agent_operations(operation_key),
        owner TEXT NOT NULL,
        session_id TEXT NOT NULL,
        active_turn_id TEXT NOT NULL REFERENCES agent_turns(turn_id),
        state_version BIGINT NOT NULL,
        fencing_token BIGINT NOT NULL,
        invocation_id TEXT NOT NULL,
        started_at TIMESTAMPTZ NOT NULL,
        heartbeat_at TIMESTAMPTZ NOT NULL,
        CHECK (state_version > 0),
        CHECK (fencing_token > 0)
    )
    """,
    """
    CREATE INDEX idx_agent_operation_attempts_owner_heartbeat
    ON agent_operation_attempts(owner, heartbeat_at, operation_key)
    """,
)


class PostgresAgentOperationAttemptStore:
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
        self._ensure_schema()

    def connect(self) -> Any:
        return self._psycopg.connect(self.dsn, row_factory=self._dict_row)

    def prepare(
        self,
        operation_key: str,
        *,
        owner: str,
        session_id: str,
        turn_id: str,
        state_version: int,
        fencing_token: int,
        invocation_id: str,
    ) -> AgentOperationAttemptRecord:
        _attempt_args(
            operation_key,
            owner,
            session_id,
            turn_id,
            state_version,
            fencing_token,
            invocation_id,
        )
        now = _clock_value(self._clock)
        with self.connect() as connection, connection.transaction():
            valid = connection.execute(
                """
                SELECT 1 FROM agent_operations operation
                JOIN agent_turns turn_row
                  ON turn_row.session_id = operation.session_id
                WHERE operation.operation_key = %s
                  AND operation.owner = %s
                  AND operation.session_id = %s
                  AND operation.state = 'reserved'
                  AND turn_row.turn_id = %s
                  AND turn_row.owner = %s
                  AND turn_row.state = 'running'
                  AND turn_row.cancel_requested = 0
                  AND turn_row.state_version = %s
                  AND turn_row.fencing_token = %s
                  AND turn_row.lease_expires_at > %s
                """,
                (
                    operation_key,
                    owner,
                    session_id,
                    turn_id,
                    owner,
                    state_version,
                    fencing_token,
                    now,
                ),
            ).fetchone()
            if valid is None:
                raise AgentOperationAttemptConflict(
                    "operation attempt is stale or fenced"
                )
            row = connection.execute(
                """
                INSERT INTO agent_operation_attempts (
                    operation_key,
                    owner,
                    session_id,
                    active_turn_id,
                    state_version,
                    fencing_token,
                    invocation_id,
                    started_at,
                    heartbeat_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT(operation_key) DO UPDATE SET
                    owner = EXCLUDED.owner,
                    session_id = EXCLUDED.session_id,
                    active_turn_id = EXCLUDED.active_turn_id,
                    state_version = EXCLUDED.state_version,
                    fencing_token = EXCLUDED.fencing_token,
                    invocation_id = EXCLUDED.invocation_id,
                    started_at = EXCLUDED.started_at,
                    heartbeat_at = EXCLUDED.heartbeat_at
                RETURNING *
                """,
                (
                    operation_key,
                    owner,
                    session_id,
                    turn_id,
                    state_version,
                    fencing_token,
                    invocation_id,
                    now,
                    now,
                ),
            ).fetchone()
        if row is None:
            raise RuntimeError("operation attempt disappeared")
        return _row_to_attempt(row)

    def classify(
        self,
        operation_key: str,
        *,
        owner: str,
        turn_id: str,
        state_version: int,
        fencing_token: int,
    ) -> AgentOperationAttemptStatus:
        _identity_args(operation_key, owner, turn_id, state_version, fencing_token)
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM agent_operation_attempts
                WHERE operation_key = %s AND owner = %s
                """,
                (operation_key, owner),
            ).fetchone()
        if row is None:
            return AgentOperationAttemptStatus.MISSING
        attempt = _row_to_attempt(row)
        if (
            attempt.active_turn_id == turn_id
            and attempt.state_version == state_version
            and attempt.fencing_token == fencing_token
        ):
            return AgentOperationAttemptStatus.ACTIVE
        return AgentOperationAttemptStatus.STALE

    def mark_stale(
        self,
        operation_key: str,
        *,
        owner: str,
        session_id: str,
        current_turn_id: str,
        current_state_version: int,
        current_fencing_token: int,
        invocation_id: str,
    ) -> bool:
        _attempt_args(
            operation_key,
            owner,
            session_id,
            current_turn_id,
            current_state_version,
            current_fencing_token,
            invocation_id,
        )
        now = _clock_value(self._clock)
        with self.connect() as connection, connection.transaction():
            current = connection.execute(
                """
                SELECT attempt.*
                FROM agent_operation_attempts attempt
                JOIN agent_operations operation
                  ON operation.operation_key = attempt.operation_key
                JOIN agent_turns turn_row
                  ON turn_row.session_id = operation.session_id
                WHERE attempt.operation_key = %s
                  AND attempt.owner = %s
                  AND operation.session_id = %s
                  AND operation.state = 'running'
                  AND turn_row.turn_id = %s
                  AND turn_row.owner = %s
                  AND turn_row.state = 'running'
                  AND turn_row.cancel_requested = 0
                  AND turn_row.state_version = %s
                  AND turn_row.fencing_token = %s
                  AND turn_row.lease_expires_at > %s
                FOR UPDATE
                """,
                (
                    operation_key,
                    owner,
                    session_id,
                    current_turn_id,
                    owner,
                    current_state_version,
                    current_fencing_token,
                    now,
                ),
            ).fetchone()
            if current is None:
                raise AgentOperationAttemptConflict("current Turn is stale or fenced")
            attempt = _row_to_attempt(current)
            if (
                attempt.active_turn_id == current_turn_id
                and attempt.state_version == current_state_version
                and attempt.fencing_token == current_fencing_token
            ):
                return False
            row = connection.execute(
                """
                UPDATE agent_operations
                SET state = 'stale',
                    last_invocation_id = %s,
                    updated_at = %s
                WHERE operation_key = %s
                  AND owner = %s
                  AND state = 'running'
                RETURNING operation_key
                """,
                (invocation_id, now, operation_key, owner),
            ).fetchone()
            return row is not None

    def heartbeat(
        self,
        operation_key: str,
        *,
        owner: str,
        turn_id: str,
        state_version: int,
        fencing_token: int,
    ) -> bool:
        _identity_args(operation_key, owner, turn_id, state_version, fencing_token)
        now = _clock_value(self._clock)
        with self.connect() as connection:
            row = connection.execute(
                """
                UPDATE agent_operation_attempts attempt
                SET heartbeat_at = %s
                FROM agent_operations operation, agent_turns turn_row
                WHERE attempt.operation_key = %s
                  AND attempt.owner = %s
                  AND attempt.active_turn_id = %s
                  AND attempt.state_version = %s
                  AND attempt.fencing_token = %s
                  AND operation.operation_key = attempt.operation_key
                  AND operation.owner = attempt.owner
                  AND operation.state = 'running'
                  AND turn_row.turn_id = %s
                  AND turn_row.owner = %s
                  AND turn_row.state = 'running'
                  AND turn_row.cancel_requested = 0
                  AND turn_row.state_version = %s
                  AND turn_row.fencing_token = %s
                  AND turn_row.lease_expires_at > %s
                RETURNING attempt.operation_key
                """,
                (
                    now,
                    operation_key,
                    owner,
                    turn_id,
                    state_version,
                    fencing_token,
                    turn_id,
                    owner,
                    state_version,
                    fencing_token,
                    now,
                ),
            ).fetchone()
        return row is not None

    def _ensure_schema(self) -> None:
        checksum = _migration_checksum(_POSTGRES_STATEMENTS)
        with self.connect() as connection, connection.transaction():
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                ("pilot107:migrations",),
            )
            existing = connection.execute(
                """
                SELECT checksum FROM schema_migrations
                WHERE migration_id = %s
                """,
                (_POSTGRES_MIGRATION_ID,),
            ).fetchone()
            if existing is not None:
                if str(existing["checksum"]) != checksum:
                    raise RuntimeError(
                        f"migration checksum changed: {_POSTGRES_MIGRATION_ID}"
                    )
                return
            for statement in _POSTGRES_STATEMENTS:
                connection.execute(statement)
            connection.execute(
                """
                INSERT INTO schema_migrations (
                    migration_id, checksum, applied_at
                ) VALUES (%s, %s, %s)
                """,
                (_POSTGRES_MIGRATION_ID, checksum, datetime.now(UTC)),
            )


def _row_to_attempt(row: Any) -> AgentOperationAttemptRecord:
    return AgentOperationAttemptRecord(
        operation_key=str(row["operation_key"]),
        owner=str(row["owner"]),
        session_id=str(row["session_id"]),
        active_turn_id=str(row["active_turn_id"]),
        state_version=int(row["state_version"]),
        fencing_token=int(row["fencing_token"]),
        invocation_id=str(row["invocation_id"]),
        started_at=_timestamp_value(row["started_at"]),
        heartbeat_at=_timestamp_value(row["heartbeat_at"]),
    )


def _attempt_args(
    operation_key: str,
    owner: str,
    session_id: str,
    turn_id: str,
    state_version: int,
    fencing_token: int,
    invocation_id: str,
) -> None:
    for value, label in (
        (operation_key, "operation_key"),
        (owner, "owner"),
        (session_id, "session_id"),
        (turn_id, "turn_id"),
        (invocation_id, "invocation_id"),
    ):
        _nonempty(value, label)
    _positive(state_version, "state_version")
    _positive(fencing_token, "fencing_token")


def _identity_args(
    operation_key: str,
    owner: str,
    turn_id: str,
    state_version: int,
    fencing_token: int,
) -> None:
    for value, label in (
        (operation_key, "operation_key"),
        (owner, "owner"),
        (turn_id, "turn_id"),
    ):
        _nonempty(value, label)
    _positive(state_version, "state_version")
    _positive(fencing_token, "fencing_token")


def _nonempty(value: str, label: str) -> str:
    if not isinstance(value, str) or not value or "\0" in value or len(value) > 512:
        raise ValueError(f"{label} is invalid")
    return value


def _positive(value: int, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{label} must be positive")
    return value


def _clock_value(clock: Callable[[], datetime]) -> datetime:
    value = clock()
    if value.tzinfo is None:
        raise ValueError("operation attempt clock must be timezone-aware")
    return value.astimezone(UTC)


def _timestamp_value(value: object) -> str:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return str(value)


def _migration_checksum(statements: tuple[str, ...]) -> str:
    digest = hashlib.sha256()
    for statement in statements:
        digest.update(statement.strip().encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()
