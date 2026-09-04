from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from pilot107.agent.operation_authority import OperationLedgerAuthoritativeGateway
from pilot107.agent.operation_gateway import operation_identity_for_invocation
from pilot107.agent.operation_ledger import AgentOperationState, SQLiteAgentOperationLedger
from pilot107.agent.operation_results import SQLiteAgentOperationResultStore
from pilot107.agent.protocol import (
    TOOL_INVOCATION_PROTOCOL_VERSION,
    TOOL_RESULT_PROTOCOL_VERSION,
    ToolInvocation,
    ToolResult,
)
from pilot107.agent.tool_gateway import AgentToolGatewayError


class FixedClock:
    def __call__(self) -> datetime:
        return datetime(2026, 9, 4, 4, 30, tzinfo=UTC)


class FakeGateway:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[str] = []

    def invoke(self, token: str, invocation: ToolInvocation) -> ToolResult:
        del token
        self.calls.append(invocation.invocation_id)
        if self.error is not None:
            raise self.error
        return ToolResult(
            schema_version=TOOL_RESULT_PROTOCOL_VERSION,
            invocation_id=invocation.invocation_id,
            result={
                "status": "scheduled",
                "run_id": "run-1",
                "submission_receipt_ref": "run-submit:run-1",
            },
            error=None,
            evidence_refs=("run:run-1",),
            bytes_returned=73,
        )


def invocation(call: str, legacy_key: str = "legacy-a") -> ToolInvocation:
    return ToolInvocation(
        schema_version=TOOL_INVOCATION_PROTOCOL_VERSION,
        invocation_id=call,
        idempotency_key=legacy_key,
        owner="alice",
        session_id="session-1",
        turn_id="turn-1",
        state_version=2,
        profile_id="experiment_builder",
        tool_name="validation_schedule",
        arguments={
            "project_id": "project-1",
            "workspace_id": "workspace-1",
            "request_key": "user-request-1",
            "expected_revision": 4,
        },
        deadline="2026-09-04T04:31:00Z",
    )


def authority(tmp_path: Path, delegate: FakeGateway):
    database = tmp_path / "agent.db"
    ledger = SQLiteAgentOperationLedger(database, clock=FixedClock())
    results = SQLiteAgentOperationResultStore(database, clock=FixedClock())
    gateway = OperationLedgerAuthoritativeGateway(
        gateway=delegate,  # type: ignore[arg-type]
        ledger=ledger,
        results=results,
    )
    return gateway, ledger, results


def test_completed_receipt_replays_for_new_provider_call_id_without_reexecution(
    tmp_path: Path,
) -> None:
    delegate = FakeGateway()
    gateway, ledger, _ = authority(tmp_path, delegate)

    first = gateway.invoke("token", invocation("provider-call-a", "legacy-a"))
    replay = gateway.invoke("token", invocation("provider-call-b", "legacy-b"))

    assert delegate.calls == ["provider-call-a"]
    assert first.result == replay.result
    assert replay.invocation_id == "provider-call-b"
    receipt = ledger.get(
        operation_identity_for_invocation(invocation("provider-call-z")).operation_key,
        owner="alice",
    )
    assert receipt.state is AgentOperationState.COMPLETED
    assert receipt.result_ref is not None
    assert receipt.result_digest is not None
    assert receipt.side_effect_receipt_ref == "run-submit:run-1"


def test_completed_receipt_fails_closed_when_replay_result_is_missing(tmp_path: Path) -> None:
    delegate = FakeGateway()
    gateway, ledger, results = authority(tmp_path, delegate)
    first_invocation = invocation("provider-call-a")
    gateway.invoke("token", first_invocation)
    receipt = ledger.get(
        operation_identity_for_invocation(first_invocation).operation_key,
        owner="alice",
    )
    assert receipt.result_ref is not None

    with results.connect() as conn:
        conn.execute(
            "DELETE FROM agent_operation_results WHERE result_ref = ?",
            (receipt.result_ref,),
        )

    with pytest.raises(AgentToolGatewayError) as error:
        gateway.invoke("token", invocation("provider-call-b", "legacy-b"))

    assert error.value.code == "AGENT.OPERATION.RESULT_UNAVAILABLE"
    assert delegate.calls == ["provider-call-a"]


def test_existing_running_operation_never_reexecutes(tmp_path: Path) -> None:
    delegate = FakeGateway()
    gateway, ledger, _ = authority(tmp_path, delegate)
    pending = invocation("provider-call-a")
    identity = operation_identity_for_invocation(pending)
    receipt, created = ledger.reserve(identity, invocation_id=pending.invocation_id)
    assert created
    ledger.mark_running(
        receipt.operation_key,
        owner="alice",
        invocation_id=pending.invocation_id,
    )

    with pytest.raises(AgentToolGatewayError) as error:
        gateway.invoke("token", invocation("provider-call-b", "legacy-b"))

    assert error.value.code == "AGENT.OPERATION.IN_PROGRESS"
    assert error.value.retryable is True
    assert delegate.calls == []


def test_unknown_execution_failure_is_not_retried_blindly(tmp_path: Path) -> None:
    delegate = FakeGateway(error=RuntimeError("transport vanished after side effect"))
    gateway, ledger, _ = authority(tmp_path, delegate)
    request = invocation("provider-call-a")

    with pytest.raises(RuntimeError):
        gateway.invoke("token", request)

    receipt = ledger.get(
        operation_identity_for_invocation(request).operation_key,
        owner="alice",
    )
    assert receipt.state is AgentOperationState.UNKNOWN

    with pytest.raises(AgentToolGatewayError) as replay_error:
        gateway.invoke("token", invocation("provider-call-b", "legacy-b"))
    assert replay_error.value.code == "AGENT.OPERATION.RECONCILIATION_REQUIRED"
    assert delegate.calls == ["provider-call-a"]


def test_stable_gateway_failure_is_terminal_and_replayed_without_delegate(
    tmp_path: Path,
) -> None:
    delegate = FakeGateway(
        error=AgentToolGatewayError(
            "denied",
            code="AGENT.TOOL.CAPABILITY_DENIED",
        )
    )
    gateway, ledger, _ = authority(tmp_path, delegate)
    request = invocation("provider-call-a")

    with pytest.raises(AgentToolGatewayError) as first:
        gateway.invoke("token", request)
    assert first.value.code == "AGENT.TOOL.CAPABILITY_DENIED"

    receipt = ledger.get(
        operation_identity_for_invocation(request).operation_key,
        owner="alice",
    )
    assert receipt.state is AgentOperationState.FAILED

    with pytest.raises(AgentToolGatewayError) as replay:
        gateway.invoke("token", invocation("provider-call-b", "legacy-b"))
    assert replay.value.code == "AGENT.TOOL.CAPABILITY_DENIED"
    assert delegate.calls == ["provider-call-a"]
