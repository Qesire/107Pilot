from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from pilot107.agent.capabilities import AgentCapabilityClaims, AgentCapabilitySigner
from pilot107.agent.operation_ledger import AgentOperationState, operation_intent_for_invocation
from pilot107.agent.project_store import SQLiteProjectStore
from pilot107.agent.protocol import TOOL_INVOCATION_PROTOCOL_VERSION, ToolInvocation
from pilot107.agent.sandbox import SandboxExecutor
from pilot107.agent.store import SQLiteAgentSessionStore
from pilot107.agent.tool_gateway import AgentToolGateway, AgentToolGatewayError
from pilot107.services.project_agent_service import ProjectAgentService


class FixedClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 9, 5, 4, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value

    def epoch(self) -> int:
        return int(self.value.timestamp())


def _semantic_digest(arguments: dict[str, object]) -> str:
    encoded = json.dumps(
        arguments,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def test_workspace_policy_failure_marks_operation_failed_without_side_effect(
    tmp_path: Path,
) -> None:
    database = tmp_path / "pilot107.db"
    project_store = SQLiteProjectStore(database)
    service = ProjectAgentService(
        store=project_store,
        workspace_root=tmp_path / "agent-workspaces",
        sandbox=SandboxExecutor(store=project_store),
    )
    view = service.create_project(
        owner="alice",
        origin="blank",
        goal="reject forbidden workspace file",
        request_key="project-request",
    )

    clock = FixedClock()
    session_store = SQLiteAgentSessionStore(database, clock=clock)
    session, _ = session_store.create_session(
        owner="alice",
        request_key="session-request",
        profile_id="experiment_builder",
        model_profile_id="faux-default",
        source={
            "project_id": view.project.project_id,
            "workspace_id": view.workspace.workspace_id,
        },
    )
    turn, _ = session_store.create_turn(
        session_id=session.session_id,
        owner="alice",
        request_key="turn-policy-rejection",
        message="create a forbidden binary file",
        expected_state_version=session.state_version,
    )
    claim = session_store.claim_turn(turn.turn_id, worker_id="worker-1", lease_seconds=120)
    assert claim is not None

    arguments: dict[str, object] = {
        "project_id": view.project.project_id,
        "workspace_id": view.workspace.workspace_id,
        "approval_summary_zh": "创建不允许由 Agent 修改的二进制文件。",
        "patches": [
            {
                "path": "weights.bin",
                "expected_source_digest": None,
                "operation": "create",
                "content": "not-a-model",
            }
        ],
    }
    invocation = ToolInvocation(
        schema_version=TOOL_INVOCATION_PROTOCOL_VERSION,
        invocation_id="invocation-policy-rejection",
        idempotency_key="idempotency-policy-rejection",
        owner="alice",
        session_id=session.session_id,
        turn_id=turn.turn_id,
        state_version=claim.state_version,
        profile_id="experiment_builder",
        tool_name="workspace_patch",
        arguments=arguments,
        deadline="2026-09-05T04:01:30Z",
    )
    intent = operation_intent_for_invocation(
        session_store,
        invocation,
        arguments_digest=_semantic_digest(arguments),
    )
    assert intent is not None

    signer = AgentCapabilitySigner(b"s" * 32, clock=clock.epoch)
    claims = AgentCapabilityClaims(
        owner="alice",
        session_id=session.session_id,
        turn_id=turn.turn_id,
        state_version=claim.state_version,
        fencing_token=claim.fencing_token,
        profile_id="experiment_builder",
        tools=frozenset({"workspace_patch"}),
        max_invocations=8,
        max_bytes=262_144,
        expires_at=clock.epoch() + 120,
        project_id=view.project.project_id,
        workspace_id=view.workspace.workspace_id,
        operations=frozenset({"write"}),
        max_commands=4,
    )
    gateway = AgentToolGateway(
        store=session_store,
        signer=signer,
        handlers={},
        profile_handlers={"experiment_builder": service.build_tool_handlers()},
        clock=clock,
    )

    with pytest.raises(AgentToolGatewayError) as caught:
        gateway.invoke(signer.sign(claims), invocation)

    assert caught.value.code == "AGENT.TOOL.WORKSPACE_POLICY"
    assert gateway.operation_ledger is not None
    operation = gateway.operation_ledger.get(intent.operation_key, owner="alice")
    assert operation.state is AgentOperationState.FAILED
    assert operation.error is not None
    assert operation.error["code"] == "AGENT.TOOL.WORKSPACE_POLICY"
    assert not Path(view.workspace.local_root, "weights.bin").exists()
    assert project_store.list_change_sets(view.project.project_id, owner="alice") == []
