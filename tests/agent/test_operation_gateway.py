from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from pilot107.agent.operation_gateway import (
    OperationLedgerShadowGateway,
    operation_identity_for_invocation,
)
from pilot107.agent.operation_ledger import AgentOperationState, SQLiteAgentOperationLedger
from pilot107.agent.protocol import (
    TOOL_INVOCATION_PROTOCOL_VERSION,
    TOOL_RESULT_PROTOCOL_VERSION,
    ToolInvocation,
    ToolResult,
)
from pilot107.agent.tool_gateway import AgentToolGatewayError


class FixedClock:
    def __call__(self) -> datetime:
        return datetime(2026, 9, 4, 0, 0, tzinfo=UTC)


class FakeGateway:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[str] = []

    def invoke(self, token: str, invocation: ToolInvocation) -> ToolResult:
        del token
        self.calls.append(invocation.invocation_id)
        if self.fail:
            raise AgentToolGatewayError("denied", code="AGENT.TOOL.CAPABILITY_DENIED")
        return ToolResult(
            schema_version=TOOL_RESULT_PROTOCOL_VERSION,
            invocation_id=invocation.invocation_id,
            result={"state": "scheduled"},
            error=None,
            evidence_refs=("run:run-1",),
            bytes_returned=21,
        )


def _invocation(*, call: str, idempotency: str, arguments=None) -> ToolInvocation:
    return ToolInvocation(
        schema_version=TOOL_INVOCATION_PROTOCOL_VERSION,
        invocation_id=call,
        idempotency_key=idempotency,
        owner="alice",
        session_id="session-1",
        turn_id="turn-1",
        state_version=2,
        profile_id="experiment_builder",
        tool_name="validation_schedule",
        arguments=arguments
        or {
            "project_id": "project-1",
            "workspace_id": "workspace-1",
            "request_key": "validation-user-request-1",
            "expected_revision": 7,
        },
        deadline="2026-09-04T00:01:00Z",
    )


def test_operation_identity_ignores_provider_invocation_and_legacy_idempotency() -> None:
    first = operation_identity_for_invocation(
        _invocation(call="inv-provider-a", idempotency="idem-provider-a")
    )
    replay = operation_identity_for_invocation(
        _invocation(call="inv-provider-b", idempotency="idem-provider-b")
    )

    assert replay.operation_key == first.operation_key
    assert replay.intent_digest == first.intent_digest
    assert replay.user_request_key == "validation-user-request-1"
    assert replay.target_type == "agent_workspace"
    assert replay.target_id == "workspace-1"
    assert replay.target_revision == "7"


def test_fallback_request_identity_is_turn_scoped_not_tool_call_scoped() -> None:
    arguments = {"run_id": "run-1"}
    first = operation_identity_for_invocation(
        _invocation(call="inv-provider-a", idempotency="idem-provider-a", arguments=arguments)
    )
    replay = operation_identity_for_invocation(
        _invocation(call="inv-provider-b", idempotency="idem-provider-b", arguments=arguments)
    )

    assert first.operation_key == replay.operation_key
    assert first.user_request_key == "turn-1"


def test_shadow_gateway_records_legacy_success_without_changing_result(tmp_path: Path) -> None:
    ledger = SQLiteAgentOperationLedger(tmp_path / "agent.db", clock=FixedClock())
    delegate = FakeGateway()
    gateway = OperationLedgerShadowGateway(gateway=delegate, ledger=ledger)  # type: ignore[arg-type]
    invocation = _invocation(call="inv-provider-a", idempotency="idem-provider-a")

    result = gateway.invoke("capability", invocation)
    identity = operation_identity_for_invocation(invocation)
    receipt = ledger.get(identity.operation_key, owner="alice")

    assert result.result == {"state": "scheduled"}
    assert delegate.calls == ["inv-provider-a"]
    assert receipt.state is AgentOperationState.COMPLETED
    assert receipt.result_digest is not None
    assert receipt.result_ref is None
    assert receipt.side_effect_receipt_ref is None


def test_shadow_gateway_records_legacy_failure_and_preserves_exception(tmp_path: Path) -> None:
    ledger = SQLiteAgentOperationLedger(tmp_path / "agent.db", clock=FixedClock())
    delegate = FakeGateway(fail=True)
    gateway = OperationLedgerShadowGateway(gateway=delegate, ledger=ledger)  # type: ignore[arg-type]
    invocation = _invocation(call="inv-provider-a", idempotency="idem-provider-a")

    with pytest.raises(AgentToolGatewayError) as error:
        gateway.invoke("capability", invocation)

    assert error.value.code == "AGENT.TOOL.CAPABILITY_DENIED"
    receipt = ledger.get(
        operation_identity_for_invocation(invocation).operation_key,
        owner="alice",
    )
    assert receipt.state is AgentOperationState.FAILED
    assert receipt.error_code == "AGENT.TOOL.CAPABILITY_DENIED"


def test_shadow_gateway_still_calls_legacy_gateway_for_provider_replay(tmp_path: Path) -> None:
    ledger = SQLiteAgentOperationLedger(tmp_path / "agent.db", clock=FixedClock())
    delegate = FakeGateway()
    gateway = OperationLedgerShadowGateway(gateway=delegate, ledger=ledger)  # type: ignore[arg-type]

    first = gateway.invoke("capability", _invocation(call="inv-a", idempotency="idem-a"))
    second = gateway.invoke("capability", _invocation(call="inv-b", idempotency="idem-b"))

    assert first.result == second.result == {"state": "scheduled"}
    assert delegate.calls == ["inv-a", "inv-b"]
    receipt = ledger.get(
        operation_identity_for_invocation(
            _invocation(call="inv-a", idempotency="idem-a")
        ).operation_key,
        owner="alice",
    )
    assert receipt.state is AgentOperationState.COMPLETED
