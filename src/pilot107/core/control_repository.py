"""Repository contract for multi-instance leases and durable outbox delivery.

The control repository is intentionally independent from Run and remediation
domain tables.  It is the small consistency boundary shared by submit,
reconcile, collection, and Agent workers as those paths move to multi-instance
execution.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from pilot107.core.redaction import redact_sensitive_text
from pilot107.core.schema_migrations import SchemaMigration, apply_schema_migrations

_NAME = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,255}$")


class ControlRepositoryConflict(RuntimeError):
    """Raised when a stale fencing token attempts to complete owned work."""


@dataclass(frozen=True)
class LeaseClaim:
    resource_kind: str
    resource_id: str
    owner: str
    fencing_token: int
    expires_at: str


@dataclass(frozen=True)
class OutboxMessage:
    message_id: str
    topic: str
    aggregate_id: str
    payload: dict[str, Any]
    state: str
    available_at: str
    lease_owner: str | None
    lease_expires_at: str | None
    fencing_token: int
    attempts: int
    last_error: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class OutboxQueueMetric:
    topic: str
    state: str
    messages: int
    attempts: int
    reclaims: int


@dataclass(frozen=True)
class OutboxMetricsSnapshot:
    queues: tuple[OutboxQueueMetric, ...]
    due_pending: int
    expired_running: int


@dataclass(frozen=True)
class ControlTrace:
    trace_id: str
    request_id: str
    method: str
    route: str
    status: int
    actor: str | None
    run_id: str | None
    job_id: str | None
    session_id: str | None
    created_at: str


class ControlRepository(Protocol):
    """Backend-neutral consistency contract used by control-plane workers."""

    def acquire_lease(
        self,
        *,
        resource_kind: str,
        resource_id: str,
        owner: str,
        lease_seconds: int,
    ) -> LeaseClaim | None: ...

    def renew_lease(self, claim: LeaseClaim, *, lease_seconds: int) -> LeaseClaim: ...

    def release_lease(self, claim: LeaseClaim) -> bool: ...

    def enqueue(
        self,
        *,
        message_id: str,
        topic: str,
        aggregate_id: str,
        payload: dict[str, Any],
        available_at: str | None = None,
    ) -> tuple[OutboxMessage, bool]: ...

    def claim_outbox(
        self,
        *,
        owner: str,
        limit: int,
        lease_seconds: int,
        topics: tuple[str, ...] = (),
    ) -> list[OutboxMessage]: ...

    def claim_outbox_message(
        self,
        *,
        message_id: str,
        owner: str,
        lease_seconds: int,
    ) -> OutboxMessage | None: ...

    def renew_outbox(
        self,
        *,
        message_id: str,
        owner: str,
        fencing_token: int,
        lease_seconds: int,
    ) -> OutboxMessage: ...

    def acknowledge(self, *, message_id: str, owner: str, fencing_token: int) -> None: ...

    def retry(
        self,
        *,
        message_id: str,
        owner: str,
        fencing_token: int,
        error: str,
        delay_seconds: int,
        max_attempts: int,
    ) -> OutboxMessage: ...

    def get_outbox(self, message_id: str) -> OutboxMessage: ...

    def outbox_metrics(self) -> OutboxMetricsSnapshot: ...

    def record_trace(
        self,
        *,
        trace_id: str,
        request_id: str,
        method: str,
        route: str,
        status: int,
        actor: str | None = None,
        run_id: str | None = None,
        job_id: str | None = None,
        session_id: str | None = None,
    ) -> ControlTrace: ...

    def list_traces(
        self,
        *,
        request_id: str | None = None,
        run_id: str | None = None,
        job_id: str | None = None,
        session_id: str | None = None,
        limit: int = 100,
    ) -> list[ControlTrace]: ...


CONTROL_MIGRATIONS = (
    SchemaMigration(
        migration_id="003g.001.control_leases_outbox",
        statements=(
            """
            CREATE TABLE control_leases (
                resource_kind TEXT NOT NULL,
                resource_id TEXT NOT NULL,
                owner TEXT,
                fencing_token INTEGER NOT NULL,
                expires_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (resource_kind, resource_id),
                CHECK (fencing_token > 0)
            )
            """,
            """
            CREATE INDEX idx_control_leases_expiry
            ON control_leases(expires_at, resource_kind)
            """,
            """
            CREATE TABLE control_outbox (
                message_id TEXT PRIMARY KEY,
                topic TEXT NOT NULL,
                aggregate_id TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                state TEXT NOT NULL,
                available_at TEXT NOT NULL,
                lease_owner TEXT,
                lease_expires_at TEXT,
                fencing_token INTEGER NOT NULL DEFAULT 0,
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                CHECK (state IN ('pending', 'running', 'succeeded', 'dead_letter')),
                CHECK (fencing_token >= 0),
                CHECK (attempts >= 0)
            )
            """,
            """
            CREATE INDEX idx_control_outbox_due
            ON control_outbox(state, available_at, lease_expires_at, created_at)
            """,
            """
            CREATE INDEX idx_control_outbox_topic_due
            ON control_outbox(topic, state, available_at, created_at)
            """,
        ),
    ),
    SchemaMigration(
        migration_id="003g.002.control_trace",
        statements=(
            """
            CREATE TABLE control_traces (
                trace_id TEXT PRIMARY KEY,
                request_id TEXT NOT NULL,
                method TEXT NOT NULL,
                route TEXT NOT NULL,
                status INTEGER NOT NULL,
                actor TEXT,
                run_id TEXT,
                job_id TEXT,
                session_id TEXT,
                created_at TEXT NOT NULL,
                CHECK (status >= 100 AND status <= 599)
            )
            """,
            "CREATE INDEX idx_control_traces_request ON control_traces(request_id, created_at)",
            "CREATE INDEX idx_control_traces_run ON control_traces(run_id, created_at)",
            "CREATE INDEX idx_control_traces_job ON control_traces(job_id, created_at)",
            "CREATE INDEX idx_control_traces_session ON control_traces(session_id, created_at)",
        ),
    ),
)


class SQLiteControlRepository:
    """SQLite reference implementation of :class:`ControlRepository`."""

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
            apply_schema_migrations(conn, CONTROL_MIGRATIONS)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 10000")
        return conn

    def acquire_lease(
        self,
        *,
        resource_kind: str,
        resource_id: str,
        owner: str,
        lease_seconds: int,
    ) -> LeaseClaim | None:
        _validate_key(resource_kind, "resource_kind", _NAME)
        _validate_key(resource_id, "resource_id", _IDENTIFIER)
        _validate_key(owner, "owner", _IDENTIFIER)
        _require_positive(lease_seconds, "lease_seconds")
        now = self._now()
        expires_at = self._after(lease_seconds)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM control_leases WHERE resource_kind = ? AND resource_id = ?",
                (resource_kind, resource_id),
            ).fetchone()
            if row is None:
                token = 1
                conn.execute(
                    """
                    INSERT INTO control_leases (
                        resource_kind, resource_id, owner, fencing_token, expires_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (resource_kind, resource_id, owner, token, expires_at, now),
                )
            elif str(row["owner"] or "") == owner and str(row["expires_at"]) > now:
                token = int(row["fencing_token"])
                conn.execute(
                    """
                    UPDATE control_leases SET expires_at = ?, updated_at = ?
                    WHERE resource_kind = ? AND resource_id = ?
                    """,
                    (expires_at, now, resource_kind, resource_id),
                )
            elif str(row["expires_at"]) <= now:
                token = int(row["fencing_token"]) + 1
                conn.execute(
                    """
                    UPDATE control_leases
                    SET owner = ?, fencing_token = ?, expires_at = ?, updated_at = ?
                    WHERE resource_kind = ? AND resource_id = ?
                    """,
                    (owner, token, expires_at, now, resource_kind, resource_id),
                )
            else:
                return None
        return LeaseClaim(resource_kind, resource_id, owner, token, expires_at)

    def renew_lease(self, claim: LeaseClaim, *, lease_seconds: int) -> LeaseClaim:
        _require_positive(lease_seconds, "lease_seconds")
        now = self._now()
        expires_at = self._after(lease_seconds)
        with self.connect() as conn:
            result = conn.execute(
                """
                UPDATE control_leases SET expires_at = ?, updated_at = ?
                WHERE resource_kind = ? AND resource_id = ? AND owner = ?
                  AND fencing_token = ? AND expires_at > ?
                """,
                (
                    expires_at,
                    now,
                    claim.resource_kind,
                    claim.resource_id,
                    claim.owner,
                    claim.fencing_token,
                    now,
                ),
            )
        if result.rowcount != 1:
            raise ControlRepositoryConflict("lease is expired or fenced by another owner")
        return LeaseClaim(
            claim.resource_kind,
            claim.resource_id,
            claim.owner,
            claim.fencing_token,
            expires_at,
        )

    def release_lease(self, claim: LeaseClaim) -> bool:
        now = self._now()
        with self.connect() as conn:
            result = conn.execute(
                """
                UPDATE control_leases SET owner = NULL, expires_at = ?, updated_at = ?
                WHERE resource_kind = ? AND resource_id = ? AND owner = ?
                  AND fencing_token = ?
                """,
                (
                    now,
                    now,
                    claim.resource_kind,
                    claim.resource_id,
                    claim.owner,
                    claim.fencing_token,
                ),
            )
        return result.rowcount == 1

    def enqueue(
        self,
        *,
        message_id: str,
        topic: str,
        aggregate_id: str,
        payload: dict[str, Any],
        available_at: str | None = None,
    ) -> tuple[OutboxMessage, bool]:
        _validate_key(message_id, "message_id", _IDENTIFIER)
        _validate_key(topic, "topic", _NAME)
        _validate_key(aggregate_id, "aggregate_id", _IDENTIFIER)
        if not isinstance(payload, dict):
            raise TypeError("payload must be an object")
        now = self._now()
        due = available_at or now
        _parse_timestamp(due, "available_at")
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with self.connect() as conn:
            result = conn.execute(
                """
                INSERT OR IGNORE INTO control_outbox (
                    message_id, topic, aggregate_id, payload_json, state, available_at,
                    lease_owner, lease_expires_at, fencing_token, attempts, last_error,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'pending', ?, NULL, NULL, 0, 0, NULL, ?, ?)
                """,
                (message_id, topic, aggregate_id, encoded, due, now, now),
            )
            row = conn.execute(
                "SELECT * FROM control_outbox WHERE message_id = ?", (message_id,)
            ).fetchone()
        if row is None:
            raise RuntimeError("outbox insert did not produce a row")
        message = _row_to_outbox(row)
        if not result.rowcount and (
            message.topic != topic
            or message.aggregate_id != aggregate_id
            or message.payload != payload
        ):
            raise ControlRepositoryConflict("message_id refers to different outbox content")
        return message, result.rowcount == 1

    def claim_outbox(
        self,
        *,
        owner: str,
        limit: int,
        lease_seconds: int,
        topics: tuple[str, ...] = (),
    ) -> list[OutboxMessage]:
        _validate_key(owner, "owner", _IDENTIFIER)
        if limit <= 0 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000")
        _require_positive(lease_seconds, "lease_seconds")
        for topic in topics:
            _validate_key(topic, "topic", _NAME)
        now = self._now()
        expires_at = self._after(lease_seconds)
        topic_sql = ""
        parameters: list[Any] = [now, now]
        if topics:
            topic_sql = f" AND topic IN ({','.join('?' for _ in topics)})"
            parameters.extend(topics)
        parameters.append(limit)
        claimed: list[OutboxMessage] = []
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """
                SELECT * FROM control_outbox
                WHERE (
                    (state = 'pending' AND available_at <= ?)
                    OR (state = 'running' AND lease_expires_at <= ?)
                )
                """
                + topic_sql
                + " ORDER BY available_at, created_at, message_id LIMIT ?",
                parameters,
            ).fetchall()
            for row in rows:
                token = int(row["fencing_token"]) + 1
                result = conn.execute(
                    """
                    UPDATE control_outbox
                    SET state = 'running', lease_owner = ?, lease_expires_at = ?,
                        fencing_token = ?, attempts = attempts + 1, updated_at = ?
                    WHERE message_id = ? AND fencing_token = ?
                      AND ((state = 'pending' AND available_at <= ?)
                           OR (state = 'running' AND lease_expires_at <= ?))
                    """,
                    (
                        owner,
                        expires_at,
                        token,
                        now,
                        str(row["message_id"]),
                        int(row["fencing_token"]),
                        now,
                        now,
                    ),
                )
                if result.rowcount != 1:
                    continue
                current = conn.execute(
                    "SELECT * FROM control_outbox WHERE message_id = ?",
                    (str(row["message_id"]),),
                ).fetchone()
                if current is not None:
                    claimed.append(_row_to_outbox(current))
        return claimed

    def claim_outbox_message(
        self,
        *,
        message_id: str,
        owner: str,
        lease_seconds: int,
    ) -> OutboxMessage | None:
        _validate_key(message_id, "message_id", _IDENTIFIER)
        _validate_key(owner, "owner", _IDENTIFIER)
        _require_positive(lease_seconds, "lease_seconds")
        now = self._now()
        expires_at = self._after(lease_seconds)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT * FROM control_outbox
                WHERE message_id = ?
                  AND ((state = 'pending' AND available_at <= ?)
                       OR (state = 'running' AND lease_expires_at <= ?))
                """,
                (message_id, now, now),
            ).fetchone()
            if row is None:
                return None
            token = int(row["fencing_token"]) + 1
            result = conn.execute(
                """
                UPDATE control_outbox
                SET state = 'running', lease_owner = ?, lease_expires_at = ?,
                    fencing_token = ?, attempts = attempts + 1, updated_at = ?
                WHERE message_id = ? AND fencing_token = ?
                  AND ((state = 'pending' AND available_at <= ?)
                       OR (state = 'running' AND lease_expires_at <= ?))
                """,
                (
                    owner,
                    expires_at,
                    token,
                    now,
                    message_id,
                    int(row["fencing_token"]),
                    now,
                    now,
                ),
            )
            if result.rowcount != 1:
                return None
            current = conn.execute(
                "SELECT * FROM control_outbox WHERE message_id = ?", (message_id,)
            ).fetchone()
        if current is None:
            raise RuntimeError("claimed outbox message disappeared")
        return _row_to_outbox(current)

    def acknowledge(self, *, message_id: str, owner: str, fencing_token: int) -> None:
        self._finish(
            message_id=message_id,
            owner=owner,
            fencing_token=fencing_token,
            state="succeeded",
            available_at=None,
            error=None,
        )

    def renew_outbox(
        self,
        *,
        message_id: str,
        owner: str,
        fencing_token: int,
        lease_seconds: int,
    ) -> OutboxMessage:
        _validate_key(message_id, "message_id", _IDENTIFIER)
        _validate_key(owner, "owner", _IDENTIFIER)
        _require_positive(fencing_token, "fencing_token")
        _require_positive(lease_seconds, "lease_seconds")
        now = self._now()
        expires_at = self._after(lease_seconds)
        with self.connect() as conn:
            result = conn.execute(
                """
                UPDATE control_outbox
                SET lease_expires_at = ?, updated_at = ?
                WHERE message_id = ? AND state = 'running'
                  AND lease_owner = ? AND fencing_token = ?
                  AND lease_expires_at > ?
                """,
                (expires_at, now, message_id, owner, fencing_token, now),
            )
        if result.rowcount != 1:
            raise ControlRepositoryConflict("outbox lease is expired or fenced")
        return self.get_outbox(message_id)

    def retry(
        self,
        *,
        message_id: str,
        owner: str,
        fencing_token: int,
        error: str,
        delay_seconds: int,
        max_attempts: int,
    ) -> OutboxMessage:
        if delay_seconds < 0:
            raise ValueError("delay_seconds must not be negative")
        _require_positive(max_attempts, "max_attempts")
        current = self.get_outbox(message_id)
        target = "dead_letter" if current.attempts >= max_attempts else "pending"
        available_at = self._after(delay_seconds)
        self._finish(
            message_id=message_id,
            owner=owner,
            fencing_token=fencing_token,
            state=target,
            available_at=available_at,
            error=redact_sensitive_text(error[:2000]),
        )
        return self.get_outbox(message_id)

    def get_outbox(self, message_id: str) -> OutboxMessage:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM control_outbox WHERE message_id = ?", (message_id,)
            ).fetchone()
        if row is None:
            raise KeyError(message_id)
        return _row_to_outbox(row)

    def outbox_metrics(self) -> OutboxMetricsSnapshot:
        now = self._now()
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT topic, state, COUNT(*) AS messages,
                       COALESCE(SUM(attempts), 0) AS attempts,
                       COALESCE(SUM(CASE WHEN attempts > 1 THEN attempts - 1 ELSE 0 END), 0)
                           AS reclaims
                FROM control_outbox
                GROUP BY topic, state
                ORDER BY topic, state
                """
            ).fetchall()
            due_pending = conn.execute(
                "SELECT COUNT(*) FROM control_outbox WHERE state = 'pending' AND available_at <= ?",
                (now,),
            ).fetchone()
            expired_running = conn.execute(
                "SELECT COUNT(*) FROM control_outbox "
                "WHERE state = 'running' AND lease_expires_at <= ?",
                (now,),
            ).fetchone()
        return OutboxMetricsSnapshot(
            queues=tuple(
                OutboxQueueMetric(
                    topic=str(row["topic"]),
                    state=str(row["state"]),
                    messages=int(row["messages"]),
                    attempts=int(row["attempts"]),
                    reclaims=int(row["reclaims"]),
                )
                for row in rows
            ),
            due_pending=0 if due_pending is None else int(due_pending[0]),
            expired_running=0 if expired_running is None else int(expired_running[0]),
        )

    def record_trace(
        self,
        *,
        trace_id: str,
        request_id: str,
        method: str,
        route: str,
        status: int,
        actor: str | None = None,
        run_id: str | None = None,
        job_id: str | None = None,
        session_id: str | None = None,
    ) -> ControlTrace:
        _validate_key(trace_id, "trace_id", _IDENTIFIER)
        _validate_key(request_id, "request_id", _IDENTIFIER)
        _validate_trace_fields(method, route, status, actor, run_id, job_id, session_id)
        created_at = self._now()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO control_traces (
                    trace_id, request_id, method, route, status, actor,
                    run_id, job_id, session_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trace_id,
                    request_id,
                    method.upper(),
                    route,
                    status,
                    actor,
                    run_id,
                    job_id,
                    session_id,
                    created_at,
                ),
            )
            row = conn.execute(
                "SELECT * FROM control_traces WHERE trace_id = ?", (trace_id,)
            ).fetchone()
        if row is None:
            raise RuntimeError("trace insert did not produce a row")
        return _row_to_trace(row)

    def list_traces(
        self,
        *,
        request_id: str | None = None,
        run_id: str | None = None,
        job_id: str | None = None,
        session_id: str | None = None,
        limit: int = 100,
    ) -> list[ControlTrace]:
        filters = _trace_filters(request_id, run_id, job_id, session_id, limit)
        clauses = [f"{column} = ?" for column, _value in filters]
        parameters: list[Any] = [value for _column, value in filters]
        parameters.append(limit)
        where = " AND ".join(clauses) if clauses else "1 = 1"
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM control_traces WHERE "
                + where
                + " ORDER BY created_at DESC, trace_id DESC LIMIT ?",
                parameters,
            ).fetchall()
        return [_row_to_trace(row) for row in rows]

    def _finish(
        self,
        *,
        message_id: str,
        owner: str,
        fencing_token: int,
        state: str,
        available_at: str | None,
        error: str | None,
    ) -> None:
        if fencing_token <= 0:
            raise ValueError("fencing_token must be positive")
        now = self._now()
        with self.connect() as conn:
            result = conn.execute(
                """
                UPDATE control_outbox
                SET state = ?, available_at = COALESCE(?, available_at),
                    lease_owner = NULL, lease_expires_at = NULL,
                    last_error = ?, updated_at = ?
                WHERE message_id = ? AND state = 'running'
                  AND lease_owner = ? AND fencing_token = ?
                """,
                (state, available_at, error, now, message_id, owner, fencing_token),
            )
        if result.rowcount != 1:
            raise ControlRepositoryConflict("outbox ownership is stale or fenced")

    def _now(self) -> str:
        current = self._clock()
        if current.tzinfo is None:
            raise ValueError("repository clock must return a timezone-aware datetime")
        return current.astimezone(UTC).isoformat()

    def _after(self, seconds: int) -> str:
        current = self._clock()
        if current.tzinfo is None:
            raise ValueError("repository clock must return a timezone-aware datetime")
        return (current.astimezone(UTC) + timedelta(seconds=seconds)).isoformat()


