"""Authoritative durable-operation replay in front of the legacy Tool Gateway.

The Pi/model loop stays unchanged.  This control-plane adapter decides whether a
provider-emitted *mutating* tool call represents a new domain operation or a
replay of an already completed one.  A completed receipt with a replayable
``result_ref`` is returned without invoking the legacy Gateway again.  Read-only
and ephemeral validation tools keep their original live semantics.
"""

from __future__ import annotations

from typing import Protocol

from pilot107.agent.operation_gateway import operation_identity_for_invocation
from pilot107.agent.operation_ledger import (
    AgentOperationReceiptRecord,
    AgentOperationState,
    DurableOperationIdentity,
)
from pilot107.agent.operation_results import (
    AgentOperationResultRecord,
    replay_tool_result,
)
from pilot107.agent.protocol import ToolInvocation, ToolResult
from pilot107.agent.tool_gateway import AgentToolGateway, AgentToolGatewayError

# Durable replay protects domain mutations and cluster scheduling.  Ordinary
# reads must remain live, while sandbox_exec is deliberately excluded because
# it is an ephemeral validation step rather than a persisted domain mutation.
DURABLE_MUTATION_TOOL_NAMES = frozenset(
    {
        "project_blueprint_save",
        "workspace_patch",
        "validation_schedule",
        "builder_build_submit",
    }
)


class AuthoritativeOperationLedger(Protocol):
    def reserve(
        self,
        identity: DurableOperationIdentity,
        *,
        invocation_id: str,
    ) -> tuple[AgentOperationReceiptRecord, bool]: ...

    def get(self, operation_key: str, *, owner: str) -> AgentOperationReceiptRecord: ...

    def mark_running(
        self,
        operation_key: str,
        *,
        owner: str,
        invocation_id: str,
    ) -> AgentOperationReceiptRecord: ...

    def complete(
        self,
        operation_key: str,
        *,
        owner: str,
        invocation_id: str,
        result_digest: str,
        result_ref: str | None,
        side_effect_receipt_ref: str | None,
    ) -> AgentOperationReceiptRecord: ...

    def fail(
        self,
        operation_key: str,
        *,
        owner: str,
        invocation_id: str,
        error_code: str,
    ) -> AgentOperationReceiptRecord: ...

    def mark_unknown(
        self,
        operation_key: str,
        *,
        owner: str,
    ) -> AgentOperationReceiptRecord: ...


class AuthoritativeOperationResultStore(Protocol):
    def put(
        self,
        *,
        operation_key: str,
        owner: str,
        result: ToolResult,
    ) -> AgentOperationResultRecord: ...

    def get(self, result_ref: str, *, owner: str) -> AgentOperationResultRecord: ...


