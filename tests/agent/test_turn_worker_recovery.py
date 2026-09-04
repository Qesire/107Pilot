from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

from pilot107.agent.capabilities import AgentCapabilitySigner
from pilot107.agent.protocol import AgentdClientError, AgentTurnEvent, DurableAgentTurnRequest
from pilot107.agent.session import AgentTurnState
from pilot107.agent.store import SQLiteAgentSessionStore
from pilot107.core.control_repository import SQLiteControlRepository
from pilot107.services.agent_session_service import AgentSessionService
from pilot107.worker.agent_turn_worker import AgentTurnWorker


class FixedClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value

    def epoch(self) -> int:
        return int(self.value.timestamp())


class RetryOnceClient:
    def __init__(self) -> None:
        self.attempts = 0
        self.requests: list[DurableAgentTurnRequest] = []

    def stream_durable_turn(
        self,
        request: DurableAgentTurnRequest,
        on_event=None,
    ) -> Iterator[AgentTurnEvent]:
        self.attempts += 1
        self.requests.append(request)
        if self.attempts == 1:
            event = _event(
                request.turn_id,
                1,
                "turn_started",
                {
                    "model_profile_id": request.model_profile_id,
                    "task_kind": "interactive_readonly",
                },
            )
            if on_event is not None:
                on_event(event)
            yield event
            raise AgentdClientError(
                "pilot-agentd transport failed",
                code="transport_error",
                retryable=True,
            )
        for event in _completed_script(request.turn_id):
            if on_event is not None:
                on_event(event)
            yield event

    def cancel_turn(self, turn_id: str) -> str:
        del turn_id
        return "accepted"


def _event(
    turn_id: str,
    sequence: int,
    event_type: str,
    payload: dict[str, object],
) -> AgentTurnEvent:
    return AgentTurnEvent(
        turn_id=turn_id,
        sequence=sequence,
        type=event_type,
        timestamp=f"2026-09-04T12:00:0{sequence}Z",
        payload=payload,
    )


def _completed_script(turn_id: str) -> list[AgentTurnEvent]:
    checkpoint = {
        "schema_version": "pilot107.agent-checkpoint/v1",
        "turn_id": turn_id,
        "lineage": [],
        "model_profile_id": "faux-default",
        "prompt_profile_id": "hpc-readonly-v1",
        "messages": [],
        "completed_tools": [],
        "usage": {
            "input_tokens": 1,
            "output_tokens": 1,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
        },
        "digest": "a" * 64,
    }
    return [
        _event(
            turn_id,
            1,
            "turn_started",
            {
                "model_profile_id": "faux-default",
                "task_kind": "interactive_readonly",
            },
        ),
        _event(turn_id, 2, "checkpoint", {"checkpoint": checkpoint}),
        _event(
            turn_id,
            3,
            "turn_completed",
            {
                "result": {"text": "done"},
                "provider": "faux-default",
                "model": "faux-1",
                "model_profile_id": "faux-default",
                "usage": checkpoint["usage"],
                "provider_calls": 1,
                "checkpoint_digest": "a" * 64,
                "duration_ms": 1,
                "checkpoint": checkpoint,
            },
        ),
    ]


def test_worker_maps_attempt_local_sequences_onto_one_durable_turn_stream(
    tmp_path: Path,
) -> None:
    database = tmp_path / "agent.db"
    clock = FixedClock()
    store = SQLiteAgentSessionStore(database, clock=clock)
    control = SQLiteControlRepository(database, clock=clock)
    session, _ = store.create_session(
        owner="alice",
        request_key="session-retry-sequence",
        profile_id="hpc-readonly-v1",
        model_profile_id="faux-default",
        source={"run_id": "run-1"},
    )
    service = AgentSessionService(store=store, control_repository=control)
    turn, _ = service.submit_message(
        session_id=session.session_id,
        owner="alice",
        request_key="turn-retry-sequence",
        message="inspect run",
        expected_state_version=session.state_version,
    )
    client = RetryOnceClient()
    worker = AgentTurnWorker(
        store=store,
        control_repository=control,
        agentd_client=client,
        capability_signer=AgentCapabilitySigner(b"s" * 32, clock=clock.epoch),
        worker_id="agent-worker-retry-sequence",
        lease_seconds=30,
        clock=clock.epoch,
    )

    first = worker.dispatch_due(limit=1)

    assert first.checked == 1
    assert first.succeeded == 0
    assert len(first.errors) == 1
    interrupted = store.get_turn(turn.turn_id, owner="alice")
    assert interrupted.state is AgentTurnState.INTERRUPTED
    assert interrupted.event_sequence == 1

    second = worker.dispatch_due(limit=1)

    assert second.checked == 1
    assert second.succeeded == 1
    assert second.errors == []
    completed = store.get_turn(turn.turn_id, owner="alice")
    assert completed.state is AgentTurnState.COMPLETED
    assert completed.event_sequence == 4
    events, cursor = store.list_events_page(
        session_id=session.session_id,
        owner="alice",
        after_event_id=0,
        limit=100,
    )
    assert cursor is None
    assert [event.sequence for event in events] == [1, 2, 3, 4]
    assert [event.event_type for event in events] == [
        "turn_started",
        "turn_started",
        "checkpoint",
        "turn_completed",
    ]
    assert client.attempts == 2
