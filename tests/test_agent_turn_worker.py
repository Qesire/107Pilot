from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pilot107.agent.capabilities import AgentCapabilitySigner
from pilot107.agent.client import AgentdClientError
from pilot107.agent.project import ExperimentProjectState
from pilot107.agent.project_store import SQLiteProjectStore
from pilot107.agent.protocol import AgentTurnEvent, DurableAgentTurnRequest
from pilot107.agent.session import AgentTurnState
from pilot107.agent.store import SQLiteAgentSessionStore
from pilot107.core.control_repository import SQLiteControlRepository
from pilot107.services.agent_session_service import AgentSessionService
from pilot107.worker.agent_turn_worker import AgentTurnWorker


class MutableClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 19, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value

    def epoch(self) -> int:
        return int(self.value.timestamp())

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


class ScriptedAgentdClient:
    def __init__(self, steps: list[AgentTurnEvent | Exception]) -> None:
        self.steps = steps
        self.requests: list[DurableAgentTurnRequest] = []
        self.cancelled: list[str] = []

    def stream_durable_turn(
        self,
        request: DurableAgentTurnRequest,
        on_event=None,
    ) -> Iterator[AgentTurnEvent]:
        self.requests.append(request)
        for step in self.steps:
            if isinstance(step, Exception):
                raise step
            if on_event is not None:
                on_event(step)
            yield step

    def cancel_turn(self, turn_id: str) -> str:
        self.cancelled.append(turn_id)
        return "accepted"


class FailFirstAcknowledgeRepository(SQLiteControlRepository):
    def __init__(self, database: Path, *, clock: MutableClock) -> None:
        super().__init__(database, clock=clock)
        self.fail_acknowledge = True

    def acknowledge(self, *, message_id: str, owner: str, fencing_token: int) -> None:
        if self.fail_acknowledge:
            self.fail_acknowledge = False
            raise RuntimeError("simulated crash before outbox acknowledge")
        super().acknowledge(
            message_id=message_id,
            owner=owner,
            fencing_token=fencing_token,
        )


class CrashAfterFirstEventStore(SQLiteAgentSessionStore):
    crash_after_first_event = True

    def append_event(self, turn_id: str, **kwargs):
        event = super().append_event(turn_id, **kwargs)
        if self.crash_after_first_event:
            self.crash_after_first_event = False
            raise SystemExit("simulated process crash after durable event")
        return event


def _event(turn_id: str, sequence: int, event_type: str, payload: dict):
    return AgentTurnEvent(
        turn_id=turn_id,
        sequence=sequence,
        type=event_type,
        timestamp=f"2026-08-19T00:00:0{sequence}Z",
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
            "input_tokens": 10,
            "output_tokens": 4,
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
                "result": "run-1 failed",
                "provider": "faux-default",
                "model": "faux-1",
                "model_profile_id": "faux-default",
                "usage": checkpoint["usage"],
                "provider_calls": 2,
                "checkpoint_digest": "a" * 64,
                "duration_ms": 5,
                "checkpoint": checkpoint,
            },
        ),
    ]


def _queued_turn(
    store: SQLiteAgentSessionStore,
    control: SQLiteControlRepository,
    *,
    owner: str = "alice",
    suffix: str = "1",
    source: dict[str, str] | None = None,
):
    session, _ = store.create_session(
        owner=owner,
        request_key=f"session-{suffix}",
        profile_id="hpc-readonly-v1",
        model_profile_id="faux-default",
        source={"run_id": f"run-{suffix}"} if source is None else source,
    )
    service = AgentSessionService(store=store, control_repository=control)
    turn, _ = service.submit_message(
        session_id=session.session_id,
        owner=owner,
        request_key=f"turn-{suffix}",
        message=f"inspect run-{suffix}",
        expected_state_version=session.state_version,
    )
    return session, turn


