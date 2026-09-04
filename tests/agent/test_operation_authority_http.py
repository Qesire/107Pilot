from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from pilot107.agent.operation_gateway import operation_identity_for_invocation
from pilot107.agent.operation_ledger import SQLiteAgentOperationLedger
from pilot107.agent.protocol import (
    TOOL_INVOCATION_PROTOCOL_VERSION,
    TOOL_RESULT_PROTOCOL_VERSION,
    ToolInvocation,
    ToolResult,
)
from pilot107.api.agent_tool_routes import AgentToolRoutes


class RecordingGateway:
    def __init__(self, db_path: Path) -> None:
        self.store = SimpleNamespace(db_path=db_path)
        self.calls: list[str] = []

    def invoke(self, token: str, invocation: ToolInvocation) -> ToolResult:
        assert token == "capability"
        self.calls.append(invocation.invocation_id)
        if invocation.tool_name == "run_get":
            result = {"run_id": "run-1", "state": f"observation-{len(self.calls)}"}
        else:
            result = {
                "status": "scheduled",
                "run_id": "run-1",
                "submission_receipt_ref": "run-submit:run-1",
            }
        return ToolResult(
            schema_version=TOOL_RESULT_PROTOCOL_VERSION,
            invocation_id=invocation.invocation_id,
            result=result,
            error=None,
            evidence_refs=("run:run-1",),
            bytes_returned=64,
        )


def _mutation(call_id: str, legacy_idempotency: str) -> ToolInvocation:
    return ToolInvocation(
        schema_version=TOOL_INVOCATION_PROTOCOL_VERSION,
        invocation_id=call_id,
        idempotency_key=legacy_idempotency,
        owner="alice",
        session_id="session-1",
        turn_id="turn-1",
        state_version=2,
        profile_id="experiment_builder",
        tool_name="validation_schedule",
        arguments={
            "project_id": "project-1",
            "workspace_id": "workspace-1",
            "session_id": "session-1",
            "turn_id": "turn-1",
            "request_key": "user-request-1",
            "expected_revision": 4,
        },
        deadline="2026-09-04T05:00:00Z",
    )


def _read(call_id: str) -> ToolInvocation:
    return ToolInvocation(
        schema_version=TOOL_INVOCATION_PROTOCOL_VERSION,
        invocation_id=call_id,
        idempotency_key=f"legacy-{call_id}",
        owner="alice",
        session_id="session-read",
        turn_id="turn-read",
        state_version=2,
        profile_id="hpc-readonly-v1",
        tool_name="run_get",
        arguments={"run_id": "run-1"},
        deadline="2026-09-04T05:00:00Z",
    )


def _post(routes: AgentToolRoutes, invocation: ToolInvocation):
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
            "authorization": "Bearer capability",
            "content-type": "application/json",
        },
    )


def test_http_boundary_replays_mutation_for_new_provider_call_id(tmp_path: Path) -> None:
    delegate = RecordingGateway(tmp_path / "agent.db")
    routes = AgentToolRoutes(delegate)  # type: ignore[arg-type]

    first = _post(routes, _mutation("provider-call-a", "legacy-a"))
    replay = _post(routes, _mutation("provider-call-b", "legacy-b"))

    assert first is not None and first.status == 200
    assert replay is not None and replay.status == 200
    assert delegate.calls == ["provider-call-a"]
    assert first.payload["result"] == replay.payload["result"]
    assert replay.payload["invocation_id"] == "provider-call-b"


def test_http_boundary_keeps_read_tools_live(tmp_path: Path) -> None:
    delegate = RecordingGateway(tmp_path / "agent.db")
    routes = AgentToolRoutes(delegate)  # type: ignore[arg-type]

    first = _post(routes, _read("read-call-a"))
    second = _post(routes, _read("read-call-b"))

    assert first is not None and first.status == 200
    assert second is not None and second.status == 200
    assert delegate.calls == ["read-call-a", "read-call-b"]
    assert first.payload["result"]["state"] == "observation-1"
    assert second.payload["result"]["state"] == "observation-2"


def test_http_boundary_blocks_existing_running_mutation(tmp_path: Path) -> None:
    db_path = tmp_path / "agent.db"
    delegate = RecordingGateway(db_path)
    routes = AgentToolRoutes(delegate)  # type: ignore[arg-type]
    request = _mutation("provider-call-a", "legacy-a")
    identity = operation_identity_for_invocation(request)
    ledger = SQLiteAgentOperationLedger(db_path)
    receipt, created = ledger.reserve(identity, invocation_id=request.invocation_id)
    assert created
    ledger.mark_running(
        receipt.operation_key,
        owner="alice",
        invocation_id=request.invocation_id,
    )

    response = _post(routes, _mutation("provider-call-b", "legacy-b"))

    assert response is not None
    assert response.status == 409
    assert response.payload["error"]["code"] == "AGENT.OPERATION.IN_PROGRESS"
    assert response.payload["error"]["retryable"] is True
    assert delegate.calls == []
