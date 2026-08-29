"""Lease-aware durable store for Agent Sessions, Turns, and tool calls."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from pilot107.agent.migrations import AGENT_SESSION_MIGRATIONS
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
from pilot107.core.schema_migrations import apply_schema_migrations

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,255}$")
_NAME = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_TERMINAL_TURN_STATES = {
    AgentTurnState.COMPLETED,
    AgentTurnState.CANCELLED,
    AgentTurnState.FAILED,
}


class AgentSessionStore(Protocol):
    def create_session(
        self,
        *,
        owner: str,
        request_key: str,
        profile_id: str,
        model_profile_id: str,
        source: Mapping[str, object],
    ) -> tuple[AgentSessionRecord, bool]: ...

    def get_session(self, session_id: str, *, owner: str) -> AgentSessionRecord: ...

    def list_sessions_page(
        self,
        *,
        owner: str,
        states: frozenset[AgentSessionState] | None,
        before: str | None,
        limit: int,
    ) -> tuple[list[AgentSessionRecord], str | None]: ...

    def create_turn(
        self,
        *,
        session_id: str,
        owner: str,
        request_key: str,
        message: str,
        expected_state_version: int,
    ) -> tuple[AgentTurnRecord, bool]: ...

    def get_turn(self, turn_id: str, *, owner: str) -> AgentTurnRecord: ...

    def get_turn_for_dispatch(self, turn_id: str) -> AgentTurnRecord: ...

    def claim_turn(
        self, turn_id: str, *, worker_id: str, lease_seconds: int
    ) -> AgentTurnLease | None: ...

    def renew_turn(
        self, claim: AgentTurnLease, *, lease_seconds: int
    ) -> AgentTurnLease: ...

    def append_event(
        self,
        turn_id: str,
        *,
        claim: AgentTurnLease,
        sequence: int,
        event_type: str,
        payload: Mapping[str, object],
    ) -> AgentTurnEventRecord: ...

    def request_cancel(
        self, turn_id: str, *, owner: str, expected_state_version: int
    ) -> AgentTurnRecord: ...

    def complete_turn(
        self,
        turn_id: str,
        *,
        claim: AgentTurnLease,
        final_checkpoint: Mapping[str, object] | None,
        resource_usage: Mapping[str, object],
        outcome: Mapping[str, object],
    ) -> AgentTurnRecord: ...

    def interrupt_turn(
        self,
        turn_id: str,
        *,
        claim: AgentTurnLease,
        checkpoint: Mapping[str, object] | None,
        error: Mapping[str, object],
    ) -> AgentTurnRecord: ...

    def list_events_page(
        self,
        *,
        session_id: str,
        owner: str,
        after_event_id: int,
        limit: int,
    ) -> tuple[list[AgentTurnEventRecord], int | None]: ...

    def list_recoverable_turns(self, *, limit: int) -> list[AgentTurnRecord]: ...

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
    ) -> tuple[AgentToolInvocationRecord, bool]: ...

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
    ) -> AgentToolInvocationRecord: ...

    def get_turn_tool_usage(
        self,
        *,
        turn_id: str,
        owner: str,
        expected_state_version: int,
        expected_fencing_token: int,
    ) -> AgentTurnToolUsage: ...


class SQLiteAgentSessionStore:
    """SQLite reference implementation with version CAS and lease fencing."""

    def __init__(
        self,
        db_path: Path,
        *,
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._id_factory = id_factory or (lambda: str(uuid.uuid4()))
        with self.connect() as conn:
            apply_schema_migrations(conn, AGENT_SESSION_MIGRATIONS)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 10000")
        return conn

    def create_session(
        self,
        *,
        owner: str,
        request_key: str,
        profile_id: str,
        model_profile_id: str,
        source: Mapping[str, object],
    ) -> tuple[AgentSessionRecord, bool]:
        _key(owner, "owner")
        _key(request_key, "request_key")
        _key(profile_id, "profile_id")
        _key(model_profile_id, "model_profile_id")
        source_json = _json_object(source, "source")
        session_id = f"session-{self._id_factory()}"
        now = self._now()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO agent_sessions (
                    session_id, owner, request_key, profile_id, model_profile_id,
                    source_json, state, state_version, context_checkpoint_json,
                    resource_usage_json, outcome_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'idle', 1, NULL, '{}', NULL, ?, ?)
                """,
                (
                    session_id,
                    owner,
                    request_key,
                    profile_id,
                    model_profile_id,
                    source_json,
                    now,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM agent_sessions WHERE owner = ? AND request_key = ?",
                (owner, request_key),
            ).fetchone()
        if row is None:
            raise RuntimeError("session insert did not produce a row")
        record = _row_to_session(row)
        if (
            record.profile_id != profile_id
            or record.model_profile_id != model_profile_id
            or str(row["source_json"]) != source_json
        ):
            raise AgentSessionConflict("request_key refers to different Session content")
        return record, cursor.rowcount == 1

    def get_session(self, session_id: str, *, owner: str) -> AgentSessionRecord:
        _key(session_id, "session_id")
        _key(owner, "owner")
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM agent_sessions WHERE session_id = ? AND owner = ?",
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
        clauses = ["owner = ?"]
        values: list[object] = [owner]
        if states:
            placeholders = ",".join("?" for _ in states)
            clauses.append(f"state IN ({placeholders})")
            values.extend(state.value for state in sorted(states, key=lambda item: item.value))
        if before is not None:
            updated_at, session_id = _decode_cursor(before)
            clauses.append("(updated_at < ? OR (updated_at = ? AND session_id < ?))")
            values.extend((updated_at, updated_at, session_id))
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM agent_sessions WHERE "
                + " AND ".join(clauses)
                + " ORDER BY updated_at DESC, session_id DESC LIMIT ?",
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
        _key(session_id, "session_id")
        _key(owner, "owner")
        _key(request_key, "request_key")
        _message(message)
        _positive(expected_state_version, "expected_state_version")
        input_digest = "sha256:" + hashlib.sha256(message.encode()).hexdigest()
        turn_id = f"turn-{self._id_factory()}"
        now = self._now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM agent_turns WHERE session_id = ? AND request_key = ?",
                (session_id, request_key),
            ).fetchone()
            if existing is not None:
                record = _row_to_turn(existing)
                if (
                    record.owner != owner
                    or record.input_digest != input_digest
                    or record.message != message
                ):
                    raise AgentSessionConflict(
                        "request_key refers to different Turn content"
                    )
                return record, False
            session_row = conn.execute(
                "SELECT * FROM agent_sessions WHERE session_id = ? AND owner = ?",
                (session_id, owner),
            ).fetchone()
            if session_row is None:
                raise KeyError(session_id)
            if (
                int(session_row["state_version"]) != expected_state_version
                or str(session_row["state"]) != AgentSessionState.IDLE.value
            ):
                raise AgentSessionConflict("Session state version is stale or not idle")
            session_update = conn.execute(
                """
                UPDATE agent_sessions
                SET state = 'queued', state_version = state_version + 1, updated_at = ?
                WHERE session_id = ? AND owner = ? AND state = 'idle'
                  AND state_version = ?
                """,
                (now, session_id, owner, expected_state_version),
            )
            if session_update.rowcount != 1:
                raise AgentSessionConflict("Session state changed while creating Turn")
            conn.execute(
                """
                INSERT INTO agent_turns (
                    turn_id, session_id, owner, request_key, input_digest, message,
                    state_version, state, cancel_requested, lease_owner,
                    lease_expires_at, fencing_token, event_sequence,
                    final_checkpoint_json, error_json, created_at, started_at, finished_at
                ) VALUES (?, ?, ?, ?, ?, ?, 1, 'queued', 0, NULL, NULL, 0, 0,
                          NULL, NULL, ?, NULL, NULL)
                """,
                (turn_id, session_id, owner, request_key, input_digest, message, now),
            )
            row = conn.execute(
                "SELECT * FROM agent_turns WHERE turn_id = ?", (turn_id,)
            ).fetchone()
        if row is None:
            raise RuntimeError("turn insert did not produce a row")
        return _row_to_turn(row), True

    def get_turn(self, turn_id: str, *, owner: str) -> AgentTurnRecord:
        _key(turn_id, "turn_id")
        _key(owner, "owner")
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM agent_turns WHERE turn_id = ? AND owner = ?",
                (turn_id, owner),
            ).fetchone()
        if row is None:
            raise KeyError(turn_id)
        return _row_to_turn(row)

    def get_turn_for_dispatch(self, turn_id: str) -> AgentTurnRecord:
        """Read a Turn from the trusted worker path without weakening owner APIs."""
        _key(turn_id, "turn_id")
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM agent_turns WHERE turn_id = ?", (turn_id,)
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
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT * FROM agent_turns
                WHERE turn_id = ? AND (
                    state IN ('queued', 'interrupted')
                    OR (state = 'running' AND lease_expires_at <= ?)
                )
                """,
                (turn_id, now),
            ).fetchone()
            if row is None:
                return None
            token = int(row["fencing_token"]) + 1
            try:
                updated = conn.execute(
                    """
                    UPDATE agent_turns
                    SET state = 'running', state_version = state_version + 1,
                        lease_owner = ?, lease_expires_at = ?, fencing_token = ?,
                        started_at = COALESCE(started_at, ?), finished_at = NULL
                    WHERE turn_id = ? AND state_version = ? AND fencing_token = ?
                      AND (state IN ('queued', 'interrupted')
                           OR (state = 'running' AND lease_expires_at <= ?))
                    """,
                    (
                        worker_id,
                        expires_at,
                        token,
                        now,
                        turn_id,
                        int(row["state_version"]),
                        int(row["fencing_token"]),
                        now,
                    ),
                )
            except sqlite3.IntegrityError:
                conn.rollback()
                return None
            if updated.rowcount != 1:
                raise AgentSessionConflict("Turn was claimed concurrently")
            session_row = conn.execute(
                "SELECT state_version FROM agent_sessions WHERE session_id = ? AND owner = ?",
                (str(row["session_id"]), str(row["owner"])),
            ).fetchone()
            if session_row is None:
                raise AgentSessionInvariantError("Turn refers to a missing Session")
            session_update = conn.execute(
                """
                UPDATE agent_sessions
                SET state = 'running', state_version = state_version + 1, updated_at = ?
                WHERE session_id = ? AND owner = ? AND state_version = ?
                  AND state IN ('queued', 'running')
                """,
                (
                    now,
                    str(row["session_id"]),
                    str(row["owner"]),
                    int(session_row["state_version"]),
                ),
            )
            if session_update.rowcount != 1:
                raise AgentSessionConflict("Session changed while claiming Turn")
            current = conn.execute(
                "SELECT * FROM agent_turns WHERE turn_id = ?", (turn_id,)
            ).fetchone()
        if current is None:
            raise RuntimeError("claimed Turn disappeared")
        return _row_to_lease(current)

    def renew_turn(
        self, claim: AgentTurnLease, *, lease_seconds: int
    ) -> AgentTurnLease:
        _positive(lease_seconds, "lease_seconds")
        now = self._now()
        expires_at = self._after(lease_seconds)
        with self.connect() as conn:
            updated = conn.execute(
                """
                UPDATE agent_turns SET lease_expires_at = ?
                WHERE turn_id = ? AND state = 'running' AND lease_owner = ?
                  AND fencing_token = ? AND lease_expires_at > ?
                """,
                (
                    expires_at,
                    claim.turn_id,
                    claim.worker_id,
                    claim.fencing_token,
                    now,
                ),
            )
            if updated.rowcount != 1:
                raise AgentSessionConflict("Turn lease is expired or fenced")
            row = conn.execute(
                "SELECT * FROM agent_turns WHERE turn_id = ?", (claim.turn_id,)
            ).fetchone()
        if row is None:
            raise RuntimeError("renewed Turn disappeared")
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
        _positive(sequence, "sequence")
        _key(event_type, "event_type", _NAME)
        payload_json = _json_object(payload, "payload")
        now = self._now()
        if claim.turn_id != turn_id:
            raise AgentSessionConflict("claim is bound to a different Turn")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM agent_turn_events WHERE turn_id = ? AND sequence = ?",
                (turn_id, sequence),
            ).fetchone()
            if existing is not None:
                record = _row_to_event(existing)
                if record.event_type != event_type or record.payload != dict(payload):
                    raise AgentSessionConflict("event sequence has different content")
                return record
            updated = conn.execute(
                """
                UPDATE agent_turns SET event_sequence = ?
                WHERE turn_id = ? AND session_id = ? AND owner = ?
                  AND state = 'running' AND lease_owner = ? AND lease_expires_at > ?
                  AND fencing_token = ? AND event_sequence = ?
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
            )
            if updated.rowcount != 1:
                raise AgentSessionConflict("event is non-contiguous or Turn is fenced")
            cursor = conn.execute(
                """
                INSERT INTO agent_turn_events (
                    turn_id, session_id, owner, sequence, event_type, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    turn_id,
                    claim.session_id,
                    claim.owner,
                    sequence,
                    event_type,
                    payload_json,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM agent_turn_events WHERE event_id = ?",
                (cursor.lastrowid,),
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
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute(
                "SELECT * FROM agent_turns WHERE turn_id = ? AND owner = ?",
                (turn_id, owner),
            ).fetchone()
            if current is None:
                raise KeyError(turn_id)
            record = _row_to_turn(current)
            if record.state in _TERMINAL_TURN_STATES:
                return record
            if record.cancel_requested:
                return record
            if record.state_version != expected_state_version:
                raise AgentSessionConflict("Turn state version is stale")
            updated = conn.execute(
                """
                UPDATE agent_turns
                SET cancel_requested = 1, state_version = state_version + 1
                WHERE turn_id = ? AND owner = ? AND state_version = ?
                  AND state IN ('queued', 'running', 'interrupted')
                """,
                (turn_id, owner, expected_state_version),
            )
            if updated.rowcount != 1:
                raise AgentSessionConflict("Turn changed while requesting cancel")
            row = conn.execute(
                "SELECT * FROM agent_turns WHERE turn_id = ?", (turn_id,)
            ).fetchone()
        if row is None:
            raise RuntimeError("cancelled Turn disappeared")
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
        checkpoint_json = _json_optional(final_checkpoint, "final_checkpoint")
        usage_json = _json_object(resource_usage, "resource_usage")
        outcome_json = _json_object(outcome, "outcome")
        status = str(outcome.get("status", "completed"))
        state = (
            AgentTurnState.CANCELLED
            if status in {"cancelled", "aborted"}
            else AgentTurnState.FAILED
            if status == "failed"
            else AgentTurnState.COMPLETED
        )
        now = self._now()
        _require_bound_claim(turn_id, claim)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            updated = conn.execute(
                """
                UPDATE agent_turns
                SET state = ?, state_version = state_version + 1,
                    lease_owner = NULL, lease_expires_at = NULL,
                    final_checkpoint_json = ?, error_json = NULL, finished_at = ?
                WHERE turn_id = ? AND session_id = ? AND owner = ?
                  AND state = 'running' AND lease_owner = ? AND fencing_token = ?
                """,
                (
                    state.value,
                    checkpoint_json,
                    now,
                    turn_id,
                    claim.session_id,
                    claim.owner,
                    claim.worker_id,
                    claim.fencing_token,
                ),
            )
            if updated.rowcount != 1:
                raise AgentSessionConflict("terminal Turn write is fenced")
            session_row = conn.execute(
                "SELECT state_version FROM agent_sessions WHERE session_id = ? AND owner = ?",
                (claim.session_id, claim.owner),
            ).fetchone()
            if session_row is None:
                raise AgentSessionInvariantError("Turn refers to a missing Session")
            session_update = conn.execute(
                """
                UPDATE agent_sessions
                SET state = 'idle', state_version = state_version + 1,
                    context_checkpoint_json = ?, resource_usage_json = ?,
                    outcome_json = ?, updated_at = ?
                WHERE session_id = ? AND owner = ? AND state = 'running'
                  AND state_version = ?
                """,
                (
                    checkpoint_json,
                    usage_json,
                    outcome_json,
                    now,
                    claim.session_id,
                    claim.owner,
                    int(session_row["state_version"]),
                ),
            )
            if session_update.rowcount != 1:
                raise AgentSessionConflict("Session changed during terminal Turn write")
            row = conn.execute(
                "SELECT * FROM agent_turns WHERE turn_id = ?", (turn_id,)
            ).fetchone()
        if row is None:
            raise RuntimeError("completed Turn disappeared")
        return _row_to_turn(row)

    def interrupt_turn(
        self,
        turn_id: str,
        *,
        claim: AgentTurnLease,
        checkpoint: Mapping[str, object] | None,
        error: Mapping[str, object],
    ) -> AgentTurnRecord:
        checkpoint_json = _json_optional(checkpoint, "checkpoint")
        error_json = _json_object(error, "error")
        now = self._now()
        _require_bound_claim(turn_id, claim)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            updated = conn.execute(
                """
                UPDATE agent_turns
                SET state = 'interrupted', state_version = state_version + 1,
                    lease_owner = NULL, lease_expires_at = NULL,
                    final_checkpoint_json = ?, error_json = ?, finished_at = ?
                WHERE turn_id = ? AND session_id = ? AND owner = ?
                  AND state = 'running' AND lease_owner = ? AND fencing_token = ?
                """,
                (
                    checkpoint_json,
                    error_json,
                    now,
                    turn_id,
                    claim.session_id,
                    claim.owner,
                    claim.worker_id,
                    claim.fencing_token,
                ),
            )
            if updated.rowcount != 1:
                raise AgentSessionConflict("interrupted Turn write is fenced")
            session_row = conn.execute(
                "SELECT state_version FROM agent_sessions WHERE session_id = ? AND owner = ?",
                (claim.session_id, claim.owner),
            ).fetchone()
            if session_row is None:
                raise AgentSessionInvariantError("Turn refers to a missing Session")
            session_update = conn.execute(
                """
                UPDATE agent_sessions
                SET state = 'queued', state_version = state_version + 1,
                    context_checkpoint_json = ?, updated_at = ?
                WHERE session_id = ? AND owner = ? AND state = 'running'
                  AND state_version = ?
                """,
                (
                    checkpoint_json,
                    now,
                    claim.session_id,
                    claim.owner,
                    int(session_row["state_version"]),
                ),
            )
            if session_update.rowcount != 1:
                raise AgentSessionConflict("Session changed while interrupting Turn")
            row = conn.execute(
                "SELECT * FROM agent_turns WHERE turn_id = ?", (turn_id,)
            ).fetchone()
        if row is None:
            raise RuntimeError("interrupted Turn disappeared")
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
                WHERE session_id = ? AND owner = ? AND event_id > ?
                ORDER BY event_id LIMIT ?
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
                   OR (state = 'running' AND lease_expires_at <= ?)
                ORDER BY created_at, turn_id LIMIT ?
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
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO agent_tool_invocations (
                    invocation_id, idempotency_key, turn_id, session_id, owner,
                    tool_name, arguments_digest, state, result_json, error_json,
                    bytes_returned, created_at, updated_at
                )
                SELECT ?, ?, ?, ?, ?, ?, ?, 'running', NULL, NULL, 0, ?, ?
                WHERE EXISTS (
                    SELECT 1 FROM agent_turns
                    WHERE turn_id = ? AND session_id = ? AND owner = ?
                      AND state = 'running' AND cancel_requested = 0
                      AND state_version = ? AND fencing_token = ?
                      AND lease_expires_at > ?
                )
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
            )
            row = conn.execute(
                """
                SELECT * FROM agent_tool_invocations
                WHERE turn_id = ? AND idempotency_key = ? AND owner = ?
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
        return record, cursor.rowcount == 1

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
        result_json = _json_optional(result, "result")
        error_json = _json_optional(error, "error")
        state = "completed" if result is not None else "failed"
        now = self._now()
        with self.connect() as conn:
            updated = conn.execute(
                """
                UPDATE agent_tool_invocations
                SET state = ?, result_json = ?, error_json = ?, bytes_returned = ?,
                    updated_at = ?
                WHERE invocation_id = ? AND owner = ? AND state = 'running'
                  AND EXISTS (
                      SELECT 1 FROM agent_turns
                      WHERE agent_turns.turn_id = agent_tool_invocations.turn_id
                        AND agent_turns.owner = agent_tool_invocations.owner
                        AND agent_turns.state = 'running'
                        AND agent_turns.cancel_requested = 0
                        AND agent_turns.state_version = ?
                        AND agent_turns.fencing_token = ?
                        AND agent_turns.lease_expires_at > ?
                  )
                """,
                (
                    state,
                    result_json,
                    error_json,
                    bytes_returned,
                    now,
                    invocation_id,
                    owner,
                    expected_state_version,
                    expected_fencing_token,
                    now,
                ),
            )
            if updated.rowcount != 1:
                existing = conn.execute(
                    "SELECT * FROM agent_tool_invocations WHERE invocation_id = ? AND owner = ?",
                    (invocation_id, owner),
                ).fetchone()
                if existing is not None:
                    record = _row_to_invocation(existing)
                    if (
                        record.state == state
                        and record.result == (dict(result) if result is not None else None)
                        and record.error == (dict(error) if error is not None else None)
                        and record.bytes_returned == bytes_returned
                    ):
                        return record
                raise AgentSessionConflict("tool invocation is complete, stale, or fenced")
            row = conn.execute(
                "SELECT * FROM agent_tool_invocations WHERE invocation_id = ?",
                (invocation_id,),
            ).fetchone()
        if row is None:
            raise RuntimeError("completed invocation disappeared")
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
            turn = conn.execute(
                """
                SELECT 1 FROM agent_turns
                WHERE turn_id = ? AND owner = ? AND state = 'running'
                  AND state_version = ? AND fencing_token = ?
                  AND lease_expires_at > ?
                """,
                (
                    turn_id,
                    owner,
                    expected_state_version,
                    expected_fencing_token,
                    now,
                ),
            ).fetchone()
            if turn is None:
                raise AgentSessionConflict("Turn capability is stale or fenced")
            row = conn.execute(
                """
                SELECT COUNT(*) AS invocations,
                       COALESCE(SUM(bytes_returned), 0) AS bytes_returned,
                       COALESCE(SUM(CASE WHEN tool_name IN (
                           'sandbox_exec', 'builder_build_submit'
                       ) THEN 1 ELSE 0 END), 0)
                           AS commands
                FROM agent_tool_invocations WHERE turn_id = ? AND owner = ?
                """,
                (turn_id, owner),
            ).fetchone()
        if row is None:
            raise RuntimeError("tool usage query did not return a row")
        return AgentTurnToolUsage(
            invocations=int(row["invocations"]),
            bytes_returned=int(row["bytes_returned"]),
            commands=int(row["commands"]),
        )

    def _now(self) -> str:
        current = self._clock()
        if current.tzinfo is None:
            raise ValueError("Store clock must return a timezone-aware datetime")
        return current.astimezone(UTC).isoformat()

    def _after(self, seconds: int) -> str:
        current = self._clock()
        if current.tzinfo is None:
            raise ValueError("Store clock must return a timezone-aware datetime")
        return (current.astimezone(UTC) + timedelta(seconds=seconds)).isoformat()


def _row_to_session(row: sqlite3.Row) -> AgentSessionRecord:
    return AgentSessionRecord(
        session_id=str(row["session_id"]),
        owner=str(row["owner"]),
        request_key=str(row["request_key"]),
        profile_id=str(row["profile_id"]),
        model_profile_id=str(row["model_profile_id"]),
        source=_load_object(row["source_json"], "source_json") or {},
        state=AgentSessionState(str(row["state"])),
        state_version=int(row["state_version"]),
        context_checkpoint=_load_object(
            row["context_checkpoint_json"], "context_checkpoint_json"
        ),
        resource_usage=_load_object(row["resource_usage_json"], "resource_usage_json")
        or {},
        outcome=_load_object(row["outcome_json"], "outcome_json"),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _row_to_turn(row: sqlite3.Row) -> AgentTurnRecord:
    return AgentTurnRecord(
        turn_id=str(row["turn_id"]),
        session_id=str(row["session_id"]),
        owner=str(row["owner"]),
        request_key=str(row["request_key"]),
        input_digest=str(row["input_digest"]),
        message=str(row["message"]),
        state_version=int(row["state_version"]),
        state=AgentTurnState(str(row["state"])),
        cancel_requested=bool(row["cancel_requested"]),
        lease_owner=str(row["lease_owner"]) if row["lease_owner"] is not None else None,
        lease_expires_at=(
            str(row["lease_expires_at"])
            if row["lease_expires_at"] is not None
            else None
        ),
        fencing_token=int(row["fencing_token"]),
        event_sequence=int(row["event_sequence"]),
        final_checkpoint=_load_object(
            row["final_checkpoint_json"], "final_checkpoint_json"
        ),
        error=_load_object(row["error_json"], "error_json"),
        created_at=str(row["created_at"]),
        started_at=str(row["started_at"]) if row["started_at"] is not None else None,
        finished_at=(
            str(row["finished_at"]) if row["finished_at"] is not None else None
        ),
    )


def _row_to_lease(row: sqlite3.Row) -> AgentTurnLease:
    lease_owner = row["lease_owner"]
    lease_expires_at = row["lease_expires_at"]
    if lease_owner is None or lease_expires_at is None:
        raise AgentSessionInvariantError("claimed Turn has no lease")
    return AgentTurnLease(
        turn_id=str(row["turn_id"]),
        session_id=str(row["session_id"]),
        owner=str(row["owner"]),
        worker_id=str(lease_owner),
        state_version=int(row["state_version"]),
        fencing_token=int(row["fencing_token"]),
        expires_at=str(lease_expires_at),
    )


def _row_to_event(row: sqlite3.Row) -> AgentTurnEventRecord:
    return AgentTurnEventRecord(
        event_id=int(row["event_id"]),
        turn_id=str(row["turn_id"]),
        session_id=str(row["session_id"]),
        owner=str(row["owner"]),
        sequence=int(row["sequence"]),
        event_type=str(row["event_type"]),
        payload=_load_object(row["payload_json"], "payload_json") or {},
        created_at=str(row["created_at"]),
    )


def _row_to_invocation(row: sqlite3.Row) -> AgentToolInvocationRecord:
    return AgentToolInvocationRecord(
        invocation_id=str(row["invocation_id"]),
        idempotency_key=str(row["idempotency_key"]),
        turn_id=str(row["turn_id"]),
        session_id=str(row["session_id"]),
        owner=str(row["owner"]),
        tool_name=str(row["tool_name"]),
        arguments_digest=str(row["arguments_digest"]),
        state=str(row["state"]),
        result=_load_object(row["result_json"], "result_json"),
        error=_load_object(row["error_json"], "error_json"),
        bytes_returned=int(row["bytes_returned"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


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


def _json_object(value: Mapping[str, object], label: str) -> str:
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} must be an object")
    try:
        return json.dumps(
            dict(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be JSON serializable") from exc


def _json_optional(value: Mapping[str, object] | None, label: str) -> str | None:
    return None if value is None else _json_object(value, label)


def _load_object(value: object, label: str) -> dict[str, Any] | None:
    if value is None:
        return None
    parsed = json.loads(str(value))
    if not isinstance(parsed, dict):
        raise AgentSessionInvariantError(f"{label} is not a JSON object")
    return parsed


def _encode_cursor(updated_at: str, session_id: str) -> str:
    return f"{updated_at}|{session_id}"


def _decode_cursor(value: str) -> tuple[str, str]:
    try:
        updated_at, session_id = value.rsplit("|", 1)
        parsed = datetime.fromisoformat(updated_at)
    except (ValueError, TypeError) as exc:
        raise ValueError("before cursor is invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError("before cursor is invalid")
    _key(session_id, "before session_id")
    return updated_at, session_id


def _require_bound_claim(turn_id: str, claim: AgentTurnLease) -> None:
    if claim.turn_id != turn_id:
        raise AgentSessionConflict("claim is bound to a different Turn")


__all__ = [
    "AgentSessionConflict",
    "AgentSessionStore",
    "AgentSessionRecord",
    "AgentSessionState",
    "AgentToolInvocationRecord",
    "AgentTurnEventRecord",
    "AgentTurnLease",
    "AgentTurnRecord",
    "AgentTurnState",
    "AgentTurnToolUsage",
    "SQLiteAgentSessionStore",
]
