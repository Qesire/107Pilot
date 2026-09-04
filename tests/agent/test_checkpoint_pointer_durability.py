from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from pilot107.agent.migrations import AGENT_SESSION_MIGRATIONS
from pilot107.agent.store import SQLiteAgentSessionStore
from pilot107.core.schema_migrations import apply_schema_migrations


class FixedClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value


def _running_turn(store: SQLiteAgentSessionStore):
    session, _ = store.create_session(
        owner="alice",
        request_key="checkpoint-pointer-session",
        profile_id="hpc-readonly-v1",
        model_profile_id="faux-default",
        source={"run_id": "run-1"},
    )
    turn, _ = store.create_turn(
        session_id=session.session_id,
        owner="alice",
        request_key="checkpoint-pointer-turn",
        message="inspect run",
        expected_state_version=session.state_version,
    )
    claim = store.claim_turn(turn.turn_id, worker_id="worker-1", lease_seconds=30)
    assert claim is not None
    return turn, claim


def test_checkpoint_event_updates_latest_pointer_in_same_store_commit(tmp_path: Path) -> None:
    store = SQLiteAgentSessionStore(tmp_path / "agent.db", clock=FixedClock())
    turn, claim = _running_turn(store)
    checkpoint = {
        "schema_version": "pilot107.agent-checkpoint/v1",
        "turn_id": turn.turn_id,
        "digest": "a" * 64,
    }

    store.append_event(
        turn.turn_id,
        claim=claim,
        sequence=1,
        event_type="checkpoint",
        payload={"checkpoint": checkpoint},
    )

    current = store.get_turn(turn.turn_id, owner="alice")
    assert current.event_sequence == 1
    assert current.final_checkpoint == checkpoint


def test_malformed_checkpoint_event_rolls_back_sequence_and_pointer(tmp_path: Path) -> None:
    store = SQLiteAgentSessionStore(tmp_path / "agent.db", clock=FixedClock())
    turn, claim = _running_turn(store)

    with pytest.raises(sqlite3.IntegrityError, match="checkpoint event must contain"):
        store.append_event(
            turn.turn_id,
            claim=claim,
            sequence=1,
            event_type="checkpoint",
            payload={},
        )

    current = store.get_turn(turn.turn_id, owner="alice")
    assert current.event_sequence == 0
    assert current.final_checkpoint is None
    events, cursor = store.list_events_page(
        session_id=turn.session_id,
        owner="alice",
        after_event_id=0,
        limit=10,
    )
    assert events == []
    assert cursor is None


def test_checkpoint_event_identity_mismatch_is_rejected(tmp_path: Path) -> None:
    store = SQLiteAgentSessionStore(tmp_path / "agent.db", clock=FixedClock())
    turn, _claim = _running_turn(store)
    checkpoint = {
        "schema_version": "pilot107.agent-checkpoint/v1",
        "turn_id": turn.turn_id,
        "digest": "d" * 64,
    }

    with pytest.raises(sqlite3.IntegrityError, match="identity does not match Turn"):
        with store.connect() as connection:
            connection.execute(
                """
                INSERT INTO agent_turn_events (
                    turn_id, session_id, owner, sequence,
                    event_type, payload_json, created_at
                ) VALUES (?, ?, ?, 1, 'checkpoint', ?, ?)
                """,
                (
                    turn.turn_id,
                    turn.session_id,
                    "mallory",
                    json.dumps({"checkpoint": checkpoint}, separators=(",", ":")),
                    "2026-09-04T12:00:00+00:00",
                ),
            )

    current = store.get_turn(turn.turn_id, owner="alice")
    assert current.final_checkpoint is None
    with store.connect() as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM agent_turn_events WHERE turn_id = ?",
            (turn.turn_id,),
        ).fetchone()
    assert count is not None
    assert int(count[0]) == 0


