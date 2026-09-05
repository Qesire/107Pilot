from __future__ import annotations

import importlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from .test_store_contract import exercise_agent_store_contract


class MutableClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 14, 0, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


def _store_api() -> Any:
    return importlib.import_module("pilot107.agent.store")


def _new_store(path: Path, clock: MutableClock) -> Any:
    return _store_api().SQLiteAgentSessionStore(path, clock=clock)


def _create_session(
    store: Any,
    *,
    owner: str = "alice",
    request_key: str = "session-request-1",
    profile_id: str = "default",
) -> Any:
    session, created = store.create_session(
        owner=owner,
        request_key=request_key,
        profile_id=profile_id,
        model_profile_id="model-default",
        source={"kind": "cli", "request_id": request_key},
    )
    assert created is True
    return session


def _create_turn(
    store: Any,
    session: Any,
    *,
    request_key: str = "turn-request-1",
    message: str = "Inspect the latest run.",
) -> Any:
    turn, created = store.create_turn(
        session_id=session.session_id,
        owner=session.owner,
        request_key=request_key,
        message=message,
        expected_state_version=session.state_version,
    )
    assert created is True
    return turn


def test_create_session_replays_same_request_key_across_reopen(tmp_path: Path) -> None:
    clock = MutableClock()
    database = tmp_path / "agent.db"
    first_store = _new_store(database, clock)
    first = _create_session(first_store)

    second_store = _new_store(database, clock)
    replay, created = second_store.create_session(
        owner="alice",
        request_key="session-request-1",
        profile_id="default",
        model_profile_id="model-default",
        source={"kind": "cli", "request_id": "session-request-1"},
    )

    assert created is False
    assert replay == first
    assert replay.state.value == "idle"
    assert replay.state_version == 1


def test_create_session_rejects_same_key_with_different_content(tmp_path: Path) -> None:
    clock = MutableClock()
    store = _new_store(tmp_path / "agent.db", clock)
    _create_session(store)

    with pytest.raises(_store_api().AgentSessionConflict):
        _create_session(store, profile_id="other-profile")


def test_get_session_and_turn_hide_foreign_owner(tmp_path: Path) -> None:
    clock = MutableClock()
    store = _new_store(tmp_path / "agent.db", clock)
    session = _create_session(store)
    turn = _create_turn(store, session)

    with pytest.raises(KeyError):
        store.get_session(session.session_id, owner="mallory")
    with pytest.raises(KeyError):
        store.get_turn(turn.turn_id, owner="mallory")


def test_create_turn_is_content_bound_and_checks_session_version(tmp_path: Path) -> None:
    clock = MutableClock()
    store = _new_store(tmp_path / "agent.db", clock)
    session = _create_session(store)

    first = _create_turn(store, session)
    replay, created = store.create_turn(
        session_id=session.session_id,
        owner=session.owner,
        request_key="turn-request-1",
        message="Inspect the latest run.",
        expected_state_version=session.state_version,
    )

    assert created is False
    assert replay == first
    assert first.state.value == "queued"

    with pytest.raises(_store_api().AgentSessionConflict):
        _create_turn(store, session, message="A different request.")

    with pytest.raises(_store_api().AgentSessionConflict):
        store.create_turn(
            session_id=session.session_id,
            owner=session.owner,
            request_key="turn-request-2",
            message="A new turn.",
            expected_state_version=session.state_version,
        )


def test_claim_reclaim_and_stale_writer_fencing(tmp_path: Path) -> None:
    clock = MutableClock()
    store = _new_store(tmp_path / "agent.db", clock)
    turn = _create_turn(store, _create_session(store))

    first_claim = store.claim_turn(turn.turn_id, worker_id="worker-1", lease_seconds=30)
    assert first_claim is not None
    assert first_claim.turn_id == turn.turn_id
    assert store.claim_turn(turn.turn_id, worker_id="worker-2", lease_seconds=30) is None

    renewed = store.renew_turn(first_claim, lease_seconds=30)
    assert renewed.fencing_token == first_claim.fencing_token
    clock.advance(31)
    second_claim = store.claim_turn(turn.turn_id, worker_id="worker-2", lease_seconds=30)
    assert second_claim is not None
    assert second_claim.fencing_token == first_claim.fencing_token + 1

    with pytest.raises(_store_api().AgentSessionConflict):
        store.append_event(
            turn.turn_id,
            claim=first_claim,
            sequence=1,
            event_type="turn.started",
            payload={},
        )