def _worker(
    store: SQLiteAgentSessionStore,
    control: SQLiteControlRepository,
    client: ScriptedAgentdClient,
    clock: MutableClock,
    *,
    publish_event_hint=lambda _session_id, _sequence: None,
) -> AgentTurnWorker:
    return AgentTurnWorker(
        store=store,
        control_repository=control,
        agentd_client=client,
        capability_signer=AgentCapabilitySigner(b"s" * 32, clock=clock.epoch),
        worker_id="agent-worker-1",
        lease_seconds=30,
        clock=clock.epoch,
        publish_event_hint=publish_event_hint,
    )


def test_dispatch_persists_each_event_before_hint_and_completes(tmp_path: Path) -> None:
    clock = MutableClock()
    database = tmp_path / "agent.db"
    store = SQLiteAgentSessionStore(database, clock=clock)
    control = SQLiteControlRepository(database, clock=clock)
    session, turn = _queued_turn(store, control)
    client = ScriptedAgentdClient(_completed_script(turn.turn_id))
    hinted: list[int] = []

    def publish_hint(session_id: str, sequence: int) -> None:
        events, _ = store.list_events_page(
            session_id=session_id,
            owner="alice",
            after_event_id=0,
            limit=100,
        )
        assert [event.sequence for event in events] == list(range(1, sequence + 1))
        hinted.append(sequence)

    result = _worker(
        store,
        control,
        client,
        clock,
        publish_event_hint=publish_hint,
    ).dispatch_due(limit=1)

    assert result.checked == 1
    assert result.succeeded == 1
    assert result.errors == []
    assert hinted == [1, 2, 3]
    assert store.get_turn(turn.turn_id, owner="alice").state is AgentTurnState.COMPLETED
    assert store.get_session(session.session_id, owner="alice").state.value == "idle"
    assert control.get_outbox(f"agent-turn:{turn.turn_id}").state == "succeeded"
    assert len(client.requests) == 1
    claims = AgentCapabilitySigner(b"s" * 32, clock=clock.epoch).verify(
        client.requests[0].capability_token
    )
    assert claims.owner == "alice"
    assert claims.turn_id == turn.turn_id
    assert claims.fencing_token == 1
    assert claims.tools == frozenset(
        {
            "platform_get_snapshot",
            "platform_observation_get",
            "account_observation_get",
            "run_get",
            "run_log_read",
            "run_resources_get",
        }
    )
    persisted, _ = store.list_events_page(
        session_id=session.session_id,
        owner="alice",
        after_event_id=0,
        limit=100,
    )
    assert client.requests[0].capability_token not in repr(persisted)


def test_platform_only_turn_claims_no_unbound_resource_tools(tmp_path: Path) -> None:
    clock = MutableClock()
    database = tmp_path / "agent.db"
    store = SQLiteAgentSessionStore(database, clock=clock)
    control = SQLiteControlRepository(database, clock=clock)
    _, turn = _queued_turn(store, control, source={})
    client = ScriptedAgentdClient(_completed_script(turn.turn_id))

    result = _worker(store, control, client, clock).dispatch_due(limit=1)

    assert result.succeeded == 1
    assert client.requests[0].context_refs == ()
    claims = AgentCapabilitySigner(b"s" * 32, clock=clock.epoch).verify(
        client.requests[0].capability_token
    )
    assert claims.tools == frozenset(
        {
            "platform_get_snapshot",
            "platform_observation_get",
            "account_observation_get",
        }
    )


