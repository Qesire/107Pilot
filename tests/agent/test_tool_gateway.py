from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pilot107.agent.protocol import TOOL_INVOCATION_PROTOCOL_VERSION, ToolInvocation
from pilot107.agent.store import SQLiteAgentSessionStore


class MutableClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 14, 0, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value

    def epoch(self) -> int:
        return int(self.value.timestamp())

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


def _gateway_api():
    from pilot107.agent.capabilities import AgentCapabilityClaims, AgentCapabilitySigner
    from pilot107.agent.tool_gateway import (
        AgentReadResult,
        AgentToolGateway,
        AgentToolGatewayError,
    )

    return (
        AgentCapabilityClaims,
        AgentCapabilitySigner,
        AgentReadResult,
        AgentToolGateway,
        AgentToolGatewayError,
    )


def _running_turn(database: Path, clock: MutableClock):
    store = SQLiteAgentSessionStore(database, clock=clock)
    session, _ = store.create_session(
        owner="alice",
        request_key="session-1",
        profile_id="hpc-readonly-v1",
        model_profile_id="faux-default",
        source={"run_id": "run-1"},
    )
    turn, _ = store.create_turn(
        session_id=session.session_id,
        owner="alice",
        request_key="turn-1",
        message="why pending?",
        expected_state_version=session.state_version,
    )
    claim = store.claim_turn(turn.turn_id, worker_id="worker-1", lease_seconds=30)
    assert claim is not None
    return store, session, turn, claim


def _invocation(session, turn, claim, **changes):
    values = {
        "schema_version": TOOL_INVOCATION_PROTOCOL_VERSION,
        "invocation_id": "invocation-1",
        "idempotency_key": "tool-1",
        "owner": "alice",
        "session_id": session.session_id,
        "turn_id": turn.turn_id,
        "state_version": claim.state_version,
        "profile_id": "hpc-readonly-v1",
        "tool_name": "run_get",
        "arguments": {"run_id": "run-1"},
        "deadline": "2026-08-14T00:00:20Z",
    }
    values.update(changes)
    return ToolInvocation(**values)


def _claims(clock, session, turn, claim, **changes):
    AgentCapabilityClaims, _, _, _, _ = _gateway_api()
    values = {
        "owner": "alice",
        "session_id": session.session_id,
        "turn_id": turn.turn_id,
        "state_version": claim.state_version,
        "fencing_token": claim.fencing_token,
        "profile_id": "hpc-readonly-v1",
        "tools": frozenset({"run_get"}),
        "max_invocations": 8,
        "max_bytes": 262_144,
        "expires_at": clock.epoch() + 60,
    }
    values.update(changes)
    return AgentCapabilityClaims(**values)


def test_gateway_reserves_before_read_and_replays_stored_result(tmp_path: Path) -> None:
    (
        _,
        AgentCapabilitySigner,
        AgentReadResult,
        AgentToolGateway,
        _,
    ) = _gateway_api()
    clock = MutableClock()
    store, session, turn, claim = _running_turn(tmp_path / "agent.db", clock)
    calls = []

    def read_run(owner, arguments):
        calls.append((owner, arguments))
        return AgentReadResult(
            result={"run_id": arguments["run_id"], "state": "pending"},
            evidence_refs=("run:run-1",),
        )

    signer = AgentCapabilitySigner(b"s" * 32, clock=clock.epoch)
    gateway = AgentToolGateway(
        store=store,
        signer=signer,
        handlers={"run_get": read_run},
        clock=clock,
    )
    token = signer.sign(_claims(clock, session, turn, claim))
    invocation = _invocation(session, turn, claim)

    first = gateway.invoke(token, invocation)
    replay = gateway.invoke(token, invocation)

    assert first == replay
    assert first.result == {"run_id": "run-1", "state": "pending"}
    assert first.evidence_refs == ("run:run-1",)
    assert first.bytes_returned > 0
    assert calls == [("alice", {"run_id": "run-1"})]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("owner", "mallory"),
        ("session_id", "session-other"),
        ("turn_id", "turn-other"),
        ("state_version", 999),
        ("profile_id", "other-profile"),
        ("tool_name", "evidence_read"),
    ],
)
def test_gateway_rejects_wrong_binding_before_reader(
    tmp_path: Path, field: str, value: object
) -> None:
    _, AgentCapabilitySigner, _, AgentToolGateway, AgentToolGatewayError = _gateway_api()
    clock = MutableClock()
    store, session, turn, claim = _running_turn(tmp_path / "agent.db", clock)
    called = []
    signer = AgentCapabilitySigner(b"s" * 32, clock=clock.epoch)
    gateway = AgentToolGateway(
        store=store,
        signer=signer,
        handlers={"run_get": lambda owner, arguments: called.append(owner)},
        clock=clock,
    )
    token = signer.sign(_claims(clock, session, turn, claim))

    with pytest.raises(AgentToolGatewayError):
        gateway.invoke(token, _invocation(session, turn, claim, **{field: value}))
    assert called == []


