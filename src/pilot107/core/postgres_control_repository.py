"""PostgreSQL implementation of the fenced control repository contract."""

from __future__ import annotations

import hashlib
import importlib
import json
import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from pilot107.core.control_repository import (
    ControlRepositoryConflict,
    LeaseClaim,
    OutboxMessage,
)

_NAME = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,255}$")
_MIGRATION_ID = "003g.001.control_leases_outbox"

_POSTGRES_STATEMENTS = (
    """
    CREATE TABLE control_leases (
        resource_kind TEXT NOT NULL,
        resource_id TEXT NOT NULL,
        owner TEXT,
        fencing_token BIGINT NOT NULL,
        expires_at TIMESTAMPTZ NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL,
        PRIMARY KEY (resource_kind, resource_id),
        CHECK (fencing_token > 0)
    )
    """,
    "CREATE INDEX idx_control_leases_expiry ON control_leases(expires_at, resource_kind)",
    """
    CREATE TABLE control_outbox (
        message_id TEXT PRIMARY KEY,
        topic TEXT NOT NULL,
        aggregate_id TEXT NOT NULL,
        payload_json JSONB NOT NULL,
        state TEXT NOT NULL,
        available_at TIMESTAMPTZ NOT NULL,
        lease_owner TEXT,
        lease_expires_at TIMESTAMPTZ,
        fencing_token BIGINT NOT NULL DEFAULT 0,
        attempts INTEGER NOT NULL DEFAULT 0,
        last_error TEXT,
        created_at TIMESTAMPTZ NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL,
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
)


class PostgresDriverUnavailable(RuntimeError):
    """Raised when the optional PostgreSQL driver is not installed."""


class PostgresConfigurationError(RuntimeError):
    """Raised when the database cannot preserve repository text contracts."""


class PostgresControlRepository:
    """PostgreSQL implementation using row locks and ``SKIP LOCKED`` claims."""

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
        self._psycopg = _load_psycopg()
        self._dict_row = importlib.import_module("psycopg.rows").dict_row
        self._initialize()

    def connect(self) -> Any:
        return self._psycopg.connect(self.dsn, row_factory=self._dict_row)

    def _initialize(self) -> None:
        checksum = _migration_checksum(_POSTGRES_STATEMENTS)
        with self.connect() as conn, conn.transaction():
            encoding_row = conn.execute("SHOW server_encoding").fetchone()
            if encoding_row is None or _text(encoding_row["server_encoding"]).upper() != "UTF8":
                raise PostgresConfigurationError("PostgreSQL server_encoding must be UTF8")
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
            row = conn.execute(
                "SELECT checksum FROM schema_migrations WHERE migration_id = %s",
                (_MIGRATION_ID,),
            ).fetchone()
            if row is not None:
                if _text(row["checksum"]) != checksum:
                    raise RuntimeError(f"migration checksum changed: {_MIGRATION_ID}")
                return
            for statement in _POSTGRES_STATEMENTS:
                conn.execute(statement)
            conn.execute(
                """
                    INSERT INTO schema_migrations (migration_id, checksum, applied_at)
                    VALUES (%s, %s, %s)
                    """,
                (_MIGRATION_ID, checksum, self._now()),
            )

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
        expires_at = now + timedelta(seconds=lease_seconds)
        with self.connect() as conn, conn.transaction():
            inserted = conn.execute(
                """
                    INSERT INTO control_leases (
                        resource_kind, resource_id, owner, fencing_token, expires_at, updated_at
                    ) VALUES (%s, %s, %s, 1, %s, %s)
                    ON CONFLICT (resource_kind, resource_id) DO NOTHING
                    RETURNING fencing_token
                    """,
                (resource_kind, resource_id, owner, expires_at, now),
            ).fetchone()
            if inserted is not None:
                token = 1
            else:
                row = conn.execute(
                    """
                        SELECT * FROM control_leases
                        WHERE resource_kind = %s AND resource_id = %s
                        FOR UPDATE
                        """,
                    (resource_kind, resource_id),
                ).fetchone()
                if row is None:
                    raise RuntimeError("lease row disappeared while locked")
                if row["owner"] == owner and _as_utc(row["expires_at"]) > now:
                    token = int(row["fencing_token"])
                elif _as_utc(row["expires_at"]) <= now:
                    token = int(row["fencing_token"]) + 1
                else:
                    return None
                conn.execute(
                    """
                        UPDATE control_leases
                        SET owner = %s, fencing_token = %s, expires_at = %s, updated_at = %s
                        WHERE resource_kind = %s AND resource_id = %s
                        """,
                    (owner, token, expires_at, now, resource_kind, resource_id),
                )
        return LeaseClaim(resource_kind, resource_id, owner, token, expires_at.isoformat())

    def renew_lease(self, claim: LeaseClaim, *, lease_seconds: int) -> LeaseClaim:
        _require_positive(lease_seconds, "lease_seconds")
        now = self._now()
        expires_at = now + timedelta(seconds=lease_seconds)
        with self.connect() as conn:
            result = conn.execute(
                """
                UPDATE control_leases SET expires_at = %s, updated_at = %s
                WHERE resource_kind = %s AND resource_id = %s AND owner = %s
                  AND fencing_token = %s AND expires_at > %s
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
            expires_at.isoformat(),
        )

    def release_lease(self, claim: LeaseClaim) -> bool:
        now = self._now()
        with self.connect() as conn:
            result = conn.execute(
                """
                UPDATE control_leases SET owner = NULL, expires_at = %s, updated_at = %s
                WHERE resource_kind = %s AND resource_id = %s AND owner = %s
                  AND fencing_token = %s
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
        return int(result.rowcount) == 1

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
        due = _parse_timestamp(available_at, "available_at") if available_at else now
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with self.connect() as conn:
            inserted = conn.execute(
                """
                INSERT INTO control_outbox (
                    message_id, topic, aggregate_id, payload_json, state, available_at,
                    lease_owner, lease_expires_at, fencing_token, attempts, last_error,
                    created_at, updated_at
                ) VALUES (%s, %s, %s, %s::jsonb, 'pending', %s, NULL, NULL, 0, 0, NULL, %s, %s)
                ON CONFLICT (message_id) DO NOTHING
                RETURNING message_id
                """,
                (message_id, topic, aggregate_id, encoded, due, now, now),
            ).fetchone()
            row = conn.execute(
                "SELECT * FROM control_outbox WHERE message_id = %s", (message_id,)
            ).fetchone()
        if row is None:
            raise RuntimeError("outbox insert did not produce a row")
        message = _row_to_outbox(row)
        if inserted is None and (
            message.topic != topic
            or message.aggregate_id != aggregate_id
            or message.payload != payload
        ):
            raise ControlRepositoryConflict("message_id refers to different outbox content")
        return message, inserted is not None

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
        expires_at = now + timedelta(seconds=lease_seconds)
        topic_clause = ""
        parameters: list[Any] = [now, now]
        if topics:
            topic_clause = " AND topic = ANY(%s)"
            parameters.append(list(topics))
        parameters.append(limit)
        claimed: list[OutboxMessage] = []
        order_and_lock = (
            " ORDER BY available_at, created_at, message_id FOR UPDATE SKIP LOCKED LIMIT %s"
        )
        with self.connect() as conn, conn.transaction():
            rows = conn.execute(
                """
                    SELECT * FROM control_outbox
                    WHERE ((state = 'pending' AND available_at <= %s)
                           OR (state = 'running' AND lease_expires_at <= %s))
                    """
                + topic_clause
                + order_and_lock,
                parameters,
            ).fetchall()
            for row in rows:
                token = int(row["fencing_token"]) + 1
                current = conn.execute(
                    """
                        UPDATE control_outbox
                        SET state = 'running', lease_owner = %s, lease_expires_at = %s,
                            fencing_token = %s, attempts = attempts + 1, updated_at = %s
                        WHERE message_id = %s
                        RETURNING *
                        """,
                    (owner, expires_at, token, now, row["message_id"]),
                ).fetchone()
                if current is not None:
                    claimed.append(_row_to_outbox(current))
        return claimed

    def acknowledge(self, *, message_id: str, owner: str, fencing_token: int) -> None:
        self._finish(
            message_id=message_id,
            owner=owner,
            fencing_token=fencing_token,
            state="succeeded",
            available_at=None,
            error=None,
        )

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
        self._finish(
            message_id=message_id,
            owner=owner,
            fencing_token=fencing_token,
            state=target,
            available_at=self._now() + timedelta(seconds=delay_seconds),
            error=error[:2000],
        )
        return self.get_outbox(message_id)

    def get_outbox(self, message_id: str) -> OutboxMessage:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM control_outbox WHERE message_id = %s", (message_id,)
            ).fetchone()
        if row is None:
            raise KeyError(message_id)
        return _row_to_outbox(row)

    def _finish(
        self,
        *,
        message_id: str,
        owner: str,
        fencing_token: int,
        state: str,
        available_at: datetime | None,
        error: str | None,
    ) -> None:
        if fencing_token <= 0:
            raise ValueError("fencing_token must be positive")
        now = self._now()
        with self.connect() as conn:
            result = conn.execute(
                """
                UPDATE control_outbox
                SET state = %s, available_at = COALESCE(%s, available_at),
                    lease_owner = NULL, lease_expires_at = NULL,
                    last_error = %s, updated_at = %s
                WHERE message_id = %s AND state = 'running'
                  AND lease_owner = %s AND fencing_token = %s
                """,
                (state, available_at, error, now, message_id, owner, fencing_token),
            )
        if result.rowcount != 1:
            raise ControlRepositoryConflict("outbox ownership is stale or fenced")

    def _now(self) -> datetime:
        current = self._clock()
        if current.tzinfo is None:
            raise ValueError("repository clock must return a timezone-aware datetime")
        return current.astimezone(UTC)


def _load_psycopg() -> Any:
    try:
        return importlib.import_module("psycopg")
    except ModuleNotFoundError as exc:
        raise PostgresDriverUnavailable(
            "install pilot107[postgres] to use PostgreSQL repositories"
        ) from exc


def _row_to_outbox(row: Mapping[str, Any]) -> OutboxMessage:
    raw_payload = row["payload_json"]
    payload = json.loads(raw_payload) if isinstance(raw_payload, str) else raw_payload
    if not isinstance(payload, dict):
        raise RuntimeError("outbox payload is not an object")
    return OutboxMessage(
        message_id=str(row["message_id"]),
        topic=str(row["topic"]),
        aggregate_id=str(row["aggregate_id"]),
        payload=payload,
        state=str(row["state"]),
        available_at=_as_utc(row["available_at"]).isoformat(),
        lease_owner=str(row["lease_owner"]) if row["lease_owner"] is not None else None,
        lease_expires_at=(
            _as_utc(row["lease_expires_at"]).isoformat()
            if row["lease_expires_at"] is not None
            else None
        ),
        fencing_token=int(row["fencing_token"]),
        attempts=int(row["attempts"]),
        last_error=str(row["last_error"]) if row["last_error"] is not None else None,
        created_at=_as_utc(row["created_at"]).isoformat(),
        updated_at=_as_utc(row["updated_at"]).isoformat(),
    )


def _as_utc(value: Any) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise RuntimeError("PostgreSQL timestamp is not timezone-aware")
    return value.astimezone(UTC)


def _text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return str(value)


def _parse_timestamp(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be ISO 8601") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{label} must include a timezone")
    return parsed.astimezone(UTC)


def _validate_key(value: str, label: str, pattern: re.Pattern[str]) -> None:
    if not pattern.fullmatch(value):
        raise ValueError(f"{label} is invalid")


def _require_positive(value: int, label: str) -> None:
    if value <= 0:
        raise ValueError(f"{label} must be positive")


def _migration_checksum(statements: tuple[str, ...]) -> str:
    digest = hashlib.sha256()
    for statement in statements:
        digest.update(statement.strip().encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()
