"""Compatibility wrapper that shadows Tool Gateway calls into the operation ledger.

This is deliberately not authoritative replay yet.  The existing Tool Gateway
continues to decide authorization, execution, and ToolResult replay.  The
wrapper establishes the provider-call-independent operation identity and records
what the legacy path observed, so crash/reconciliation semantics can be proven
before the ledger is allowed to suppress a mutation.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Protocol

from pilot107.agent.operation_ledger import (
    AgentOperationReceiptRecord,
    DurableOperationIdentity,
    durable_operation_identity,
)
from pilot107.agent.protocol import ToolInvocation, ToolResult
from pilot107.agent.tool_gateway import AgentToolGateway, AgentToolGatewayError


class AgentOperationLedger(Protocol):
    def reserve(
        self,
        identity: DurableOperationIdentity,
        *,
        invocation_id: str,
    ) -> tuple[AgentOperationReceiptRecord, bool]: ...

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


class OperationLedgerShadowGateway:
    """Record legacy Gateway outcomes without changing their replay authority."""

    def __init__(self, *, gateway: AgentToolGateway, ledger: AgentOperationLedger) -> None:
        self.gateway = gateway
        self.ledger = ledger

    def invoke(self, token: str, invocation: ToolInvocation) -> ToolResult:
        identity = operation_identity_for_invocation(invocation)
        receipt, created = self.ledger.reserve(
            identity,
            invocation_id=invocation.invocation_id,
        )
        if created:
            receipt = self.ledger.mark_running(
                receipt.operation_key,
                owner=invocation.owner,
                invocation_id=invocation.invocation_id,
            )

        try:
            result = self.gateway.invoke(token, invocation)
        except AgentToolGatewayError as exc:
            # A Gateway rejection has a stable public error code and is safe to
            # record as terminal failure without inventing a side-effect fact.
            self.ledger.fail(
                receipt.operation_key,
                owner=invocation.owner,
                invocation_id=invocation.invocation_id,
                error_code=exc.code,
            )
            raise
        except Exception:
            # An unexpected exception may have happened after an external side
            # effect.  The shadow ledger must not call it "failed" and invite a
            # blind retry; it records UNKNOWN for a later domain reconciler.
            self.ledger.mark_unknown(
                receipt.operation_key,
                owner=invocation.owner,
            )
            raise

        if result.error is not None:
            self.ledger.fail(
                receipt.operation_key,
                owner=invocation.owner,
                invocation_id=invocation.invocation_id,
                error_code=str(result.error.get("code") or "AGENT.TOOL.ERROR"),
            )
            return result

        self.ledger.complete(
            receipt.operation_key,
            owner=invocation.owner,
            invocation_id=invocation.invocation_id,
            result_digest=tool_result_digest(result),
            result_ref=None,
            side_effect_receipt_ref=None,
        )
        return result


def operation_identity_for_invocation(
    invocation: ToolInvocation,
) -> DurableOperationIdentity:
    # The legacy agentd idempotency key is derived from turn_id + toolCallId and
    # therefore changes when a provider emits a new tool-call ID.  Domain tools
    # that expose a request_key get that stronger identity; all other calls are
    # bounded to their durable Turn rather than to the provider call ID.
    request_key = _argument_string(invocation.arguments, "request_key") or invocation.turn_id
    target_type, target_id = _target(invocation.arguments)
    target_revision = (
        _argument_scalar(invocation.arguments, "expected_revision")
        or _argument_scalar(invocation.arguments, "workspace_revision")
        or _argument_scalar(invocation.arguments, "revision")
        or _argument_scalar(invocation.arguments, "expected_version")
    )
    return durable_operation_identity(
        owner=invocation.owner,
        session_id=invocation.session_id,
        tool_name=invocation.tool_name,
        arguments=invocation.arguments,
        user_request_key=request_key,
        target_type=target_type,
        target_id=target_id,
        target_revision=target_revision,
    )


def tool_result_digest(result: ToolResult) -> str:
    payload = {
        "result": result.result,
        "error": result.error,
        "evidence_refs": list(result.evidence_refs),
        "bytes_returned": result.bytes_returned,
    }
    return "sha256:" + hashlib.sha256(_canonical(payload)).hexdigest()


def _target(arguments: Mapping[str, object]) -> tuple[str | None, str | None]:
    for field, target_type in (
        ("workspace_id", "agent_workspace"),
        ("run_id", "run"),
        ("project_id", "agent_project"),
        ("contract_id", "contract"),
        ("object_id", "evidence_object"),
    ):
        value = _argument_string(arguments, field)
        if value is not None:
            return target_type, value
    return None, None


def _argument_string(arguments: Mapping[str, object], field: str) -> str | None:
    value = arguments.get(field)
    if isinstance(value, str) and value:
        return value
    return None


def _argument_scalar(arguments: Mapping[str, object], field: str) -> str | None:
    value = arguments.get(field)
    if isinstance(value, str) and value:
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return None


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