def test_gateway_rejects_changed_idempotency_content(tmp_path: Path) -> None:
    _, AgentCapabilitySigner, AgentReadResult, AgentToolGateway, AgentToolGatewayError = (
        _gateway_api()
    )
    clock = MutableClock()
    store, session, turn, claim = _running_turn(tmp_path / "agent.db", clock)
    signer = AgentCapabilitySigner(b"s" * 32, clock=clock.epoch)
    gateway = AgentToolGateway(
        store=store,
        signer=signer,
        handlers={
            "run_get": lambda owner, arguments: AgentReadResult(
                result={"run_id": arguments["run_id"]}, evidence_refs=()
            )
        },
        clock=clock,
    )
    token = signer.sign(_claims(clock, session, turn, claim))
    gateway.invoke(token, _invocation(session, turn, claim))

    with pytest.raises(AgentToolGatewayError) as conflict:
        gateway.invoke(
            token,
            _invocation(
                session,
                turn,
                claim,
                invocation_id="invocation-2",
                arguments={"run_id": "run-2"},
            ),
        )
    assert conflict.value.code == "AGENT.TOOL.IDEMPOTENCY_CONFLICT"


def test_gateway_enforces_invocation_and_cumulative_byte_budgets(tmp_path: Path) -> None:
    _, AgentCapabilitySigner, AgentReadResult, AgentToolGateway, AgentToolGatewayError = (
        _gateway_api()
    )
    clock = MutableClock()
    store, session, turn, claim = _running_turn(tmp_path / "agent.db", clock)
    signer = AgentCapabilitySigner(b"s" * 32, clock=clock.epoch)
    gateway = AgentToolGateway(
        store=store,
        signer=signer,
        handlers={
            "run_get": lambda owner, arguments: AgentReadResult(
                result={"content": "x" * 100}, evidence_refs=()
            )
        },
        clock=clock,
    )
    byte_token = signer.sign(_claims(clock, session, turn, claim, max_bytes=20))
    with pytest.raises(AgentToolGatewayError) as bytes_error:
        gateway.invoke(byte_token, _invocation(session, turn, claim))
    assert bytes_error.value.code == "AGENT.TOOL.BYTE_BUDGET_EXCEEDED"

    count_token = signer.sign(_claims(clock, session, turn, claim, max_invocations=1))
    with pytest.raises(AgentToolGatewayError) as count_error:
        gateway.invoke(
            count_token,
            _invocation(
                session,
                turn,
                claim,
                invocation_id="invocation-2",
                idempotency_key="tool-2",
            ),
        )
    assert count_error.value.code == "AGENT.TOOL.INVOCATION_BUDGET_EXCEEDED"


def test_gateway_rejects_stale_fence_without_reader_access(tmp_path: Path) -> None:
    _, AgentCapabilitySigner, _, AgentToolGateway, AgentToolGatewayError = _gateway_api()
    clock = MutableClock()
    store, session, turn, claim = _running_turn(tmp_path / "agent.db", clock)
    called = []
    signer = AgentCapabilitySigner(b"s" * 32, clock=clock.epoch)
    gateway = AgentToolGateway(
        store=store,
        signer=signer,
        handlers={"run_get": lambda owner, arguments: called.append(owner)},
        clock=clock,
    )
    token = signer.sign(_claims(clock, session, turn, claim))
    clock.advance(31)
    reclaimed = store.claim_turn(turn.turn_id, worker_id="worker-2", lease_seconds=30)
    assert reclaimed is not None

    with pytest.raises(AgentToolGatewayError) as fenced:
        gateway.invoke(
            token,
            _invocation(session, turn, claim, deadline="2026-08-14T00:01:00Z"),
        )
    assert fenced.value.code == "AGENT.TOOL.FENCED"
    assert called == []


def test_gateway_persists_invalid_handler_result_for_stable_replay(tmp_path: Path) -> None:
    _, AgentCapabilitySigner, _, AgentToolGateway, AgentToolGatewayError = _gateway_api()
    clock = MutableClock()
    store, session, turn, claim = _running_turn(tmp_path / "agent.db", clock)
    signer = AgentCapabilitySigner(b"s" * 32, clock=clock.epoch)
    gateway = AgentToolGateway(
        store=store,
        signer=signer,
        handlers={"run_get": lambda owner, arguments: None},  # type: ignore[dict-item]
        clock=clock,
    )
    token = signer.sign(_claims(clock, session, turn, claim))
    invocation = _invocation(session, turn, claim)

    for _ in range(2):
        with pytest.raises(AgentToolGatewayError) as invalid:
            gateway.invoke(token, invocation)
        assert invalid.value.code == "AGENT.TOOL.INVALID_RESULT"


