from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from pilot107.agent.checkpoint_repair import SQLiteToolReceiptCheckpointRebuilder


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _invocation_id(turn_id: str, tool_call_id: str) -> str:
    digest = hashlib.sha256(f"{turn_id}\0{tool_call_id}".encode("utf-8")).hexdigest()
    return f"inv-{digest}"


def _create_database(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE agent_turn_events (
                turn_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                owner TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL
            );
            CREATE TABLE agent_tool_invocations (
                invocation_id TEXT NOT NULL,
                turn_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                owner TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                arguments_digest TEXT NOT NULL,
                state TEXT NOT NULL,
                result_json TEXT,
                error_json TEXT,
                bytes_returned INTEGER NOT NULL
            );
            """
        )


def _insert_event(
    path: Path,
    *,
    sequence: int,
    event_type: str,
    payload: dict[str, object],
    turn_id: str = "turn-1",
    session_id: str = "session-1",
    owner: str = "alice",
) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            INSERT INTO agent_turn_events (
                turn_id, session_id, owner, sequence, event_type, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                turn_id,
                session_id,
                owner,
                sequence,
                event_type,
                json.dumps(payload, separators=(",", ":")),
            ),
        )


def _insert_completed_invocation(
    path: Path,
    *,
    tool_call_id: str,
    tool_name: str = "platform_get_snapshot",
    arguments: dict[str, object] | None = None,
    arguments_digest: str | None = None,
    turn_id: str = "turn-1",
    session_id: str = "session-1",
    owner: str = "alice",
) -> None:
    actual_arguments = arguments or {}
    digest = arguments_digest or hashlib.sha256(_canonical(actual_arguments)).hexdigest()
    result = {
        "result": {"ok": True},
        "evidence_refs": ["evidence://snapshot"],
    }
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            INSERT INTO agent_tool_invocations (
                invocation_id, turn_id, session_id, owner, tool_name,
                arguments_digest, state, result_json, error_json, bytes_returned
            ) VALUES (?, ?, ?, ?, ?, ?, 'completed', ?, NULL, 12)
            """,
            (
                _invocation_id(turn_id, tool_call_id),
                turn_id,
                session_id,
                owner,
                tool_name,
                digest,
                json.dumps(result, separators=(",", ":")),
            ),
        )


def _build(path: Path, checkpoint: dict[str, object] | None = None):
    return SQLiteToolReceiptCheckpointRebuilder(path).build(
        turn_id="turn-1",
        session_id="session-1",
        owner="alice",
        checkpoint=checkpoint,
        session_source={"run_id": "run-1"},
    )


def test_rebuilder_recovers_completed_receipt_after_requested_event(tmp_path: Path) -> None:
    database = tmp_path / "repair.db"
    _create_database(database)
    _insert_event(
        database,
        sequence=1,
        event_type="message_delta",
        payload={"delta": "checking durable state"},
    )
    _insert_event(
        database,
        sequence=2,
        event_type="tool_call_requested",
        payload={
            "tool_call_id": "call-1",
            "tool_name": "platform_get_snapshot",
            "arguments": {},
        },
    )
    _insert_completed_invocation(database, tool_call_id="call-1")

    repairs = _build(database)

    assert len(repairs) == 1
    repair = repairs[0]
    assert repair.parent_checkpoint_digest is None
    assert repair.invocation_id == _invocation_id("turn-1", "call-1")
    assert repair.tool_call_id == "call-1"
    assert repair.tool_name == "platform_get_snapshot"
    assert repair.arguments == {}
    assert repair.assistant_text == "checking durable state"
    assert json.loads(repair.content) == {"ok": True}
    assert repair.details == {
        "result": {"ok": True},
        "evidence_refs": ["evidence://snapshot"],
        "bytes_returned": 12,
    }
    assert repair.receipt_ref.startswith(
        f"agent-tool-receipt:{repair.invocation_id}:sha256:"
    )
    assert repair.is_error is False


def test_rebuilder_fails_closed_on_arguments_digest_mismatch(tmp_path: Path) -> None:
    database = tmp_path / "repair.db"
    _create_database(database)
    _insert_event(
        database,
        sequence=1,
        event_type="tool_call_requested",
        payload={
            "tool_call_id": "call-1",
            "tool_name": "platform_get_snapshot",
            "arguments": {},
        },
    )
    _insert_completed_invocation(
        database,
        tool_call_id="call-1",
        arguments_digest="f" * 64,
    )

    assert _build(database) == ()


def test_rebuilder_does_not_skip_an_unresolved_causal_gap(tmp_path: Path) -> None:
    database = tmp_path / "repair.db"
    _create_database(database)
    _insert_event(
        database,
        sequence=1,
        event_type="tool_call_requested",
        payload={
            "tool_call_id": "call-pending",
            "tool_name": "platform_get_snapshot",
            "arguments": {},
        },
    )
    _insert_event(
        database,
        sequence=2,
        event_type="tool_call_requested",
        payload={
            "tool_call_id": "call-completed",
            "tool_name": "platform_get_snapshot",
            "arguments": {},
        },
    )
    _insert_completed_invocation(database, tool_call_id="call-completed")

    assert _build(database) == ()


def test_rebuilder_does_not_reinject_receipt_after_repaired_checkpoint_commits(
    tmp_path: Path,
) -> None:
    database = tmp_path / "repair.db"
    _create_database(database)
    _insert_event(
        database,
        sequence=1,
        event_type="tool_call_requested",
        payload={
            "tool_call_id": "call-1",
            "tool_name": "platform_get_snapshot",
            "arguments": {},
        },
    )
    _insert_completed_invocation(database, tool_call_id="call-1")
    checkpoint = {
        "schema_version": "pilot107.agent-checkpoint/v1",
        "turn_id": "turn-1",
        "lineage": [],
        "model_profile_id": "campus-default",
        "prompt_profile_id": "hpc-readonly-v1",
        "messages": [],
        "completed_tools": [
            {
                "tool_call_id": "call-1",
                "tool_name": "platform_get_snapshot",
                "arguments": {},
                "result": {"ok": True},
                "is_error": False,
            }
        ],
        "usage": {
            "input_tokens": None,
            "output_tokens": None,
            "cache_read_tokens": None,
            "cache_write_tokens": None,
        },
        "digest": "c" * 64,
    }
    _insert_event(
        database,
        sequence=2,
        event_type="checkpoint",
        payload={"checkpoint": checkpoint},
    )

    assert _build(database, checkpoint) == ()


def test_rebuilder_fails_closed_when_current_checkpoint_has_no_durable_event(
    tmp_path: Path,
) -> None:
    database = tmp_path / "repair.db"
    _create_database(database)
    _insert_event(
        database,
        sequence=1,
        event_type="tool_call_requested",
        payload={
            "tool_call_id": "call-1",
            "tool_name": "platform_get_snapshot",
            "arguments": {},
        },
    )
    _insert_completed_invocation(database, tool_call_id="call-1")
    checkpoint = {
        "turn_id": "turn-1",
        "completed_tools": [],
        "digest": "d" * 64,
    }

    assert _build(database, checkpoint) == ()
