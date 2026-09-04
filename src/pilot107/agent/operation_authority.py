"""Durable operation authority for Agent mutation tools.

The model/Pi loop is intentionally untouched.  The production integration is a
small controller used by :class:`AgentToolGateway` at two precise boundaries:

1. after capability binding, but before legacy provider-call reservation, it may
   replay an already terminal operation without consulting provider call IDs;
2. after all legacy preconditions pass and immediately before the real mutation
   handler, it reserves and starts the durable operation.

This prevents authorization, budget, or handler-availability failures from
poisoning a durable receipt while still closing the provider-call-ID replay gap.
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

# Durable replay protects persisted mutations and cluster scheduling.  Ordinary
# reads remain live.  ``sandbox_exec`` is deliberately excluded because it is an
# ephemeral validation step rather than a persisted domain mutation.
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


class DurableOperationController:
    """Provider-call-independent authority used at the real handler boundary."""

    def __init__(
        self,
        *,
        ledger: AuthoritativeOperationLedger,
        results: AuthoritativeOperationResultStore,
        protected_tools: frozenset[str] = DURABLE_MUTATION_TOOL_NAMES,
    ) -> None:
        self.ledger = ledger
        self.results = results
        self.protected_tools = protected_tools

    def protects(self, tool_name: str) -> bool:
        return tool_name in self.protected_tools

    def replay_existing(self, invocation: ToolInvocation) -> ToolResult | None:
        """Read existing authority without creating a receipt.

        This method is safe to call immediately after capability validation.  A
        completed receipt can bypass the legacy ``invocation_id`` conflict; an
        absent receipt leaves all old preconditions untouched.
        """

        if not self.protects(invocation.tool_name):
            return None
        identity = operation_identity_for_invocation(invocation)
        try:
            receipt = self.ledger.get(identity.operation_key, owner=invocation.owner)
        except KeyError:
            return None
        return self._existing(receipt, invocation)

    def reserve_or_replay(self, invocation: ToolInvocation) -> ToolResult | None:
        """Reserve at the last safe point before the mutation handler executes."""

        if not self.protects(invocation.tool_name):
            return None
        identity = operation_identity_for_invocation(invocation)
        receipt, created = self.ledger.reserve(
            identity,
            invocation_id=invocation.invocation_id,
        )
        if not created:
            return self._existing(receipt, invocation)
        self.ledger.mark_running(
            receipt.operation_key,
            owner=invocation.owner,
            invocation_id=invocation.invocation_id,
        )
        return None

    def complete(self, invocation: ToolInvocation, result: ToolResult) -> None:
        if not self.protects(invocation.tool_name):
            return
        identity = operation_identity_for_invocation(invocation)
        stored = self.results.put(
            operation_key=identity.operation_key,
            owner=invocation.owner,
            result=result,
        )
        self.ledger.complete(
            identity.operation_key,
            owner=invocation.owner,
            invocation_id=invocation.invocation_id,
            result_digest=stored.result_digest,
            result_ref=stored.result_ref,
            side_effect_receipt_ref=_side_effect_receipt_ref(result),
        )

    def fail(self, invocation: ToolInvocation, *, error_code: str) -> None:
        """Persist a stable handler-level failure after execution has started."""

        if not self.protects(invocation.tool_name):
            return
        identity = operation_identity_for_invocation(invocation)
        self.ledger.fail(
            identity.operation_key,
            owner=invocation.owner,
            invocation_id=invocation.invocation_id,
            error_code=error_code,
        )

    def mark_unknown(self, invocation: ToolInvocation) -> None:
        if not self.protects(invocation.tool_name):
            return
        identity = operation_identity_for_invocation(invocation)
        self.ledger.mark_unknown(identity.operation_key, owner=invocation.owner)

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

        raise AgentToolGatewayError(
            "Agent operation state does not permit execution",
            code="AGENT.OPERATION.STATE_CONFLICT",
        )


class OperationLedgerAuthoritativeGateway:
    """Compatibility adapter retained for focused controller tests only.

    Production routing no longer uses this outer wrapper because it cannot know
    whether a legacy Gateway error happened before or after the mutation handler.
    New integration must use :class:`DurableOperationController` inside
    ``AgentToolGateway``.
    """

    def __init__(
        self,
        *,
        gateway: AgentToolGateway,
        ledger: AuthoritativeOperationLedger,
        results: AuthoritativeOperationResultStore,
        protected_tools: frozenset[str] = DURABLE_MUTATION_TOOL_NAMES,
    ) -> None:
        self.gateway = gateway
        self.controller = DurableOperationController(
            ledger=ledger,
            results=results,
            protected_tools=protected_tools,
        )

    def invoke(self, token: str, invocation: ToolInvocation) -> ToolResult:
        if not self.controller.protects(invocation.tool_name):
            return self.gateway.invoke(token, invocation)
        replay = self.controller.replay_existing(invocation)
        if replay is not None:
            return replay
        replay = self.controller.reserve_or_replay(invocation)
        if replay is not None:
            return replay
        try:
            result = self.gateway.invoke(token, invocation)
        except AgentToolGatewayError as exc:
            self.controller.fail(invocation, error_code=exc.code)
            raise
        except Exception:
            self.controller.mark_unknown(invocation)
            raise
        if result.error is not None or result.result is None:
            self.controller.mark_unknown(invocation)
            raise AgentToolGatewayError(
                "Agent operation returned an invalid authoritative result",
                code="AGENT.TOOL.INVALID_RESULT",
            )
        self.controller.complete(invocation, result)
        return result


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
