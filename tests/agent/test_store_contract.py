from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta

import pytest

from pilot107.agent.session import AgentSessionConflict
from pilot107.agent.store import AgentSessionStore


def exercise_agent_store_contract(
    store: AgentSessionStore,
    *,
    advance_clock: Callable[[timedelta], None],
) -> None:
    session, created = store.create_session(
        owner="alice",
        request_key="session-1",
        profile_id="hpc-readonly-v1",
        model_profile_id="faux-default",
        source={"run_id": "run-1"},
    )
    assert created is True
    replay, replay_created = store.create_session(
        owner="alice",
        request_key="session-1",
        profile_id="hpc-readonly-v1",
        model_profile_id="faux-default",
        source={"run_id": "run-1"},
    )
    assert replay_created is False
    assert replay.session_id == session.session_id

    turn, turn_created = store.create_turn(
        session_id=session.session_id,
        owner="alice",
        request_key="turn-1",
        message="why pending?",
        expected_state_version=session.state_version,
    )
    assert turn_created is True
    first = store.claim_turn(turn.turn_id, worker_id="worker-a", lease_seconds=1)
    assert first is not None
    event = store.append_event(
        turn.turn_id,
        claim=first,
        sequence=1,
        event_type="turn_started",
        payload={},
    )
    assert event.sequence == 1

    invocation, invocation_created = store.reserve_tool_invocation(
        invocation_id="invocation-1",
        idempotency_key="tool-1",
        owner="alice",
        session_id=session.session_id,
        turn_id=turn.turn_id,
        expected_state_version=first.state_version,
        expected_fencing_token=first.fencing_token,
        tool_name="run_get",
        arguments_digest="sha256:arguments-1",
    )
    assert invocation_created is True
    invocation_replay, invocation_replay_created = store.reserve_tool_invocation(
        invocation_id="invocation-1",
        idempotency_key="tool-1",
        owner="alice",
        session_id=session.session_id,
        turn_id=turn.turn_id,
        expected_state_version=first.state_version,
        expected_fencing_token=first.fencing_token,
        tool_name="run_get",
        arguments_digest="sha256:arguments-1",
    )
    assert invocation_replay_created is False
    assert invocation_replay == invocation
    with pytest.raises(AgentSessionConflict):
        store.reserve_tool_invocation(
            invocation_id="invocation-conflict",
            idempotency_key="tool-1",
            owner="alice",
            session_id=session.session_id,
            turn_id=turn.turn_id,
            expected_state_version=first.state_version,
            expected_fencing_token=first.fencing_token,
            tool_name="run_get",
            arguments_digest="sha256:different",
        )
    store.finish_tool_invocation(
        invocation_id="invocation-1",
        owner="alice",
        expected_state_version=first.state_version,
        expected_fencing_token=first.fencing_token,
        result={"run_id": "run-1"},
        error=None,
        bytes_returned=17,
    )
    usage = store.get_turn_tool_usage(
        turn_id=turn.turn_id,
        owner="alice",
        expected_state_version=first.state_version,
        expected_fencing_token=first.fencing_token,
    )
    assert usage.invocations == 1
    assert usage.bytes_returned == 17

    advance_clock(timedelta(seconds=2))
    second = store.claim_turn(turn.turn_id, worker_id="worker-b", lease_seconds=30)
    assert second is not None
    assert second.fencing_token == first.fencing_token + 1
    with pytest.raises(AgentSessionConflict):
        store.append_event(
            turn.turn_id,
            claim=first,
            sequence=2,
            event_type="message_delta",
            payload={"delta": "stale"},
        )
    with pytest.raises(AgentSessionConflict):
        store.reserve_tool_invocation(
            invocation_id="invocation-stale",
            idempotency_key="tool-stale",
            owner="alice",
            session_id=session.session_id,
            turn_id=turn.turn_id,
            expected_state_version=first.state_version,
            expected_fencing_token=first.fencing_token,
            tool_name="run_get",
            arguments_digest="sha256:stale",
        )

    interrupted = store.interrupt_turn(
        turn.turn_id,
        claim=second,
        checkpoint={"summary": "resume safely"},
        error={"code": "transport_error"},
    )
    assert interrupted.state.value == "interrupted"
    assert turn.turn_id in {
        candidate.turn_id for candidate in store.list_recoverable_turns(limit=10)
    }

    third = store.claim_turn(turn.turn_id, worker_id="worker-c", lease_seconds=30)
    assert third is not None
    cancelling = store.request_cancel(
        turn.turn_id,
        owner="alice",
        expected_state_version=third.state_version,
    )
    assert cancelling.cancel_requested is True
    refreshed = store.renew_turn(third, lease_seconds=30)
    assert refreshed.state_version == cancelling.state_version
    completed = store.complete_turn(
        turn.turn_id,
        claim=refreshed,
        final_checkpoint={"summary": "cancelled"},
        resource_usage={"tool_invocations": 1, "bytes_returned": 17},
        outcome={"status": "cancelled"},
    )
    assert completed.state.value == "cancelled"
    assert store.get_session(session.session_id, owner="alice").state.value == "idle"
