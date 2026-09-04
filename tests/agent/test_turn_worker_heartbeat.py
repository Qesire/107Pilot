from __future__ import annotations

import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

from pilot107.agent.capabilities import AgentCapabilitySigner
from pilot107.agent.protocol import AgentTurnEvent, DurableAgentTurnRequest
from pilot107.agent.session import AgentTurnState
from pilot107.agent.store import SQLiteAgentSessionStore
from pilot107.core.control_repository import SQLiteControlRepository
from pilot107.services.agent_session_service import AgentSessionService
from pilot107.worker.agent_turn_worker import AgentTurnWorker


class FixedClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 9, 4, 11, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value

    def epoch(self) -> int:
        return int(self.value.timestamp())


class RenewalProbe:
    def __init__(self) -> None:
        self.turn_renewals = 0
        self.outbox_renewals = 0
        self.ready = threading.Event()
        self._lock = threading.Lock()

    def note_turn(self) -> None:
        with self._lock:
            self.turn_renewals += 1
            self._update()

    def note_outbox(self) -> None:
        with self._lock:
            self.outbox_renewals += 1
            self._update()

    def _update(self) -> None:
        if self.turn_renewals > 0 and self.outbox_renewals > 0:
            self.ready.set()


class RecordingSessionStore(SQLiteAgentSessionStore):
    def __init__(self, database: Path, *, clock: FixedClock, probe: RenewalProbe) -> None:
        super().__init__(database, clock=clock)
        self.probe = probe

    def renew_turn(self, claim, *, lease_seconds: int):
        renewed = super().renew_turn(claim, lease_seconds=lease_seconds)
        self.probe.note_turn()
        return renewed


class RecordingControlRepository(SQLiteControlRepository):
    def __init__(self, database: Path, *, clock: FixedClock, probe: RenewalProbe) -> None:
        super().__init__(database, clock=clock)
        self.probe = probe

    def renew_outbox(
        self,
        *,
        message_id: str,
        owner: str,
        fencing_token: int,
        lease_seconds: int,
    ):
        renewed = super().renew_outbox(
            message_id=message_id,
            owner=owner,
            fencing_token=fencing_token,
            lease_seconds=lease_seconds,
        )
        self.probe.note_outbox()
        return renewed


class FastHeartbeatWorker(AgentTurnWorker):
    def _heartbeat_interval_seconds(self) -> float:
        return 0.01


class RenewalBlockingClient:
    def __init__(self, probe: RenewalProbe) -> None:
        self.probe = probe

    def stream_durable_turn(
        self,
        request: DurableAgentTurnRequest,
        on_event=None,
    ) -> Iterator[AgentTurnEvent]:
        assert self.probe.ready.wait(1.0)
        for event in _completed_script(request.turn_id):
            if on_event is not None:
                on_event(event)
            yield event

    def cancel_turn(self, turn_id: str) -> str:
        return "accepted"


def _event(turn_id: str, sequence: int, event_type: str, payload: dict) -> AgentTurnEvent:
    return AgentTurnEvent(
        turn_id=turn_id,
        sequence=sequence,
        type=event_type,
        timestamp=f"2026-09-04T11:00:0{sequence}Z",
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
            {"model_profile_id": "faux-default", "task_kind": "interactive"},
        ),
        _event(turn_id, 2, "checkpoint", {"checkpoint": checkpoint}),
        _event(
            turn_id,
            3,
            "turn_completed",
            {
                "result": "done",
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


def test_worker_renews_turn_and_outbox_during_long_stream(tmp_path: Path) -> None:
    database = tmp_path / "agent.db"
    clock = FixedClock()
    probe = RenewalProbe()
    store = RecordingSessionStore(database, clock=clock, probe=probe)
    control = RecordingControlRepository(database, clock=clock, probe=probe)
    session, _ = store.create_session(
        owner="alice",
        request_key="session-heartbeat",
        profile_id="hpc-readonly-v1",
        model_profile_id="faux-default",
        source={"run_id": "run-1"},
    )
    service = AgentSessionService(store=store, control_repository=control)
    turn, _ = service.submit_message(
        session_id=session.session_id,
        owner="alice",
        request_key="turn-heartbeat",
        message="inspect run",
        expected_state_version=session.state_version,
    )
    worker = FastHeartbeatWorker(
        store=store,
        control_repository=control,
        agentd_client=RenewalBlockingClient(probe),
        capability_signer=AgentCapabilitySigner(b"s" * 32, clock=clock.epoch),
        worker_id="agent-worker-heartbeat",
        lease_seconds=30,
        clock=clock.epoch,
    )

    result = worker.dispatch_due(limit=1)

    assert result.checked == 1
    assert result.succeeded == 1
    assert result.errors == []
    assert probe.turn_renewals >= 1
    assert probe.outbox_renewals >= 1
    assert store.get_turn(turn.turn_id, owner="alice").state is AgentTurnState.COMPLETED
    assert control.get_outbox(f"agent-turn:{turn.turn_id}").state == "succeeded"