def test_repair_turn_receives_one_project_scoped_capability(tmp_path: Path) -> None:
    clock = MutableClock()
    database = tmp_path / "agent.db"
    store = SQLiteAgentSessionStore(database, clock=clock)
    control = SQLiteControlRepository(database, clock=clock)
    session, _ = AgentSessionService(
        store=store,
        control_repository=control,
    ).create_session(
        owner="alice",
        request_key="repair-session",
        profile_id="run_diagnosis_repair",
        model_profile_id="faux-default",
        source={
            "project_id": "project-repair",
            "workspace_id": "workspace-repair",
            "run_id": "run-failed",
            "remediation_session_id": "remsession-repair",
        },
    )
    turn, _ = AgentSessionService(
        store=store,
        control_repository=control,
    ).submit_message(
        session_id=session.session_id,
        owner="alice",
        request_key="repair-turn",
        message="repair train.py",
        expected_state_version=session.state_version,
    )
    events = _completed_script(turn.turn_id)
    checkpoint = events[1].payload["checkpoint"]
    assert isinstance(checkpoint, dict)
    checkpoint["prompt_profile_id"] = "run_diagnosis_repair"
    terminal_checkpoint = events[2].payload["checkpoint"]
    assert isinstance(terminal_checkpoint, dict)
    terminal_checkpoint["prompt_profile_id"] = "run_diagnosis_repair"
    client = ScriptedAgentdClient(events)

    result = _worker(store, control, client, clock).dispatch_due(limit=1)

    assert result.succeeded == 1
    [request] = client.requests
    claims = AgentCapabilitySigner(
        b"s" * 32, clock=clock.epoch
    ).verify(request.capability_token)
    assert claims.profile_id == "run_diagnosis_repair"
    assert claims.project_id == "project-repair"
    assert claims.workspace_id == "workspace-repair"
    assert "workspace_patch" in claims.tools


def test_transport_failure_interrupts_with_last_checkpoint_and_retries(
    tmp_path: Path,
) -> None:
    clock = MutableClock()
    database = tmp_path / "agent.db"
    store = SQLiteAgentSessionStore(database, clock=clock)
    control = SQLiteControlRepository(database, clock=clock)
    _, turn = _queued_turn(store, control)
    checkpoint_event = _completed_script(turn.turn_id)[1]
    client = ScriptedAgentdClient(
        [
            _completed_script(turn.turn_id)[0],
            checkpoint_event,
            AgentdClientError(
                "pilot-agentd transport failed",
                code="transport_error",
                retryable=True,
            ),
        ]
    )

    result = _worker(store, control, client, clock).dispatch_due(limit=1)

    assert result.checked == 1
    assert result.succeeded == 0
    assert len(result.errors) == 1
    interrupted = store.get_turn(turn.turn_id, owner="alice")
    assert interrupted.state is AgentTurnState.INTERRUPTED
    assert interrupted.final_checkpoint == checkpoint_event.payload["checkpoint"]
    assert control.get_outbox(f"agent-turn:{turn.turn_id}").state == "pending"


def test_transport_failure_without_checkpoint_retries_without_inventing_one(
    tmp_path: Path,
) -> None:
    clock = MutableClock()
    database = tmp_path / "agent.db"
    store = SQLiteAgentSessionStore(database, clock=clock)
    control = SQLiteControlRepository(database, clock=clock)
    _, turn = _queued_turn(store, control)
    client = ScriptedAgentdClient(
        [
            AgentdClientError(
                "pilot-agentd transport failed",
                code="transport_error",
                retryable=True,
            )
        ]
    )

    result = _worker(store, control, client, clock).dispatch_due(limit=1)

    assert len(result.errors) == 1
    interrupted = store.get_turn(turn.turn_id, owner="alice")
    assert interrupted.state is AgentTurnState.INTERRUPTED
    assert interrupted.final_checkpoint is None


