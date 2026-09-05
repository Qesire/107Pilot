from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from pilot107.agent.capabilities import AgentCapabilityClaims, AgentCapabilitySigner
from pilot107.agent.operation_context import current_agent_operation_key
from pilot107.agent.operation_ledger import operation_intent_for_invocation
from pilot107.agent.protocol import TOOL_INVOCATION_PROTOCOL_VERSION, ToolInvocation
from pilot107.agent.store import SQLiteAgentSessionStore
from pilot107.agent.tool_gateway import AgentReadResult, AgentToolGateway


class FixedClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 9, 5, 2, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value

    def epoch(self) -> int:
        return int(self.value.timestamp())


def _semantic_digest(arguments: dict[str, object]) -> str:
    value = dict(arguments)
    value.pop("turn_id", None)
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def test_gateway_binds_internal_operation_key_only_while_handler_runs(tmp_path: Path) -> None:
    clock = FixedClock()
    store = SQLiteAgentSessionStore(tmp_path / "agent.db", clock=clock)
    session, _ = store.create_session(
        owner="alice",
        request_key="session-request",
        profile_id="experiment_builder",
        model_profile_id="faux-default",
        source={"project_id": "project-1", "workspace_id": "workspace-1"},
    )
    turn, _ = store.create_turn(
        session_id=session.session_id,
        owner="alice",
        request_key="turn-request",
        message="save project blueprint",
        expected_state_version=session.state_version,
    )
    claim = store.claim_turn(turn.turn_id, worker_id="worker-1", lease_seconds=120)
    assert claim is not None

    arguments: dict[str, object] = {
        "project_id": "project-1",
        "workspace_id": "workspace-1",
    }
    invocation = ToolInvocation(
        schema_version=TOOL_INVOCATION_PROTOCOL_VERSION,
        invocation_id="invocation-operation-context",
        idempotency_key="idempotency-operation-context",
        owner="alice",
        session_id=session.session_id,
        turn_id=turn.turn_id,
        state_version=claim.state_version,
        profile_id="experiment_builder",
        tool_name="project_blueprint_save",
        arguments=arguments,
        deadline="2026-09-05T02:01:30Z",
    )
    intent = operation_intent_for_invocation(
        store,
        invocation,
        arguments_digest=_semantic_digest(arguments),
    )
    assert intent is not None

    observed: list[str | None] = []

    def handler(owner: str, payload: object) -> AgentReadResult:
        assert owner == "alice"
        assert isinstance(payload, dict)
        assert "operation_key" not in payload
        observed.append(current_agent_operation_key())
        return AgentReadResult(result={"saved": True}, evidence_refs=("project:project-1",))

    signer = AgentCapabilitySigner(b"s" * 32, clock=clock.epoch)
    claims = AgentCapabilityClaims(
        owner="alice",
        session_id=session.session_id,
        turn_id=turn.turn_id,
        state_version=claim.state_version,
        fencing_token=claim.fencing_token,
        profile_id="experiment_builder",
        tools=frozenset({"project_blueprint_save"}),
        max_invocations=8,
        max_bytes=262_144,
        expires_at=clock.epoch() + 120,
        project_id="project-1",
        workspace_id="workspace-1",
        operations=frozenset({"write"}),
        max_commands=4,
    )
    gateway = AgentToolGateway(
        store=store,
        signer=signer,
        handlers={},
        profile_handlers={"experiment_builder": {"project_blueprint_save": handler}},
        clock=clock,
    )

    result = gateway.invoke(signer.sign(claims), invocation)

    assert result.result == {"saved": True}
    assert observed == [intent.operation_key]
    assert current_agent_operation_key() is None