def test_checkpoint_pointer_migration_backfills_recoverable_turn(tmp_path: Path) -> None:
    database = tmp_path / "legacy.db"
    applied_at = "2026-09-04T12:00:00+00:00"
    checkpoint = {
        "schema_version": "pilot107.agent-checkpoint/v1",
        "turn_id": "turn-legacy",
        "digest": "b" * 64,
    }
    with sqlite3.connect(database) as connection:
        apply_schema_migrations(connection, (AGENT_SESSION_MIGRATIONS[0],))
        connection.execute(
            """
            INSERT INTO agent_sessions (
                session_id, owner, request_key, profile_id, model_profile_id,
                source_json, state, state_version, context_checkpoint_json,
                resource_usage_json, outcome_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'queued', 2, NULL, '{}', NULL, ?, ?)
            """,
            (
                "session-legacy",
                "alice",
                "legacy-session",
                "hpc-readonly-v1",
                "faux-default",
                "{}",
                applied_at,
                applied_at,
            ),
        )
        connection.execute(
            """
            INSERT INTO agent_turns (
                turn_id, session_id, owner, request_key, input_digest, message,
                state_version, state, cancel_requested, lease_owner,
                lease_expires_at, fencing_token, event_sequence,
                final_checkpoint_json, error_json, created_at, started_at, finished_at
            ) VALUES (?, ?, ?, ?, ?, ?, 2, 'interrupted', 0, NULL, NULL, 1, 1,
                      NULL, NULL, ?, ?, ?)
            """,
            (
                "turn-legacy",
                "session-legacy",
                "alice",
                "legacy-turn",
                "sha256:" + "c" * 64,
                "inspect run",
                applied_at,
                applied_at,
                applied_at,
            ),
        )
        connection.execute(
            """
            INSERT INTO agent_turn_events (
                turn_id, session_id, owner, sequence, event_type, payload_json, created_at
            ) VALUES (?, ?, ?, 1, 'checkpoint', ?, ?)
            """,
            (
                "turn-legacy",
                "session-legacy",
                "alice",
                json.dumps({"checkpoint": checkpoint}, separators=(",", ":")),
                applied_at,
            ),
        )
        connection.commit()

    store = SQLiteAgentSessionStore(database, clock=FixedClock())

    current = store.get_turn("turn-legacy", owner="alice")
    assert current.final_checkpoint == checkpoint
    with store.connect() as connection:
        migrations = connection.execute(
            """
            SELECT migration_id FROM schema_migrations
            WHERE migration_id IN (?, ?)
            ORDER BY migration_id
            """,
            (
                "006a.002.agent_checkpoint_pointer",
                "006a.003.agent_checkpoint_pointer_identity",
            ),
        ).fetchall()
    assert [str(row[0]) for row in migrations] == [
        "006a.002.agent_checkpoint_pointer",
        "006a.003.agent_checkpoint_pointer_identity",
    ]


def test_identity_migration_clears_legacy_mismatched_checkpoint_pointer(
    tmp_path: Path,
) -> None:
    database = tmp_path / "legacy-mismatch.db"
    applied_at = "2026-09-04T12:00:00+00:00"
    checkpoint = {
        "schema_version": "pilot107.agent-checkpoint/v1",
        "turn_id": "turn-legacy-mismatch",
        "digest": "e" * 64,
    }
    with sqlite3.connect(database) as connection:
        apply_schema_migrations(connection, (AGENT_SESSION_MIGRATIONS[0],))
        connection.execute(
            """
            INSERT INTO agent_sessions (
                session_id, owner, request_key, profile_id, model_profile_id,
                source_json, state, state_version, context_checkpoint_json,
                resource_usage_json, outcome_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'queued', 2, NULL, '{}', NULL, ?, ?)
            """,
            (
                "session-legacy-mismatch",
                "alice",
                "legacy-mismatch-session",
                "hpc-readonly-v1",
                "faux-default",
                "{}",
                applied_at,
                applied_at,
            ),
        )
        connection.execute(
            """
            INSERT INTO agent_turns (
                turn_id, session_id, owner, request_key, input_digest, message,
                state_version, state, cancel_requested, lease_owner,
                lease_expires_at, fencing_token, event_sequence,
                final_checkpoint_json, error_json, created_at, started_at, finished_at
            ) VALUES (?, ?, ?, ?, ?, ?, 2, 'interrupted', 0, NULL, NULL, 1, 1,
                      NULL, NULL, ?, ?, ?)
            """,
            (
                "turn-legacy-mismatch",
                "session-legacy-mismatch",
                "alice",
                "legacy-mismatch-turn",
                "sha256:" + "f" * 64,
                "inspect run",
                applied_at,
                applied_at,
                applied_at,
            ),
        )
        connection.execute(
            """
            INSERT INTO agent_turn_events (
                turn_id, session_id, owner, sequence, event_type, payload_json, created_at
            ) VALUES (?, ?, ?, 1, 'checkpoint', ?, ?)
            """,
            (
                "turn-legacy-mismatch",
                "session-legacy-mismatch",
                "mallory",
                json.dumps({"checkpoint": checkpoint}, separators=(",", ":")),
                applied_at,
            ),
        )
        connection.commit()
        apply_schema_migrations(connection, (AGENT_SESSION_MIGRATIONS[1],))
        polluted = connection.execute(
            "SELECT final_checkpoint_json FROM agent_turns WHERE turn_id = ?",
            ("turn-legacy-mismatch",),
        ).fetchone()
        assert polluted is not None
        assert json.loads(str(polluted[0])) == checkpoint

        apply_schema_migrations(connection, (AGENT_SESSION_MIGRATIONS[2],))
        corrected = connection.execute(
            "SELECT final_checkpoint_json FROM agent_turns WHERE turn_id = ?",
            ("turn-legacy-mismatch",),
        ).fetchone()

    assert corrected is not None
    assert corrected[0] is None