@pytest.mark.parametrize("binding", ["session_id", "turn_id"])
def test_validation_schedule_rejects_argument_binding_spoof_before_handler(
    tmp_path: Path, binding: str
) -> None:
    _, AgentCapabilitySigner, AgentReadResult, AgentToolGateway, AgentToolGatewayError = (
        _gateway_api()
    )
    clock = MutableClock()
    store = SQLiteAgentSessionStore(tmp_path / "agent.db", clock=clock)
    session, _ = store.create_session(
        owner="alice",
        request_key="builder-session",
        profile_id="experiment_builder",
        model_profile_id="faux-default",
        source={"project_id": "project-1", "workspace_id": "workspace-1"},
    )
    turn, _ = store.create_turn(
        session_id=session.session_id,
        owner="alice",
        request_key="builder-turn",
        message="validate",
        expected_state_version=session.state_version,
    )
    claim = store.claim_turn(turn.turn_id, worker_id="worker-1", lease_seconds=30)
    assert claim is not None
    called: list[str] = []

    def schedule(owner, arguments):
        called.append(owner)
        return AgentReadResult(result={}, evidence_refs=())

    signer = AgentCapabilitySigner(b"s" * 32, clock=clock.epoch)
    gateway = AgentToolGateway(
        store=store,
        signer=signer,
        handlers={"validation_schedule": schedule},
        clock=clock,
    )
    token = signer.sign(
        _claims(
            clock,
            session,
            turn,
            claim,
            profile_id="experiment_builder",
            tools=frozenset({"validation_schedule"}),
            project_id="project-1",
            workspace_id="workspace-1",
            operations=frozenset({"validate"}),
        )
    )
    arguments = {
        "project_id": "project-1",
        "workspace_id": "workspace-1",
        "session_id": session.session_id,
        "turn_id": turn.turn_id,
    }
    arguments[binding] = "spoofed-binding"
    invocation = _invocation(
        session,
        turn,
        claim,
        profile_id="experiment_builder",
        tool_name="validation_schedule",
        arguments=arguments,
    )

    with pytest.raises(AgentToolGatewayError) as error:
        gateway.invoke(token, invocation)

    assert error.value.code == "AGENT.TOOL.CAPABILITY_DENIED"
    assert called == []


@pytest.mark.parametrize("binding", ["session_id", "turn_id"])
def test_builder_submit_rejects_argument_binding_spoof_before_handler(
    tmp_path: Path, binding: str
) -> None:
    _, AgentCapabilitySigner, AgentReadResult, AgentToolGateway, AgentToolGatewayError = (
        _gateway_api()
    )
    clock = MutableClock()
    store = SQLiteAgentSessionStore(tmp_path / "builder.db", clock=clock)
    session, _ = store.create_session(
        owner="alice",
        request_key="builder-session",
        profile_id="experiment_builder",
        model_profile_id="faux-default",
        source={"project_id": "project-1", "workspace_id": "workspace-1"},
    )
    turn, _ = store.create_turn(
        session_id=session.session_id,
        owner="alice",
        request_key="builder-turn",
        message="build",
        expected_state_version=session.state_version,
    )
    claim = store.claim_turn(turn.turn_id, worker_id="worker-1", lease_seconds=30)
    assert claim is not None
    called: list[str] = []

    def submit(owner, arguments):
        called.append(owner)
        return AgentReadResult(result={}, evidence_refs=())

    signer = AgentCapabilitySigner(b"s" * 32, clock=clock.epoch)
    gateway = AgentToolGateway(
        store=store,
        signer=signer,
        handlers={"builder_build_submit": submit},
        clock=clock,
    )
    token = signer.sign(
        _claims(
            clock,
            session,
            turn,
            claim,
            profile_id="experiment_builder",
            tools=frozenset({"builder_build_submit"}),
            project_id="project-1",
            workspace_id="workspace-1",
            operations=frozenset({"write", "validate"}),
            max_commands=1,
        )
    )
    arguments = {
        "project_id": "project-1",
        "workspace_id": "workspace-1",
        "session_id": session.session_id,
        "turn_id": turn.turn_id,
    }
    arguments[binding] = "spoofed-binding"
    invocation = _invocation(
        session,
        turn,
        claim,
        profile_id="experiment_builder",
        tool_name="builder_build_submit",
        arguments=arguments,
    )

    with pytest.raises(AgentToolGatewayError) as error:
        gateway.invoke(token, invocation)

    assert error.value.code == "AGENT.TOOL.CAPABILITY_DENIED"
    assert called == []
