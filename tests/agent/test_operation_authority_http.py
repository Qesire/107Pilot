from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from pilot107.agent.capabilities import AgentCapabilityClaims, AgentCapabilitySigner
from pilot107.agent.operation_gateway import operation_identity_for_invocation
from pilot107.agent.operation_ledger import SQLiteAgentOperationLedger
from pilot107.agent.protocol import TOOL_INVOCATION_PROTOCOL_VERSION, ToolInvocation
from pilot107.agent.store import SQLiteAgentSessionStore
from pilot107.agent.tool_gateway import AgentReadResult, AgentToolGateway
from pilot107.api.agent_tool_routes import AgentToolRoutes


class FixedClock:
    def __call__(self) -> datetime:
        return datetime(2026, 9, 4, 4, 30, tzinfo=UTC)

    def epoch(self) -> int:
        return int(self().timestamp())


def _running_gateway(
    db_path: Path,
    *,
    profile_id: str,
    tool_name: str,
    handler,
):
    clock = FixedClock()
    store = SQLiteAgentSessionStore(db_path, clock=clock)
    source = (
        {"project_id": "project-1", "workspace_id": "workspace-1"}
        if profile_id == "experiment_builder"
        else {"run_id": "run-1"}
    )
    session, _ = store.create_session(
        owner="alice",
        request_key=f"session-{profile_id}",
        profile_id=profile_id,
        model_profile_id="faux-default",
        source=source,
    )
    turn, _ = store.create_turn(
        session_id=session.session_id,
        owner="alice",
        request_key=f"turn-{profile_id}",
        message="test durable operation authority",
        expected_state_version=session.state_version,
    )
    claim = store.claim_turn(turn.turn_id, worker_id="worker-1", lease_seconds=120)
    assert claim is not None
    signer = AgentCapabilitySigner(b"s" * 32, clock=clock.epoch)
    claims = AgentCapabilityClaims(
        owner="alice",
        session_id=session.session_id,
        turn_id=turn.turn_id,
        state_version=claim.state_version,
        fencing_token=claim.fencing_token,
        profile_id=profile_id,
        tools=frozenset({tool_name}),
        max_invocations=8,
        max_bytes=262_144,
        expires_at=clock.epoch() + 60,
        project_id="project-1" if profile_id == "experiment_builder" else None,
        workspace_id="workspace-1" if profile_id == "experiment_builder" else None,
        operations=(
            frozenset({"read", "write", "validate"})
            if profile_id == "experiment_builder"
            else frozenset()
        ),
        max_commands=4,
    )
    gateway = AgentToolGateway(
        store=store,
        signer=signer,
        handlers={tool_name: handler},
        clock=clock,
    )
    routes = AgentToolRoutes(gateway)
    return routes, signer.sign(claims), session, claim


def _mutation(session_id: str, turn_id: str, state_version: int, call_id: str) -> ToolInvocation:
    return ToolInvocation(
        schema_version=TOOL_INVOCATION_PROTOCOL_VERSION,
        invocation_id=call_id,
        idempotency_key=f"legacy-{call_id}",
        owner="alice",
        session_id=session_id,
        turn_id=turn_id,
        state_version=state_version,
        profile_id="experiment_builder",
        tool_name="validation_schedule",
        arguments={
            "project_id": "project-1",
            "workspace_id": "workspace-1",
            "session_id": session_id,
            "turn_id": turn_id,
            "request_key": "user-request-1",
            "expected_revision": 4,
        },
        deadline="2026-09-04T04:31:00Z",
    )


def _read(session_id: str, turn_id: str, state_version: int, call_id: str) -> ToolInvocation:
    return ToolInvocation(
        schema_version=TOOL_INVOCATION_PROTOCOL_VERSION,
        invocation_id=call_id,
        idempotency_key=f"legacy-{call_id}",
        owner="alice",
        session_id=session_id,
        turn_id=turn_id,
        state_version=state_version,
        profile_id="hpc-readonly-v1",
        tool_name="run_get",
        arguments={"run_id": "run-1"},
        deadline="2026-09-04T04:31:00Z",
    )


def _post(routes: AgentToolRoutes, token: str, invocation: ToolInvocation):
    return routes.handle_post(
        ["internal", "v1", "agent-tools", "invoke"],
        body=json.dumps(
            {
                "schema_version": invocation.schema_version,
                "invocation_id": invocation.invocation_id,
                "idempotency_key": invocation.idempotency_key,
                "owner": invocation.owner,
                "session_id": invocation.session_id,
                "turn_id": invocation.turn_id,
                "state_version": invocation.state_version,
                "profile_id": invocation.profile_id,
                "tool_name": invocation.tool_name,
                "arguments": invocation.arguments,
                "deadline": invocation.deadline,
            }
        ).encode("utf-8"),
        headers={
            "authorization": f"Bearer {token}",
            "content-type": "application/json",
        },
    )