def test_events_are_contiguous_and_owner_scoped(tmp_path: Path) -> None:
    clock = MutableClock()
    store = _new_store(tmp_path / "agent.db", clock)
    turn = _create_turn(store, _create_session(store))
    claim = store.claim_turn(turn.turn_id, worker_id="worker-1", lease_seconds=30)
    assert claim is not None

    event_one = store.append_event(
        turn.turn_id,
        claim=claim,
        sequence=1,
        event_type="turn.started",
        payload={"worker_id": "worker-1"},
    )
    with pytest.raises(_store_api().AgentSessionConflict):
        store.append_event(
            turn.turn_id,
            claim=claim,
            sequence=3,
            event_type="assistant.delta",
            payload={"text": "skipped"},
        )
    event_two = store.append_event(
        turn.turn_id,
        claim=claim,
        sequence=2,
        event_type="assistant.delta",
        payload={"text": "ok"},
    )

    first_page, cursor = store.list_events_page(
        session_id=turn.session_id,
        owner="alice",
        after_event_id=0,
        limit=1,
    )
    assert first_page == [event_one]
    assert cursor == event_one.event_id
    second_page, cursor = store.list_events_page(
        session_id=turn.session_id,
        owner="alice",
        after_event_id=event_one.event_id,
        limit=10,
    )
    assert second_page == [event_two]
    assert cursor is None
    with pytest.raises(KeyError):
        store.list_events_page(
            session_id=turn.session_id,
            owner="mallory",
            after_event_id=0,
            limit=10,
        )


def test_cancel_persists_and_terminal_completion_updates_session(tmp_path: Path) -> None:
    clock = MutableClock()
    database = tmp_path / "agent.db"
    store = _new_store(database, clock)
    session = _create_session(store)
    turn = _create_turn(store, session)
    claim = store.claim_turn(turn.turn_id, worker_id="worker-1", lease_seconds=30)
    assert claim is not None

    cancel_requested = store.request_cancel(
        turn_id=claim.turn_id,
        owner="alice",
        expected_state_version=claim.state_version,
    )
    assert cancel_requested.cancel_requested is True

    reopened = _new_store(database, clock)
    persisted = reopened.get_turn(claim.turn_id, owner="alice")
    assert persisted.cancel_requested is True

    final_turn = reopened.complete_turn(
        claim.turn_id,
        claim=claim,
        final_checkpoint={"summary": "Cancelled before the next tool call."},
        resource_usage={"tool_invocations": 0, "bytes_returned": 0},
        outcome={"status": "cancelled"},
    )
    assert final_turn.state.value == "cancelled"
    updated_session = reopened.get_session(session.session_id, owner="alice")
    assert updated_session.state.value == "idle"
    assert updated_session.context_checkpoint == final_turn.final_checkpoint
    assert updated_session.resource_usage == {"tool_invocations": 0, "bytes_returned": 0}
    assert updated_session.outcome == {"status": "cancelled"}


def test_interrupted_turn_is_recoverable(tmp_path: Path) -> None:
    clock = MutableClock()
    store = _new_store(tmp_path / "agent.db", clock)
    turn = _create_turn(store, _create_session(store))
    claim = store.claim_turn(turn.turn_id, worker_id="worker-1", lease_seconds=30)
    assert claim is not None

    interrupted = store.interrupt_turn(
        claim.turn_id,
        claim=claim,
        checkpoint={"summary": "recover me"},
        error={"code": "worker_shutdown", "message": "worker shutdown"},
    )
    assert interrupted.state.value == "interrupted"
    assert store.list_recoverable_turns(limit=10) == [interrupted]


def test_partial_unique_index_allows_only_one_running_turn_per_owner(
    tmp_path: Path,
) -> None:
    clock = MutableClock()
    store = _new_store(tmp_path / "agent.db", clock)
    alice_turn_one = _create_turn(store, _create_session(store, request_key="alice-session-1"))
    alice_turn_two = _create_turn(
        store,
        _create_session(store, request_key="alice-session-2"),
        request_key="alice-turn-2",
    )
    bob_turn = _create_turn(
        store,
        _create_session(store, owner="bob", request_key="bob-session-1"),
        request_key="bob-turn-1",
    )

    alice_claim = store.claim_turn(alice_turn_one.turn_id, worker_id="worker-1", lease_seconds=30)
    assert alice_claim is not None
    assert alice_claim.owner == "alice"
    assert store.claim_turn(alice_turn_two.turn_id, worker_id="worker-2", lease_seconds=30) is None
    bob_claim = store.claim_turn(bob_turn.turn_id, worker_id="worker-2", lease_seconds=30)
    assert bob_claim is not None
    assert bob_claim.owner == "bob"