def test_provider_unavailable_blocks_only_the_scoped_generative_project(
    tmp_path: Path,
) -> None:
    """Catch a failed generative Turn that leaves its Project deceptively active."""

    clock = MutableClock()
    database = tmp_path / "agent.db"
    store = SQLiteAgentSessionStore(database, clock=clock)
    control = SQLiteControlRepository(database, clock=clock)
    projects = SQLiteProjectStore(database, clock=clock)
    project = projects.create_project(
        owner="alice",
        origin="blank",
        goal="generate an experiment",
        request_key="model-unavailable-project",
    )
    session, _ = store.create_session(
        owner="alice",
        request_key="model-unavailable-session",
        profile_id="experiment_builder",
        model_profile_id="campus-default",
        source={
            "project_id": project.project_id,
            "workspace_id": "workspace-model-unavailable",
        },
    )
    turn, _ = AgentSessionService(
        store=store,
        control_repository=control,
    ).submit_message(
        session_id=session.session_id,
        owner="alice",
        request_key="model-unavailable-turn",
        message="generate the experiment files",
        expected_state_version=session.state_version,
    )
    client = ScriptedAgentdClient(
        [
            _event(
                turn.turn_id,
                1,
                "turn_started",
                {"model_profile_id": "campus-default", "task_kind": "interactive"},
            ),
            _event(
                turn.turn_id,
                2,
                "turn_failed",
                {
                    "error": {
                        "code": "provider_unavailable",
                        "message": "The model provider is unavailable.",
                        "retryable": False,
                    }
                },
            ),
        ]
    )
    worker = AgentTurnWorker(
        store=store,
        control_repository=control,
        agentd_client=client,
        capability_signer=AgentCapabilitySigner(b"s" * 32, clock=clock.epoch),
        project_store=projects,
        worker_id="agent-worker-model-unavailable",
        lease_seconds=30,
        clock=clock.epoch,
    )

    result = worker.dispatch_due(limit=1)

    assert result.succeeded == 1
    assert store.get_turn(turn.turn_id, owner="alice").state is AgentTurnState.FAILED
    assert (
        projects.get_project(project.project_id, owner="alice").state
        is ExperimentProjectState.BLOCKED
    )


def test_event_is_durable_before_crash_and_replay_keeps_one_sequence(tmp_path: Path) -> None:
    clock = MutableClock()
    database = tmp_path / "agent.db"
    store = CrashAfterFirstEventStore(database, clock=clock)
    control = SQLiteControlRepository(database, clock=clock)
    session, turn = _queued_turn(store, control)

    with pytest.raises(SystemExit, match="simulated process crash"):
        _worker(
            store,
            control,
            ScriptedAgentdClient([_completed_script(turn.turn_id)[0]]),
            clock,
        ).dispatch_due(limit=1)
    events, _ = store.list_events_page(
        session_id=session.session_id,
        owner="alice",
        after_event_id=0,
        limit=100,
    )
    assert [event.sequence for event in events] == [1]

    clock.advance(31)
    second = _worker(
        store,
        control,
        ScriptedAgentdClient(_completed_script(turn.turn_id)),
        clock,
    ).dispatch_due(limit=1)
    assert second.succeeded == 1
    events, _ = store.list_events_page(
        session_id=session.session_id,
        owner="alice",
        after_event_id=0,
        limit=100,
    )
    assert [event.sequence for event in events] == [1, 2, 3]


def test_lost_event_hints_do_not_interrupt_a_durable_turn(tmp_path: Path) -> None:
    clock = MutableClock()
    database = tmp_path / "agent.db"
    store = SQLiteAgentSessionStore(database, clock=clock)
    control = SQLiteControlRepository(database, clock=clock)
    _, turn = _queued_turn(store, control)

    def unavailable_hint_channel(_session_id: str, _sequence: int) -> None:
        raise RuntimeError("hint channel unavailable")

    result = _worker(
        store,
        control,
        ScriptedAgentdClient(_completed_script(turn.turn_id)),
        clock,
        publish_event_hint=unavailable_hint_channel,
    ).dispatch_due(limit=1)

    assert result.succeeded == 1
    assert store.get_turn(turn.turn_id, owner="alice").state is AgentTurnState.COMPLETED


def test_terminal_turn_is_recovered_after_crash_before_outbox_acknowledge(
    tmp_path: Path,
) -> None:
    clock = MutableClock()
    database = tmp_path / "agent.db"
    store = SQLiteAgentSessionStore(database, clock=clock)
    control = FailFirstAcknowledgeRepository(database, clock=clock)
    _, turn = _queued_turn(store, control)
    client = ScriptedAgentdClient(_completed_script(turn.turn_id))

    first = _worker(store, control, client, clock).dispatch_due(limit=1)
    assert len(first.errors) == 1
    assert store.get_turn(turn.turn_id, owner="alice").state is AgentTurnState.COMPLETED
    assert control.get_outbox(f"agent-turn:{turn.turn_id}").state == "running"

    clock.advance(31)
    second = _worker(store, control, client, clock).dispatch_due(limit=1)
    assert second.succeeded == 1
    assert len(client.requests) == 1
    assert control.get_outbox(f"agent-turn:{turn.turn_id}").state == "succeeded"


