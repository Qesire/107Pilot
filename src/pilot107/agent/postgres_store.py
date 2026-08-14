"""Native PostgreSQL implementation of the durable Agent Session Store."""

from __future__ import annotations

import hashlib
import importlib
import json
import re
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from pilot107.agent.session import (
    AgentSessionConflict,
    AgentSessionInvariantError,
    AgentSessionRecord,
    AgentSessionState,
    AgentToolInvocationRecord,
    AgentTurnEventRecord,
    AgentTurnLease,
    AgentTurnRecord,
    AgentTurnState,
    AgentTurnToolUsage,
)
from pilot107.core.postgres_control_repository import PostgresDriverUnavailable
from pilot107.core.postgres_domain_schema import initialize_postgres_domain_schema

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,255}$")
_NAME = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_TERMINAL = {"completed", "cancelled", "failed"}


class PostgresAgentSessionStore:
    """Agent Store using PostgreSQL row locks, JSONB, and native RETURNING."""

    def __init__(
        self,
        dsn: str,
        *,
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        if not dsn or any(character in dsn for character in "\r\n\0"):
            raise ValueError("PostgreSQL DSN is invalid")
        self.dsn = dsn
        self._clock = clock or (lambda: datetime.now(UTC))
        self._id_factory = id_factory or (lambda: str(uuid.uuid4()))
        try:
            self._psycopg = importlib.import_module("psycopg")
            self._dict_row = importlib.import_module("psycopg.rows").dict_row
            self._jsonb = importlib.import_module("psycopg.types.json").Jsonb
        except ModuleNotFoundError as exc:
            raise PostgresDriverUnavailable(
                "install pilot107[postgres] to use PostgreSQL Agent repositories"
            ) from exc
        initialize_postgres_domain_schema(dsn)

    def connect(self) -> Any:
        return self._psycopg.connect(self.dsn, row_factory=self._dict_row)

    def create_session(
        self,
        *,
        owner: str,
        request_key: str,
        profile_id: str,
        model_profile_id: str,
        source: Mapping[str, object],
    ) -> tuple[AgentSessionRecord, bool]:
        for value, label in (
            (owner, "owner"),
            (request_key, "request_key"),
            (profile_id, "profile_id"),
            (model_profile_id, "model_profile_id"),
        ):
            _key(value, label)
        source_value = _json_object(source, "source")
        session_id = f"session-{self._id_factory()}"
        now = self._now()
        with self.connect() as conn:
            row = conn.execute(
                """
                INSERT INTO agent_sessions (
                    session_id, owner, request_key, profile_id, model_profile_id,
                    source_json, state, state_version, context_checkpoint_json,
                    resource_usage_json, outcome_json, created_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, 'idle', 1, NULL, %s, NULL, %s, %s)
                ON CONFLICT (owner, request_key) DO NOTHING
                RETURNING *
                """,
                (
                    session_id,
                    owner,
                    request_key,
                    profile_id,
                    model_profile_id,
                    self._jsonb(source_value),
                    self._jsonb({}),
                    now,
                    now,
                ),
            ).fetchone()
            created = row is not None
            if row is None:
                row = conn.execute(
                    "SELECT * FROM agent_sessions WHERE owner = %s AND request_key = %s",
                    (owner, request_key),
                ).fetchone()
        if row is None:
            raise RuntimeError("session insert did not produce a row")
        record = _row_to_session(row)
        if (
            record.profile_id != profile_id
            or record.model_profile_id != model_profile_id
            or record.source != source_value
        ):
            raise AgentSessionConflict("request_key refers to different Session content")
        return record, created

    def get_session(self, session_id: str, *, owner: str) -> AgentSessionRecord:
        _key(session_id, "session_id")
        _key(owner, "owner")
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM agent_sessions WHERE session_id = %s AND owner = %s",
                (session_id, owner),
            ).fetchone()
        if row is None:
            raise KeyError(session_id)
        return _row_to_session(row)

    def list_sessions_page(
        self,
        *,
        owner: str,
        states: frozenset[AgentSessionState] | None,
        before: str | None,
        limit: int,
    ) -> tuple[list[AgentSessionRecord], str | None]:
        _key(owner, "owner")
        _limit(limit)
        clauses = ["owner = %s"]
        values: list[object] = [owner]
        if states:
            clauses.append("state = ANY(%s)")
            values.append([state.value for state in sorted(states, key=lambda item: item.value)])
        if before is not None:
            updated_at, session_id = _decode_cursor(before)
            clauses.append("(updated_at < %s OR (updated_at = %s AND session_id < %s))")
            values.extend((updated_at, updated_at, session_id))
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM agent_sessions WHERE "
                + " AND ".join(clauses)
                + " ORDER BY updated_at DESC, session_id DESC LIMIT %s",
                (*values, limit + 1),
            ).fetchall()
        records = [_row_to_session(row) for row in rows[:limit]]
        cursor = None
        if len(rows) > limit and records:
            cursor = _encode_cursor(records[-1].updated_at, records[-1].session_id)
        return records, cursor

    def create_turn(
        self,
        *,
        session_id: str,
        owner: str,
        request_key: str,
        message: str,
        expected_state_version: int,
    ) -> tuple[AgentTurnRecord, bool]:
        for value, label in (
            (session_id, "session_id"),
            (owner, "owner"),
            (request_key, "request_key"),
        ):
            _key(value, label)
        _message(message)
        _positive(expected_state_version, "expected_state_version")
        digest = "sha256:" + hashlib.sha256(message.encode()).hexdigest()
        turn_id = f"turn-{self._id_factory()}"
        now = self._now()
        with self.connect() as conn:
            session = conn.execute(
                """
                SELECT * FROM agent_sessions
                WHERE session_id = %s AND owner = %s FOR UPDATE
                """,
                (session_id, owner),
            ).fetchone()
            if session is None:
                raise KeyError(session_id)
            existing = conn.execute(
                """
                SELECT * FROM agent_turns
                WHERE session_id = %s AND request_key = %s FOR UPDATE
                """,
                (session_id, request_key),
            ).fetchone()
            if existing is not None:
                record = _row_to_turn(existing)
                if (
                    record.owner != owner
                    or record.input_digest != digest
                    or record.message != message
                ):
                    raise AgentSessionConflict(
                        "request_key refers to different Turn content"
                    )
                return record, False
            updated_session = conn.execute(
                """
                UPDATE agent_sessions
                SET state = 'queued', state_version = state_version + 1, updated_at = %s
                WHERE session_id = %s AND owner = %s AND state = 'idle'
                  AND state_version = %s
                RETURNING state_version
                """,
                (now, session_id, owner, expected_state_version),
            ).fetchone()
            if updated_session is None:
                raise AgentSessionConflict("Session state version is stale or not idle")
            row = conn.execute(
                """
                INSERT INTO agent_turns (
                    turn_id, session_id, owner, request_key, input_digest, message,
                    state_version, state, cancel_requested, lease_owner,
                    lease_expires_at, fencing_token, event_sequence,
                    final_checkpoint_json, error_json, created_at, started_at, finished_at
                ) VALUES (%s, %s, %s, %s, %s, %s, 1, 'queued', 0, NULL, NULL,
                          0, 0, NULL, NULL, %s, NULL, NULL)
                RETURNING *
                """,
                (turn_id, session_id, owner, request_key, digest, message, now),
            ).fetchone()
        if row is None:
            raise RuntimeError("turn insert did not produce a row")
        return _row_to_turn(row), True

    def get_turn(self, turn_id: str, *, owner: str) -> AgentTurnRecord:
        _key(turn_id, "turn_id")
        _key(owner, "owner")
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM agent_turns WHERE turn_id = %s AND owner = %s",
                (turn_id, owner),
            ).fetchone()
        if row is None:
            raise KeyError(turn_id)
        return _row_to_turn(row)

    def claim_turn(
        self, turn_id: str, *, worker_id: str, lease_seconds: int
    ) -> AgentTurnLease | None:
        _key(turn_id, "turn_id")
        _key(worker_id, "worker_id")
        _positive(lease_seconds, "lease_seconds")
        now = self._now()
        expires_at = self._after(lease_seconds)
        try:
            with self.connect() as conn:
                current = conn.execute(
                    """
                    SELECT * FROM agent_turns
                    WHERE turn_id = %s AND (
                        state IN ('queued', 'interrupted')
                        OR (state = 'running' AND lease_expires_at <= %s)
                    ) FOR UPDATE
                    """,
                    (turn_id, now),
                ).fetchone()
                if current is None:
                    return None
                row = conn.execute(
                    """
                    UPDATE agent_turns
                    SET state = 'running', state_version = state_version + 1,
                        lease_owner = %s, lease_expires_at = %s,
                        fencing_token = fencing_token + 1,
                        started_at = COALESCE(started_at, %s), finished_at = NULL
                    WHERE turn_id = %s AND state_version = %s AND fencing_token = %s
                      AND (state IN ('queued', 'interrupted')
                           OR (state = 'running' AND lease_expires_at <= %s))
                    RETURNING *
                    """,
                    (
                        worker_id,
                        expires_at,
                        now,
                        turn_id,
                        int(current["state_version"]),
                        int(current["fencing_token"]),
                        now,
                    ),
                ).fetchone()
                if row is None:
                    raise AgentSessionConflict("Turn was claimed concurrently")
                session = conn.execute(
                    """
                    SELECT state_version FROM agent_sessions
                    WHERE session_id = %s AND owner = %s FOR UPDATE
                    """,
                    (str(row["session_id"]), str(row["owner"])),
                ).fetchone()
                if session is None:
                    raise AgentSessionInvariantError("Turn refers to a missing Session")
                session_update = conn.execute(
                    """
                    UPDATE agent_sessions
                    SET state = 'running', state_version = state_version + 1,
                        updated_at = %s
                    WHERE session_id = %s AND owner = %s AND state_version = %s
                      AND state IN ('queued', 'running')
                    RETURNING state_version
                    """,
                    (
                        now,
                        str(row["session_id"]),
                        str(row["owner"]),
                        int(session["state_version"]),
                    ),
                ).fetchone()
                if session_update is None:
                    raise AgentSessionConflict("Session changed while claiming Turn")
        except self._psycopg.IntegrityError:
            return None
        return _row_to_lease(row)

    def renew_turn(
        self, claim: AgentTurnLease, *, lease_seconds: int
    ) -> AgentTurnLease:
        _positive(lease_seconds, "lease_seconds")
        now = self._now()
        expires_at = self._after(lease_seconds)
        with self.connect() as conn:
            row = conn.execute(
                """
                UPDATE agent_turns SET lease_expires_at = %s
                WHERE turn_id = %s AND state = 'running' AND lease_owner = %s
                  AND fencing_token = %s AND lease_expires_at > %s
                RETURNING *
                """,
                (
                    expires_at,
                    claim.turn_id,
                    claim.worker_id,
                    claim.fencing_token,
                    now,
                ),
            ).fetchone()
        if row is None:
            raise AgentSessionConflict("Turn lease is expired or fenced")
        return _row_to_lease(row)

    def append_event(
        self,
        turn_id: str,
        *,
        claim: AgentTurnLease,
        sequence: int,
        event_type: str,
        payload: Mapping[str, object],
    ) -> AgentTurnEventRecord:
        _bound(turn_id, claim)
        _positive(sequence, "sequence")
        _key(event_type, "event_type", _NAME)
        payload_value = _json_object(payload, "payload")
        now = self._now()
        with self.connect() as conn:
            locked = conn.execute(
                "SELECT turn_id FROM agent_turns WHERE turn_id = %s FOR UPDATE",
                (turn_id,),
            ).fetchone()
            if locked is None:
                raise AgentSessionConflict("Turn does not exist")
            existing = conn.execute(
                """
                SELECT * FROM agent_turn_events
                WHERE turn_id = %s AND sequence = %s FOR UPDATE
                """,
                (turn_id, sequence),
            ).fetchone()
            if existing is not None:
                record = _row_to_event(existing)
                if record.event_type != event_type or record.payload != payload_value:
                    raise AgentSessionConflict("event sequence has different content")
                return record
            updated = conn.execute(
                """
                UPDATE agent_turns SET event_sequence = %s
                WHERE turn_id = %s AND session_id = %s AND owner = %s
                  AND state = 'running' AND lease_owner = %s AND lease_expires_at > %s
                  AND fencing_token = %s AND event_sequence = %s
                RETURNING turn_id
                """,
                (
                    sequence,
                    turn_id,
                    claim.session_id,
                    claim.owner,
                    claim.worker_id,
                    now,
                    claim.fencing_token,
                    sequence - 1,
                ),
            ).fetchone()
            if updated is None:
                raise AgentSessionConflict("event is non-contiguous or Turn is fenced")
            row = conn.execute(
                """
                INSERT INTO agent_turn_events (
                    turn_id, session_id, owner, sequence, event_type, payload_json, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING *
                """,
                (
                    turn_id,
                    claim.session_id,
                    claim.owner,
                    sequence,
                    event_type,
                    self._jsonb(payload_value),
                    now,
                ),
            ).fetchone()
        if row is None:
            raise RuntimeError("event insert did not produce a row")
        return _row_to_event(row)

    def request_cancel(
        self, turn_id: str, *, owner: str, expected_state_version: int
    ) -> AgentTurnRecord:
        _key(turn_id, "turn_id")
        _key(owner, "owner")
        _positive(expected_state_version, "expected_state_version")
        with self.connect() as conn:
            current = conn.execute(
                """
                SELECT * FROM agent_turns
                WHERE turn_id = %s AND owner = %s FOR UPDATE
                """,
                (turn_id, owner),
            ).fetchone()
            if current is None:
                raise KeyError(turn_id)
            record = _row_to_turn(current)
            if record.state.value in _TERMINAL:
                return record
            if record.state_version != expected_state_version:
                raise AgentSessionConflict("Turn state version is stale")
            if record.cancel_requested:
                return record
            row = conn.execute(
                """
                UPDATE agent_turns
                SET cancel_requested = 1, state_version = state_version + 1
                WHERE turn_id = %s AND owner = %s AND state_version = %s
                  AND state IN ('queued', 'running', 'interrupted')
                RETURNING *
                """,
                (turn_id, owner, expected_state_version),
            ).fetchone()
        if row is None:
            raise AgentSessionConflict("Turn changed while requesting cancel")
        return _row_to_turn(row)

    def complete_turn(
        self,
        turn_id: str,
        *,
        claim: AgentTurnLease,
        final_checkpoint: Mapping[str, object] | None,
        resource_usage: Mapping[str, object],
        outcome: Mapping[str, object],
    ) -> AgentTurnRecord:
        _bound(turn_id, claim)
        checkpoint = _json_optional(final_checkpoint, "final_checkpoint")
        usage = _json_object(resource_usage, "resource_usage")
        outcome_value = _json_object(outcome, "outcome")
        status = str(outcome.get("status", "completed"))
        state = "cancelled" if status in {"cancelled", "aborted"} else (
            "failed" if status == "failed" else "completed"
        )
        now = self._now()
        with self.connect() as conn:
            row = conn.execute(
                """
                UPDATE agent_turns
                SET state = %s, state_version = state_version + 1,
                    lease_owner = NULL, lease_expires_at = NULL,
                    final_checkpoint_json = %s, error_json = NULL, finished_at = %s
                WHERE turn_id = %s AND session_id = %s AND owner = %s
                  AND state = 'running' AND lease_owner = %s AND fencing_token = %s
                RETURNING *
                """,
                (
                    state,
                    self._adapt_optional(checkpoint),
                    now,
                    turn_id,
                    claim.session_id,
                    claim.owner,
                    claim.worker_id,
                    claim.fencing_token,
                ),
            ).fetchone()
            if row is None:
                raise AgentSessionConflict("terminal Turn write is fenced")
            session = conn.execute(
                """
                SELECT state_version FROM agent_sessions
                WHERE session_id = %s AND owner = %s FOR UPDATE
                """,
                (claim.session_id, claim.owner),
            ).fetchone()
            if session is None:
                raise AgentSessionInvariantError("Turn refers to a missing Session")
            updated = conn.execute(
                """
                UPDATE agent_sessions
                SET state = 'idle', state_version = state_version + 1,
                    context_checkpoint_json = %s, resource_usage_json = %s,
                    outcome_json = %s, updated_at = %s
                WHERE session_id = %s AND owner = %s AND state = 'running'
                  AND state_version = %s
                RETURNING state_version
                """,
                (
                    self._adapt_optional(checkpoint),
                    self._jsonb(usage),
                    self._jsonb(outcome_value),
                    now,
                    claim.session_id,
                    claim.owner,
                    int(session["state_version"]),
                ),
            ).fetchone()
            if updated is None:
                raise AgentSessionConflict("Session changed during terminal Turn write")
        return _row_to_turn(row)

    def interrupt_turn(
        self,
        turn_id: str,
        *,
        claim: AgentTurnLease,
        checkpoint: Mapping[str, object] | None,
        error: Mapping[str, object],
    ) -> AgentTurnRecord:
        _bound(turn_id, claim)
        checkpoint_value = _json_optional(checkpoint, "checkpoint")
        error_value = _json_object(error, "error")
        now = self._now()
        with self.connect() as conn:
            row = conn.execute(
                """
                UPDATE agent_turns
                SET state = 'interrupted', state_version = state_version + 1,
                    lease_owner = NULL, lease_expires_at = NULL,
                    final_checkpoint_json = %s, error_json = %s, finished_at = %s
                WHERE turn_id = %s AND session_id = %s AND owner = %s
                  AND state = 'running' AND lease_owner = %s AND fencing_token = %s
                RETURNING *
                """,
                (
                    self._adapt_optional(checkpoint_value),
                    self._jsonb(error_value),
                    now,
                    turn_id,
                    claim.session_id,
                    claim.owner,
                    claim.worker_id,
                    claim.fencing_token,
                ),
            ).fetchone()
            if row is None:
                raise AgentSessionConflict("interrupted Turn write is fenced")
            session = conn.execute(
                """
                SELECT state_version FROM agent_sessions
                WHERE session_id = %s AND owner = %s FOR UPDATE
                """,
                (claim.session_id, claim.owner),
            ).fetchone()
            if session is None:
                raise AgentSessionInvariantError("Turn refers to a missing Session")
            updated = conn.execute(
                """
                UPDATE agent_sessions
                SET state = 'queued', state_version = state_version + 1,
                    context_checkpoint_json = %s, updated_at = %s
                WHERE session_id = %s AND owner = %s AND state = 'running'
                  AND state_version = %s
                RETURNING state_version
                """,
                (
                    self._adapt_optional(checkpoint_value),
                    now,
                    claim.session_id,
                    claim.owner,
                    int(session["state_version"]),
                ),
            ).fetchone()
            if updated is None:
                raise AgentSessionConflict("Session changed while interrupting Turn")
        return _row_to_turn(row)

    def list_events_page(
        self,
        *,
        session_id: str,
        owner: str,
        after_event_id: int,
        limit: int,
    ) -> tuple[list[AgentTurnEventRecord], int | None]:
        _key(session_id, "session_id")
        _key(owner, "owner")
        if after_event_id < 0:
            raise ValueError("after_event_id must not be negative")
        _limit(limit)
        self.get_session(session_id, owner=owner)
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM agent_turn_events
                WHERE session_id = %s AND owner = %s AND event_id > %s
                ORDER BY event_id LIMIT %s
                """,
                (session_id, owner, after_event_id, limit + 1),
            ).fetchall()
        records = [_row_to_event(row) for row in rows[:limit]]
        cursor = records[-1].event_id if len(rows) > limit and records else None
        return records, cursor

    def list_recoverable_turns(self, *, limit: int) -> list[AgentTurnRecord]:
        _limit(limit, maximum=1000)
        now = self._now()
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM agent_turns
                WHERE state IN ('queued', 'interrupted')
                   OR (state = 'running' AND lease_expires_at <= %s)
                ORDER BY created_at, turn_id LIMIT %s
                """,
                (now, limit),
            ).fetchall()
        return [_row_to_turn(row) for row in rows]

    def reserve_tool_invocation(
        self,
        *,
        invocation_id: str,
        idempotency_key: str,
        owner: str,
        session_id: str,
        turn_id: str,
        expected_state_version: int,
        expected_fencing_token: int,
        tool_name: str,
        arguments_digest: str,
    ) -> tuple[AgentToolInvocationRecord, bool]:
        for value, label in (
            (invocation_id, "invocation_id"),
            (idempotency_key, "idempotency_key"),
            (owner, "owner"),
            (session_id, "session_id"),
            (turn_id, "turn_id"),
            (arguments_digest, "arguments_digest"),
        ):
            _key(value, label)
        _key(tool_name, "tool_name", _NAME)
        _positive(expected_state_version, "expected_state_version")
        _positive(expected_fencing_token, "expected_fencing_token")
        now = self._now()
        with self.connect() as conn:
            row = conn.execute(
                """
                INSERT INTO agent_tool_invocations (
                    invocation_id, idempotency_key, turn_id, session_id, owner,
                    tool_name, arguments_digest, state, result_json, error_json,
                    bytes_returned, created_at, updated_at
                )
                SELECT %s, %s, %s, %s, %s, %s, %s, 'running', NULL, NULL, 0, %s, %s
                WHERE EXISTS (
                    SELECT 1 FROM agent_turns
                    WHERE turn_id = %s AND session_id = %s AND owner = %s
                      AND state = 'running' AND cancel_requested = 0
                      AND state_version = %s AND fencing_token = %s
                      AND lease_expires_at > %s
                )
                ON CONFLICT DO NOTHING
                RETURNING *
                """,
                (
                    invocation_id,
                    idempotency_key,
                    turn_id,
                    session_id,
                    owner,
                    tool_name,
                    arguments_digest,
                    now,
                    now,
                    turn_id,
                    session_id,
                    owner,
                    expected_state_version,
                    expected_fencing_token,
                    now,
                ),
            ).fetchone()
            created = row is not None
            if row is None:
                row = conn.execute(
                    """
                    SELECT * FROM agent_tool_invocations
                    WHERE turn_id = %s AND idempotency_key = %s AND owner = %s
                    """,
                    (turn_id, idempotency_key, owner),
                ).fetchone()
        if row is None:
            raise AgentSessionConflict("Turn capability is stale or fenced")
        record = _row_to_invocation(row)
        if (
            record.invocation_id != invocation_id
            or record.session_id != session_id
            or record.tool_name != tool_name
            or record.arguments_digest != arguments_digest
        ):
            raise AgentSessionConflict(
                "idempotency_key refers to different tool invocation content"
            )
        return record, created

    def finish_tool_invocation(
        self,
        *,
        invocation_id: str,
        owner: str,
        expected_state_version: int,
        expected_fencing_token: int,
        result: Mapping[str, object] | None,
        error: Mapping[str, object] | None,
        bytes_returned: int,
    ) -> AgentToolInvocationRecord:
        _key(invocation_id, "invocation_id")
        _key(owner, "owner")
        _positive(expected_state_version, "expected_state_version")
        _positive(expected_fencing_token, "expected_fencing_token")
        if bytes_returned < 0:
            raise ValueError("bytes_returned must not be negative")
        if (result is None) == (error is None):
            raise ValueError("exactly one of result or error must be provided")
        result_value = _json_optional(result, "result")
        error_value = _json_optional(error, "error")
        state = "completed" if result is not None else "failed"
        now = self._now()
        with self.connect() as conn:
            row = conn.execute(
                """
                UPDATE agent_tool_invocations
                SET state = %s, result_json = %s, error_json = %s,
                    bytes_returned = %s, updated_at = %s
                WHERE invocation_id = %s AND owner = %s AND state = 'running'
                  AND EXISTS (
                      SELECT 1 FROM agent_turns
                      WHERE agent_turns.turn_id = agent_tool_invocations.turn_id
                        AND agent_turns.owner = agent_tool_invocations.owner
                        AND agent_turns.state = 'running'
                        AND agent_turns.cancel_requested = 0
                        AND agent_turns.state_version = %s
                        AND agent_turns.fencing_token = %s
                        AND agent_turns.lease_expires_at > %s
                  )
                RETURNING *
                """,
                (
                    state,
                    self._adapt_optional(result_value),
                    self._adapt_optional(error_value),
                    bytes_returned,
                    now,
                    invocation_id,
                    owner,
                    expected_state_version,
                    expected_fencing_token,
                    now,
                ),
            ).fetchone()
            if row is None:
                existing = conn.execute(
                    """
                    SELECT * FROM agent_tool_invocations
                    WHERE invocation_id = %s AND owner = %s
                    """,
                    (invocation_id, owner),
                ).fetchone()
                if existing is not None:
                    record = _row_to_invocation(existing)
                    if (
                        record.state == state
                        and record.result == result_value
                        and record.error == error_value
                        and record.bytes_returned == bytes_returned
                    ):
                        return record
                raise AgentSessionConflict("tool invocation is complete, stale, or fenced")
        return _row_to_invocation(row)

    def get_turn_tool_usage(
        self,
        *,
        turn_id: str,
        owner: str,
        expected_state_version: int,
        expected_fencing_token: int,
    ) -> AgentTurnToolUsage:
        _key(turn_id, "turn_id")
        _key(owner, "owner")
        _positive(expected_state_version, "expected_state_version")
        _positive(expected_fencing_token, "expected_fencing_token")
        now = self._now()
        with self.connect() as conn:
            valid = conn.execute(
                """
                SELECT 1 FROM agent_turns
                WHERE turn_id = %s AND owner = %s AND state = 'running'
                  AND state_version = %s AND fencing_token = %s
                  AND lease_expires_at > %s
                """,
                (
                    turn_id,
                    owner,
                    expected_state_version,
                    expected_fencing_token,
                    now,
                ),
            ).fetchone()
            if valid is None:
                raise AgentSessionConflict("Turn capability is stale or fenced")
            row = conn.execute(
                """
                SELECT COUNT(*) AS invocations,
                       COALESCE(SUM(bytes_returned), 0) AS bytes_returned
                FROM agent_tool_invocations WHERE turn_id = %s AND owner = %s
                """,
                (turn_id, owner),
            ).fetchone()
        if row is None:
            raise RuntimeError("tool usage query did not return a row")
        return AgentTurnToolUsage(
            invocations=int(row["invocations"]),
            bytes_returned=int(row["bytes_returned"]),
        )

    def _adapt_optional(self, value: dict[str, Any] | None) -> Any:
        return None if value is None else self._jsonb(value)

    def _now(self) -> datetime:
        current = self._clock()
        if current.tzinfo is None:
            raise ValueError("Store clock must return a timezone-aware datetime")
        return current.astimezone(UTC)

    def _after(self, seconds: int) -> datetime:
        return self._now() + timedelta(seconds=seconds)