class OperationLedgerAuthoritativeGateway:
    """Suppress duplicate domain execution once a terminal receipt is durable."""

    def __init__(
        self,
        *,
        gateway: AgentToolGateway,
        ledger: AuthoritativeOperationLedger,
        results: AuthoritativeOperationResultStore,
        protected_tools: frozenset[str] = DURABLE_MUTATION_TOOL_NAMES,
    ) -> None:
        self.gateway = gateway
        self.ledger = ledger
        self.results = results
        self.protected_tools = protected_tools

    def invoke(self, token: str, invocation: ToolInvocation) -> ToolResult:
        if invocation.tool_name not in self.protected_tools:
            return self.gateway.invoke(token, invocation)

        identity = operation_identity_for_invocation(invocation)
        receipt, created = self.ledger.reserve(
            identity,
            invocation_id=invocation.invocation_id,
        )
        if not created:
            replay = self._existing(receipt, invocation)
            if replay is not None:
                return replay
            raise AgentToolGatewayError(
                "Agent operation cannot be executed from its current state",
                code="AGENT.OPERATION.STATE_CONFLICT",
            )

        receipt = self.ledger.mark_running(
            receipt.operation_key,
            owner=invocation.owner,
            invocation_id=invocation.invocation_id,
        )
        try:
            result = self.gateway.invoke(token, invocation)
        except AgentToolGatewayError as exc:
            self.ledger.fail(
                receipt.operation_key,
                owner=invocation.owner,
                invocation_id=invocation.invocation_id,
                error_code=exc.code,
            )
            raise
        except Exception:
            # The process observed an unclassified failure after entering the
            # execution window.  A side effect may already exist; never invite
            # a blind retry by calling the operation failed.
            self.ledger.mark_unknown(
                receipt.operation_key,
                owner=invocation.owner,
            )
            raise

        if result.error is not None or result.result is None:
            # The current legacy Gateway raises on failures, so an error envelope
            # here is protocol drift.  Treat it as unknown rather than replaying
            # an envelope whose side-effect semantics are not established.
            self.ledger.mark_unknown(
                receipt.operation_key,
                owner=invocation.owner,
            )
            raise AgentToolGatewayError(
                "Agent operation returned an invalid authoritative result",
                code="AGENT.TOOL.INVALID_RESULT",
            )

        stored = self.results.put(
            operation_key=receipt.operation_key,
            owner=invocation.owner,
            result=result,
        )
        self.ledger.complete(
            receipt.operation_key,
            owner=invocation.owner,
            invocation_id=invocation.invocation_id,
            result_digest=stored.result_digest,
            result_ref=stored.result_ref,
            side_effect_receipt_ref=_side_effect_receipt_ref(result),
        )
        return result

    def _existing(
        self,
        receipt: AgentOperationReceiptRecord,
        invocation: ToolInvocation,
    ) -> ToolResult | None:
        if receipt.state is AgentOperationState.COMPLETED:
            if receipt.result_ref is None or receipt.result_digest is None:
                raise AgentToolGatewayError(
                    "Completed Agent operation has no replayable result",
                    code="AGENT.OPERATION.RESULT_UNAVAILABLE",
                )
            try:
                stored = self.results.get(receipt.result_ref, owner=invocation.owner)
            except KeyError:
                raise AgentToolGatewayError(
                    "Completed Agent operation result is unavailable",
                    code="AGENT.OPERATION.RESULT_UNAVAILABLE",
                ) from None
            if (
                stored.operation_key != receipt.operation_key
                or stored.result_digest != receipt.result_digest
            ):
                raise AgentToolGatewayError(
                    "Completed Agent operation result does not match its receipt",
                    code="AGENT.OPERATION.RESULT_MISMATCH",
                )
            return replay_tool_result(stored, invocation_id=invocation.invocation_id)

        if receipt.state is AgentOperationState.FAILED:
            raise AgentToolGatewayError(
                "Durable Agent operation previously failed",
                code=receipt.error_code or "AGENT.TOOL.READ_FAILED",
            )

        if receipt.state in {AgentOperationState.RESERVED, AgentOperationState.RUNNING}:
            raise AgentToolGatewayError(
                "Durable Agent operation is already in progress",
                code="AGENT.OPERATION.IN_PROGRESS",
                retryable=True,
            )

        if receipt.state in {
            AgentOperationState.STALE,
            AgentOperationState.RECONCILING,
            AgentOperationState.UNKNOWN,
        }:
            raise AgentToolGatewayError(
                "Durable Agent operation requires reconciliation",
                code="AGENT.OPERATION.RECONCILIATION_REQUIRED",
                retryable=True,
            )

        return None


def _side_effect_receipt_ref(result: ToolResult) -> str | None:
    """Extract an explicit domain receipt only when the handler actually returned one."""

    payload = result.result
    if payload is None:
        return None
    for field in (
        "operation_receipt_ref",
        "side_effect_receipt_ref",
        "submission_receipt_ref",
        "receipt_ref",
    ):
        value = payload.get(field)
        if isinstance(value, str) and value:
            return value
    return None