def test_tool_invocation_replay_conflict_usage_and_stale_fence(tmp_path: Path) -> None:
    clock = MutableClock()
    store = _new_store(tmp_path / "agent.db", clock)
    turn = _create_turn(store, _create_session(store))
    claim = store.claim_turn(turn.turn_id, worker_id="worker-1", lease_seconds=30)
    assert claim is not None

    invocation, created = store.reserve_tool_invocation(
        invocation_id="inv-1",
        turn_id=claim.turn_id,
        session_id=claim.session_id,
        owner=claim.owner,
        tool_name="workspace.read",
        idempotency_key="read-workspace-1",
        arguments_digest="sha256:arguments-1",
        expected_state_version=claim.state_version,
        expected_fencing_token=claim.fencing_token,
    )
    replay, created = store.reserve_tool_invocation(
        invocation_id="inv-1",
        turn_id=claim.turn_id,
        session_id=claim.session_id,
        owner=claim.owner,
        tool_name="workspace.read",
        idempotency_key="read-workspace-1",
        arguments_digest="sha256:arguments-1",
        expected_state_version=claim.state_version,
        expected_fencing_token=claim.fencing_token,
    )
    assert created is False
    assert replay == invocation

    with pytest.raises(_store_api().AgentSessionConflict):
        store.reserve_tool_invocation(
            invocation_id="inv-2",
            turn_id=claim.turn_id,
            session_id=claim.session_id,
            owner=claim.owner,
            tool_name="workspace.read",
            idempotency_key="read-workspace-1",
            arguments_digest="sha256:different",
            expected_state_version=claim.state_version,
            expected_fencing_token=claim.fencing_token,
        )

    completed = store.finish_tool_invocation(
        invocation_id="inv-1",
        owner=claim.owner,
        expected_state_version=claim.state_version,
        expected_fencing_token=claim.fencing_token,
        result={"content": "hello"},
        error=None,
        bytes_returned=5,
    )
    assert completed.state == "completed"
    usage = store.get_turn_tool_usage(
        turn_id=claim.turn_id,
        owner=claim.owner,
        expected_state_version=claim.state_version,
        expected_fencing_token=claim.fencing_token,
    )
    assert usage.invocations == 1
    assert usage.bytes_returned == 5

    clock.advance(31)
    new_claim = store.claim_turn(claim.turn_id, worker_id="worker-2", lease_seconds=30)
    assert new_claim is not None
    with pytest.raises(_store_api().AgentSessionConflict):
        store.reserve_tool_invocation(
            invocation_id="inv-stale",
            turn_id=claim.turn_id,
            session_id=claim.session_id,
            owner=claim.owner,
            tool_name="workspace.read",
            idempotency_key="read-workspace-stale",
            arguments_digest="sha256:stale",
            expected_state_version=claim.state_version,
            expected_fencing_token=claim.fencing_token,
        )


def test_list_sessions_has_stable_descending_cursor(tmp_path: Path) -> None:
    clock = MutableClock()
    store = _new_store(tmp_path / "agent.db", clock)
    first = _create_session(store, request_key="session-1")
    clock.advance(1)
    second = _create_session(store, request_key="session-2")

    first_page, cursor = store.list_sessions_page(owner="alice", states=None, before=None, limit=1)
    assert first_page == [second]
    assert cursor is not None
    second_page, cursor = store.list_sessions_page(
        owner="alice", states=None, before=cursor, limit=1
    )
    assert second_page == [first]
    assert cursor is None


def test_sqlite_store_satisfies_backend_contract(tmp_path: Path) -> None:
    clock = MutableClock()
    store = _new_store(tmp_path / "agent-contract.db", clock)

    exercise_agent_store_contract(
        store,
        advance_clock=lambda delta: clock.advance(int(delta.total_seconds())),
    )