def _row_to_session(row: Mapping[str, object]) -> AgentSessionRecord:
    return AgentSessionRecord(
        session_id=str(row["session_id"]),
        owner=str(row["owner"]),
        request_key=str(row["request_key"]),
        profile_id=str(row["profile_id"]),
        model_profile_id=str(row["model_profile_id"]),
        source=_object(row["source_json"], "source_json") or {},
        state=AgentSessionState(str(row["state"])),
        state_version=int(str(row["state_version"])),
        context_checkpoint=_object(
            row.get("context_checkpoint_json"), "context_checkpoint_json"
        ),
        resource_usage=_object(row["resource_usage_json"], "resource_usage_json") or {},
        outcome=_object(row.get("outcome_json"), "outcome_json"),
        created_at=_iso(row["created_at"]),
        updated_at=_iso(row["updated_at"]),
    )


def _row_to_turn(row: Mapping[str, object]) -> AgentTurnRecord:
    return AgentTurnRecord(
        turn_id=str(row["turn_id"]),
        session_id=str(row["session_id"]),
        owner=str(row["owner"]),
        request_key=str(row["request_key"]),
        input_digest=str(row["input_digest"]),
        message=str(row["message"]),
        state_version=int(str(row["state_version"])),
        state=AgentTurnState(str(row["state"])),
        cancel_requested=bool(row["cancel_requested"]),
        lease_owner=str(row["lease_owner"]) if row.get("lease_owner") is not None else None,
        lease_expires_at=_iso_optional(row.get("lease_expires_at")),
        fencing_token=int(str(row["fencing_token"])),
        event_sequence=int(str(row["event_sequence"])),
        final_checkpoint=_object(
            row.get("final_checkpoint_json"), "final_checkpoint_json"
        ),
        error=_object(row.get("error_json"), "error_json"),
        created_at=_iso(row["created_at"]),
        started_at=_iso_optional(row.get("started_at")),
        finished_at=_iso_optional(row.get("finished_at")),
    )