def _row_to_outbox(row: sqlite3.Row) -> OutboxMessage:
    payload = json.loads(str(row["payload_json"]))
    if not isinstance(payload, dict):
        raise RuntimeError("outbox payload is not an object")
    return OutboxMessage(
        message_id=str(row["message_id"]),
        topic=str(row["topic"]),
        aggregate_id=str(row["aggregate_id"]),
        payload=payload,
        state=str(row["state"]),
        available_at=str(row["available_at"]),
        lease_owner=str(row["lease_owner"]) if row["lease_owner"] is not None else None,
        lease_expires_at=(
            str(row["lease_expires_at"]) if row["lease_expires_at"] is not None else None
        ),
        fencing_token=int(row["fencing_token"]),
        attempts=int(row["attempts"]),
        last_error=str(row["last_error"]) if row["last_error"] is not None else None,
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _row_to_trace(row: sqlite3.Row) -> ControlTrace:
    return ControlTrace(
        trace_id=str(row["trace_id"]),
        request_id=str(row["request_id"]),
        method=str(row["method"]),
        route=str(row["route"]),
        status=int(row["status"]),
        actor=str(row["actor"]) if row["actor"] is not None else None,
        run_id=str(row["run_id"]) if row["run_id"] is not None else None,
        job_id=str(row["job_id"]) if row["job_id"] is not None else None,
        session_id=str(row["session_id"]) if row["session_id"] is not None else None,
        created_at=str(row["created_at"]),
    )


def _validate_trace_fields(
    method: str,
    route: str,
    status: int,
    actor: str | None,
    run_id: str | None,
    job_id: str | None,
    session_id: str | None,
) -> None:
    if not re.fullmatch(r"[A-Z]{3,10}", method.upper()):
        raise ValueError("method is invalid")
    _validate_trace_text(route, "route", 512)
    if not 100 <= status <= 599:
        raise ValueError("status must be between 100 and 599")
    if actor is not None:
        _validate_trace_text(actor, "actor", 255)
    for label, value in (("run_id", run_id), ("job_id", job_id), ("session_id", session_id)):
        if value is not None:
            _validate_key(value, label, _IDENTIFIER)


def _validate_trace_text(value: str, label: str, maximum: int) -> None:
    if not value or len(value) > maximum or any(character in value for character in "\r\n\0"):
        raise ValueError(f"{label} is invalid")


def _trace_filters(
    request_id: str | None,
    run_id: str | None,
    job_id: str | None,
    session_id: str | None,
    limit: int,
) -> list[tuple[str, str]]:
    if limit <= 0 or limit > 1000:
        raise ValueError("limit must be between 1 and 1000")
    filters: list[tuple[str, str]] = []
    for column, value in (
        ("request_id", request_id),
        ("run_id", run_id),
        ("job_id", job_id),
        ("session_id", session_id),
    ):
        if value is not None:
            _validate_key(value, column, _IDENTIFIER)
            filters.append((column, value))
    return filters


def _validate_key(value: str, label: str, pattern: re.Pattern[str]) -> None:
    if not pattern.fullmatch(value):
        raise ValueError(f"{label} is invalid")


def _require_positive(value: int, label: str) -> None:
    if value <= 0:
        raise ValueError(f"{label} must be positive")


def _parse_timestamp(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be ISO 8601") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{label} must include a timezone")
    return parsed