def test_http_boundary_replays_mutation_for_new_provider_call_id(tmp_path: Path) -> None:
    calls: list[str] = []

    def schedule(owner, arguments):
        calls.append(str(arguments["request_key"]))
        return AgentReadResult(
            result={
                "status": "scheduled",
                "run_id": "run-1",
                "submission_receipt_ref": "run-submit:run-1",
            },
            evidence_refs=("run:run-1",),
        )

    db_path = tmp_path / "agent.db"
    routes, token, session, claim = _running_gateway(
        db_path,
        profile_id="experiment_builder",
        tool_name="validation_schedule",
        handler=schedule,
    )
    first_invocation = _mutation(
        session.session_id,
        claim.turn_id,
        claim.state_version,
        "provider-call-a",
    )
    replay_invocation = _mutation(
        session.session_id,
        claim.turn_id,
        claim.state_version,
        "provider-call-b",
    )

    first = _post(routes, token, first_invocation)
    replay = _post(routes, token, replay_invocation)

    assert first is not None and first.status == 200
    assert replay is not None and replay.status == 200
    assert calls == ["user-request-1"]
    assert first.payload["result"] == replay.payload["result"]
    assert replay.payload["invocation_id"] == "provider-call-b"


def test_http_boundary_keeps_read_tools_live(tmp_path: Path) -> None:
    calls = 0

    def read_run(owner, arguments):
        nonlocal calls
        calls += 1
        return AgentReadResult(
            result={"run_id": arguments["run_id"], "state": f"observation-{calls}"},
            evidence_refs=("run:run-1",),
        )

    routes, token, session, claim = _running_gateway(
        tmp_path / "agent.db",
        profile_id="hpc-readonly-v1",
        tool_name="run_get",
        handler=read_run,
    )
    first = _post(
        routes,
        token,
        _read(session.session_id, claim.turn_id, claim.state_version, "read-call-a"),
    )
    second = _post(
        routes,
        token,
        _read(session.session_id, claim.turn_id, claim.state_version, "read-call-b"),
    )

    assert first is not None and first.status == 200
    assert second is not None and second.status == 200
    assert calls == 2
    assert first.payload["result"]["state"] == "observation-1"
    assert second.payload["result"]["state"] == "observation-2"


def test_invalid_capability_does_not_create_operation_receipt(tmp_path: Path) -> None:
    calls: list[str] = []

    def schedule(owner, arguments):
        calls.append(owner)
        return AgentReadResult(result={"status": "scheduled"}, evidence_refs=())

    db_path = tmp_path / "agent.db"
    routes, _, session, claim = _running_gateway(
        db_path,
        profile_id="experiment_builder",
        tool_name="validation_schedule",
        handler=schedule,
    )
    request = _mutation(
        session.session_id,
        claim.turn_id,
        claim.state_version,
        "provider-call-a",
    )

    response = _post(routes, "not-a-valid-capability", request)

    assert response is not None and response.status == 401
    assert calls == []
    ledger = SQLiteAgentOperationLedger(db_path)
    with pytest.raises(KeyError):
        ledger.get(
            operation_identity_for_invocation(request).operation_key,
            owner="alice",
        )


def test_http_boundary_blocks_existing_running_mutation(tmp_path: Path) -> None:
    calls: list[str] = []

    def schedule(owner, arguments):
        calls.append(owner)
        return AgentReadResult(result={"status": "scheduled"}, evidence_refs=())

    db_path = tmp_path / "agent.db"
    routes, token, session, claim = _running_gateway(
        db_path,
        profile_id="experiment_builder",
        tool_name="validation_schedule",
        handler=schedule,
    )
    request = _mutation(
        session.session_id,
        claim.turn_id,
        claim.state_version,
        "provider-call-a",
    )
    ledger = SQLiteAgentOperationLedger(db_path)
    identity = operation_identity_for_invocation(request)
    receipt, created = ledger.reserve(identity, invocation_id=request.invocation_id)
    assert created
    ledger.mark_running(
        receipt.operation_key,
        owner="alice",
        invocation_id=request.invocation_id,
    )

    response = _post(
        routes,
        token,
        _mutation(
            session.session_id,
            claim.turn_id,
            claim.state_version,
            "provider-call-b",
        ),
    )

    assert response is not None
    assert response.status == 409
    assert response.payload["error"]["code"] == "AGENT.OPERATION.IN_PROGRESS"
    assert response.payload["error"]["retryable"] is True
    assert calls == []