def _row_to_lease(row: Mapping[str, object]) -> AgentTurnLease:
    if row.get("lease_owner") is None or row.get("lease_expires_at") is None:
        raise AgentSessionInvariantError("claimed Turn has no lease")
    return AgentTurnLease(
        turn_id=str(row["turn_id"]),
        session_id=str(row["session_id"]),
        owner=str(row["owner"]),
        worker_id=str(row["lease_owner"]),
        state_version=int(str(row["state_version"])),
        fencing_token=int(str(row["fencing_token"])),
        expires_at=_iso(row["lease_expires_at"]),
    )


def _row_to_event(row: Mapping[str, object]) -> AgentTurnEventRecord:
    return AgentTurnEventRecord(
        event_id=int(str(row["event_id"])),
        turn_id=str(row["turn_id"]),
        session_id=str(row["session_id"]),
        owner=str(row["owner"]),
        sequence=int(str(row["sequence"])),
        event_type=str(row["event_type"]),
        payload=_object(row["payload_json"], "payload_json") or {},
        created_at=_iso(row["created_at"]),
    )


def _row_to_invocation(row: Mapping[str, object]) -> AgentToolInvocationRecord:
    return AgentToolInvocationRecord(
        invocation_id=str(row["invocation_id"]),
        idempotency_key=str(row["idempotency_key"]),
        turn_id=str(row["turn_id"]),
        session_id=str(row["session_id"]),
        owner=str(row["owner"]),
        tool_name=str(row["tool_name"]),
        arguments_digest=str(row["arguments_digest"]),
        state=str(row["state"]),
        result=_object(row.get("result_json"), "result_json"),
        error=_object(row.get("error_json"), "error_json"),
        bytes_returned=int(str(row["bytes_returned"])),
        created_at=_iso(row["created_at"]),
        updated_at=_iso(row["updated_at"]),
    )