def test_cancel_before_invocation_persists_aborted_terminal_event(tmp_path: Path) -> None:
    clock = MutableClock()
    database = tmp_path / "agent.db"
    store = SQLiteAgentSessionStore(database, clock=clock)
    control = SQLiteControlRepository(database, clock=clock)
    session, turn = _queued_turn(store, control)
    store.request_cancel(
        turn.turn_id,
        owner="alice",
        expected_state_version=turn.state_version,
    )
    client = ScriptedAgentdClient([])

    result = _worker(store, control, client, clock).dispatch_due(limit=1)

    assert result.succeeded == 1
    assert client.requests == []
    assert client.cancelled == [turn.turn_id]
    events, _ = store.list_events_page(
        session_id=session.session_id,
        owner="alice",
        after_event_id=0,
        limit=100,
    )
    assert [(event.sequence, event.event_type) for event in events] == [(1, "turn_failed")]
    assert events[0].payload["error"]["code"] == "aborted"


def test_cancel_requested_after_event_calls_agentd_once_and_finishes_aborted(
    tmp_path: Path,
) -> None:
    clock = MutableClock()
    database = tmp_path / "agent.db"
    store = SQLiteAgentSessionStore(database, clock=clock)
    control = SQLiteControlRepository(database, clock=clock)
    session, turn = _queued_turn(store, control)
    client = ScriptedAgentdClient(
        [
            _completed_script(turn.turn_id)[0],
            _event(
                turn.turn_id,
                2,
                "turn_failed",
                {
                    "error": {
                        "code": "aborted",
                        "message": "The Turn was aborted.",
                        "retryable": False,
                    }
                },
            ),
        ]
    )
    requested = False

    def request_cancel_after_first_event(_session_id: str, sequence: int) -> None:
        nonlocal requested
        if sequence == 1 and not requested:
            requested = True
            current = store.get_turn(turn.turn_id, owner="alice")
            store.request_cancel(
                turn.turn_id,
                owner="alice",
                expected_state_version=current.state_version,
            )

    result = _worker(
        store,
        control,
        client,
        clock,
        publish_event_hint=request_cancel_after_first_event,
    ).dispatch_due(limit=1)

    assert result.succeeded == 1
    assert client.cancelled == [turn.turn_id]
    assert store.get_turn(turn.turn_id, owner="alice").state is AgentTurnState.CANCELLED
    assert store.get_session(session.session_id, owner="alice").state.value == "idle"


def test_blocked_alice_messages_do_not_prevent_bob_dispatch(tmp_path: Path) -> None:
    clock = MutableClock()
    database = tmp_path / "agent.db"
    store = SQLiteAgentSessionStore(database, clock=clock)
    control = SQLiteControlRepository(database, clock=clock)
    _, alice_active = _queued_turn(store, control, suffix="alice-active")
    _, alice_waiting = _queued_turn(store, control, suffix="alice-waiting")
    _, bob = _queued_turn(store, control, owner="bob", suffix="bob")
    assert store.claim_turn(
        alice_active.turn_id,
        worker_id="other-worker",
        lease_seconds=30,
    ) is not None
    client = ScriptedAgentdClient(_completed_script(bob.turn_id))

    result = _worker(store, control, client, clock).dispatch_due(limit=3)

    assert result.checked == 3
    assert result.succeeded == 1
    assert [request.turn_id for request in client.requests] == [bob.turn_id]
    assert store.get_turn(alice_waiting.turn_id, owner="alice").state is AgentTurnState.QUEUED
    assert store.get_turn(bob.turn_id, owner="bob").state is AgentTurnState.COMPLETED