def _json_object(value: Mapping[str, object], label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} must be an object")
    try:
        encoded = json.dumps(dict(value), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be JSON serializable") from exc
    parsed = json.loads(encoded)
    if not isinstance(parsed, dict):
        raise ValueError(f"{label} must be a JSON object")
    return parsed


def _json_optional(
    value: Mapping[str, object] | None, label: str
) -> dict[str, Any] | None:
    return None if value is None else _json_object(value, label)


def _object(value: object, label: str) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise AgentSessionInvariantError(f"{label} is not a JSON object")
    return value


def _iso(value: object) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise AgentSessionInvariantError("PostgreSQL timestamp has no timezone")
        return value.astimezone(UTC).isoformat()
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        raise AgentSessionInvariantError("PostgreSQL timestamp has no timezone")
    return parsed.astimezone(UTC).isoformat()


def _iso_optional(value: object) -> str | None:
    return None if value is None else _iso(value)


def _key(value: str, label: str, pattern: re.Pattern[str] = _IDENTIFIER) -> None:
    if not pattern.fullmatch(value):
        raise ValueError(f"{label} is invalid")


def _message(value: str) -> None:
    if not value.strip() or len(value) > 65_536 or "\0" in value:
        raise ValueError("message is invalid")


def _positive(value: int, label: str) -> None:
    if value <= 0:
        raise ValueError(f"{label} must be positive")


def _limit(value: int, *, maximum: int = 100) -> None:
    if value <= 0 or value > maximum:
        raise ValueError(f"limit must be between 1 and {maximum}")


def _bound(turn_id: str, claim: AgentTurnLease) -> None:
    if claim.turn_id != turn_id:
        raise AgentSessionConflict("claim is bound to a different Turn")


def _encode_cursor(updated_at: str, session_id: str) -> str:
    return f"{updated_at}|{session_id}"


def _decode_cursor(value: str) -> tuple[datetime, str]:
    try:
        updated_at, session_id = value.rsplit("|", 1)
        parsed = datetime.fromisoformat(updated_at)
    except (ValueError, TypeError) as exc:
        raise ValueError("before cursor is invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError("before cursor is invalid")
    _key(session_id, "before session_id")
    return parsed, session_id
