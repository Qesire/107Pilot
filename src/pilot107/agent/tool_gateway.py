"""Authoritative reservation, authorization, and budgets for Agent tools."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, NoReturn

from pilot107.agent.capabilities import (
    AgentCapabilityClaims,
    AgentCapabilityError,
    AgentCapabilitySigner,
)
from pilot107.agent.operation_ledger import (
    AgentOperationConflict,
    AgentOperationIntent,
    AgentOperationLedger,
    AgentOperationRecord,
    AgentOperationState,
    build_agent_operation_ledger,
    operation_intent_for_invocation,
)
from pilot107.agent.operation_reconciler import (
    AgentOperationReconciler,
    build_agent_operation_reconciler,
)
from pilot107.agent.project import is_project_agent_profile
from pilot107.agent.protocol import TOOL_RESULT_PROTOCOL_VERSION, ToolInvocation, ToolResult
from pilot107.agent.session import AgentSessionConflict
from pilot107.agent.store import AgentSessionStore


class AgentToolGatewayError(RuntimeError):
    """Stable, redacted Tool Gateway failure suitable for API mapping."""

    def __init__(self, message: str, *, code: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class AgentReadResult:
    result: dict[str, Any]
    evidence_refs: tuple[str, ...]


type AgentReadHandler = Callable[[str, Mapping[str, object]], AgentReadResult]


class AgentToolGateway:
    def __init__(
        self,
        *,
        store: AgentSessionStore,
        signer: AgentCapabilitySigner,
        handlers: Mapping[str, AgentReadHandler],
        profile_handlers: Mapping[str, Mapping[str, AgentReadHandler]] | None = None,
        operation_ledger: AgentOperationLedger | None = None,
        operation_reconciler: AgentOperationReconciler | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store
        self.signer = signer
        self.handlers = dict(handlers)
        self.profile_handlers = {
            profile: dict(values) for profile, values in (profile_handlers or {}).items()
        }
        self._clock = clock or (lambda: datetime.now(UTC))
        self.operation_ledger = operation_ledger or build_agent_operation_ledger(
            store,
            clock=self._clock,
        )
        self.operation_reconciler = operation_reconciler or build_agent_operation_reconciler(
            store,
            self.operation_ledger,
            clock=self._clock,
        )

    def invoke(self, token: str, invocation: ToolInvocation) -> ToolResult:
        claims = self._verify(token)
        self._validate_binding(claims, invocation)
        arguments_digest = hashlib.sha256(_canonical(invocation.arguments)).hexdigest()
        operation_arguments_digest = _semantic_operation_digest(invocation)
        try:
            reserved, created = self.store.reserve_tool_invocation(
                invocation_id=invocation.invocation_id,
                idempotency_key=invocation.idempotency_key,
                owner=invocation.owner,
                session_id=invocation.session_id,
                turn_id=invocation.turn_id,
                expected_state_version=invocation.state_version,
                expected_fencing_token=claims.fencing_token,
                tool_name=invocation.tool_name,
                arguments_digest=arguments_digest,
            )
        except AgentSessionConflict as exc:
            self._raise_store_conflict(claims, invocation, exc)

        if not created:
            return self._replay(reserved, invocation.invocation_id)

        usage = self.store.get_turn_tool_usage(
            turn_id=invocation.turn_id,
            owner=invocation.owner,
            expected_state_version=invocation.state_version,
            expected_fencing_token=claims.fencing_token,
        )
        if usage.invocations > claims.max_invocations:
            self._persist_failure(
                invocation,
                claims,
                code="AGENT.TOOL.INVOCATION_BUDGET_EXCEEDED",
                message="Agent tool invocation budget exceeded",
            )
            raise AgentToolGatewayError(
                "Agent tool invocation budget exceeded",
                code="AGENT.TOOL.INVOCATION_BUDGET_EXCEEDED",
            )
        if invocation.tool_name in {"sandbox_exec", "builder_build_submit"} and (
            usage.commands > claims.max_commands
        ):
            self._persist_failure(
                invocation,
                claims,
                code="AGENT.TOOL.COMMAND_BUDGET_EXCEEDED",
                message="Agent sandbox command budget exceeded",
            )
            raise AgentToolGatewayError(
                "Agent sandbox command budget exceeded",
                code="AGENT.TOOL.COMMAND_BUDGET_EXCEEDED",
            )

        handler = self.profile_handlers.get(claims.profile_id, {}).get(
            invocation.tool_name
        ) or self.handlers.get(invocation.tool_name)
        if handler is None:
            self._persist_failure(
                invocation,
                claims,
                code="AGENT.TOOL.UNAVAILABLE",
                message="Agent tool is unavailable",
            )
            raise AgentToolGatewayError(
                "Agent tool is unavailable", code="AGENT.TOOL.UNAVAILABLE"
            )

        operation_intent = self._operation_intent(
            invocation,
            claims,
            operation_arguments_digest,
        )
        if operation_intent is not None:
            replay = self._reserve_or_replay_operation(
                operation_intent,
                invocation,
                claims,
                usage_bytes=usage.bytes_returned,
            )
            if replay is not None:
                return replay

        try:
            read_result = handler(invocation.owner, invocation.arguments)
        except AgentToolGatewayError as exc:
            if operation_intent is not None:
                self._fail_operation(operation_intent, invocation, claims, exc)
            self._persist_failure(
                invocation, claims, code=exc.code, message=str(exc), retryable=exc.retryable
            )
            raise
        except Exception:
            if operation_intent is not None:
                self._unknown_operation(
                    operation_intent,
                    invocation,
                    claims,
                    code="AGENT.TOOL.OPERATION_UNKNOWN",
                    message="Agent mutation outcome requires reconciliation",
                )
                self._persist_failure(
                    invocation,
                    claims,
                    code="AGENT.TOOL.OPERATION_UNKNOWN",
                    message="Agent mutation outcome requires reconciliation",
                    retryable=False,
                )
                raise AgentToolGatewayError(
                    "Agent mutation outcome requires reconciliation",
                    code="AGENT.TOOL.OPERATION_UNKNOWN",
                ) from None
            self._persist_failure(
                invocation,
                claims,
                code="AGENT.TOOL.READ_FAILED",
                message="Agent read tool failed",
            )
            raise AgentToolGatewayError(
                "Agent read tool failed", code="AGENT.TOOL.READ_FAILED"
            ) from None
        if not isinstance(read_result, AgentReadResult):
            if operation_intent is not None:
                self._unknown_operation(
                    operation_intent,
                    invocation,
                    claims,
                    code="AGENT.TOOL.INVALID_RESULT",
                    message="Mutation completed without a valid ToolResult",
                )
                self._persist_failure(
                    invocation,
                    claims,
                    code="AGENT.TOOL.OPERATION_UNKNOWN",
                    message="Agent mutation outcome requires reconciliation",
                )
                raise AgentToolGatewayError(
                    "Agent mutation outcome requires reconciliation",
                    code="AGENT.TOOL.OPERATION_UNKNOWN",
                )
            self._persist_failure(
                invocation,
                claims,
                code="AGENT.TOOL.INVALID_RESULT",
                message="Agent read tool returned an invalid result",
            )
            raise AgentToolGatewayError(
                "Agent read tool returned an invalid result",
                code="AGENT.TOOL.INVALID_RESULT",
            )
        bytes_returned = len(_canonical(read_result.result))
        stored_result = {
            "result": read_result.result,
            "evidence_refs": list(read_result.evidence_refs),
        }
        if operation_intent is not None:
            operation_result = {
                **stored_result,
                "bytes_returned": bytes_returned,
            }
            try:
                assert self.operation_ledger is not None
                self.operation_ledger.complete(
                    operation_intent.operation_key,
                    owner=invocation.owner,
                    expected_state_version=invocation.state_version,
                    expected_fencing_token=claims.fencing_token,
                    result=operation_result,
                    side_effect_ref=(
                        read_result.evidence_refs[0] if read_result.evidence_refs else None
                    ),
                )
            except AgentOperationConflict:
                raise AgentToolGatewayError(
                    "Agent mutation receipt could not be committed under the current fence",
                    code="AGENT.TOOL.FENCED",
                ) from None
        if usage.bytes_returned + bytes_returned > claims.max_bytes:
            self._persist_failure(
                invocation,
                claims,
                code="AGENT.TOOL.BYTE_BUDGET_EXCEEDED",
                message="Agent tool byte budget exceeded",
            )
            raise AgentToolGatewayError(
                "Agent tool byte budget exceeded",
                code="AGENT.TOOL.BYTE_BUDGET_EXCEEDED",
            )
        try:
            self.store.finish_tool_invocation(
                invocation_id=invocation.invocation_id,
                owner=invocation.owner,
                expected_state_version=invocation.state_version,
                expected_fencing_token=claims.fencing_token,
                result=stored_result,
                error=None,
                bytes_returned=bytes_returned,
            )
        except AgentSessionConflict:
            if operation_intent is not None:
                raise AgentToolGatewayError(
                    "Agent mutation receipt is durable but the Turn is stale or fenced",
                    code="AGENT.TOOL.FENCED",
                ) from None
            raise
        return ToolResult(
            schema_version=TOOL_RESULT_PROTOCOL_VERSION,
            invocation_id=invocation.invocation_id,
            result=read_result.result,
            error=None,
            evidence_refs=read_result.evidence_refs,
            bytes_returned=bytes_returned,
        )

    def _operation_intent(
        self,
        invocation: ToolInvocation,
        claims: AgentCapabilityClaims,
        arguments_digest: str,
    ) -> AgentOperationIntent | None:
        if self.operation_ledger is None:
            return None
        try:
            return operation_intent_for_invocation(
                self.store,
                invocation,
                arguments_digest=arguments_digest,
            )
        except AgentOperationConflict:
            self._raise_operation_conflict(claims, invocation)

    def _reserve_or_replay_operation(
        self,
        intent: AgentOperationIntent,
        invocation: ToolInvocation,
        claims: AgentCapabilityClaims,
        *,
        usage_bytes: int,
    ) -> ToolResult | None:
        assert self.operation_ledger is not None
        try:
            record, _ = self.operation_ledger.reserve(
                intent,
                invocation_id=invocation.invocation_id,
                expected_state_version=invocation.state_version,
                expected_fencing_token=claims.fencing_token,
            )
        except AgentOperationConflict:
            self._raise_operation_conflict(claims, invocation)
        if record.state is AgentOperationState.COMPLETED:
            return self._replay_completed_operation(
                record,
                invocation,
                claims,
                usage_bytes=usage_bytes,
            )
        if record.state is AgentOperationState.FAILED:
            self._replay_failed_operation(record, invocation, claims)
        if self.operation_reconciler is not None and (
            record.state in {AgentOperationState.UNKNOWN, AgentOperationState.STALE}
            or (
                record.state is AgentOperationState.RUNNING
                and record.origin_turn_id != invocation.turn_id
            )
        ):
            try:
                reconciled = self.operation_reconciler.reconcile(
                    record,
                    invocation=invocation,
                    expected_fencing_token=claims.fencing_token,
                )
            except Exception:
                # Recovery is fail-closed: a reconciler outage must never cause
                # the mutation handler to be executed again.
                reconciled = None
            if reconciled is not None and reconciled.state is AgentOperationState.COMPLETED:
                return self._replay_completed_operation(
                    reconciled,
                    invocation,
                    claims,
                    usage_bytes=usage_bytes,
                )
            record = self.operation_ledger.get(intent.operation_key, owner=invocation.owner)
        if record.state is AgentOperationState.RUNNING:
            self._persist_failure(
                invocation,
                claims,
                code="AGENT.TOOL.OPERATION_IN_PROGRESS",
                message="Agent mutation is already in progress",
                retryable=True,
            )
            raise AgentToolGatewayError(
                "Agent mutation is already in progress",
                code="AGENT.TOOL.OPERATION_IN_PROGRESS",
                retryable=True,
            )
        if record.state in {
            AgentOperationState.STALE,
            AgentOperationState.RECONCILING,
            AgentOperationState.UNKNOWN,
        }:
            self._persist_failure(
                invocation,
                claims,
                code="AGENT.TOOL.OPERATION_UNKNOWN",
                message="Agent mutation outcome requires reconciliation",
                retryable=record.state is not AgentOperationState.UNKNOWN,
            )
            raise AgentToolGatewayError(
                "Agent mutation outcome requires reconciliation",
                code="AGENT.TOOL.OPERATION_UNKNOWN",
                retryable=record.state is not AgentOperationState.UNKNOWN,
            )
        try:
            self.operation_ledger.start(
                intent.operation_key,
                owner=invocation.owner,
                invocation_id=invocation.invocation_id,
                expected_state_version=invocation.state_version,
                expected_fencing_token=claims.fencing_token,
            )
        except AgentOperationConflict:
            current = self.operation_ledger.get(intent.operation_key, owner=invocation.owner)
            if current.state is AgentOperationState.COMPLETED:
                return self._replay_completed_operation(
                    current,
                    invocation,
                    claims,
                    usage_bytes=usage_bytes,
                )
            if current.state is AgentOperationState.FAILED:
                self._replay_failed_operation(current, invocation, claims)
            self._persist_failure(
                invocation,
                claims,
                code="AGENT.TOOL.OPERATION_IN_PROGRESS",
                message="Agent mutation is already in progress or requires reconciliation",
                retryable=True,
            )
            raise AgentToolGatewayError(
                "Agent mutation is already in progress or requires reconciliation",
                code="AGENT.TOOL.OPERATION_IN_PROGRESS",
                retryable=True,
            ) from None
        return None

    def _replay_completed_operation(
        self,
        record: AgentOperationRecord,
        invocation: ToolInvocation,
        claims: AgentCapabilityClaims,
        *,
        usage_bytes: int,
    ) -> ToolResult:
        stored = record.result
        if (
            not isinstance(stored, dict)
            or not isinstance(stored.get("result"), dict)
            or not isinstance(stored.get("evidence_refs"), list)
            or not all(isinstance(item, str) for item in stored["evidence_refs"])
            or isinstance(stored.get("bytes_returned"), bool)
            or not isinstance(stored.get("bytes_returned"), int)
            or stored["bytes_returned"] < 0
        ):
            raise AgentToolGatewayError(
                "Stored Agent operation receipt is invalid",
                code="AGENT.TOOL.INVALID_RESULT",
            )
        bytes_returned = stored["bytes_returned"]
        if usage_bytes + bytes_returned > claims.max_bytes:
            self._persist_failure(
                invocation,
                claims,
                code="AGENT.TOOL.BYTE_BUDGET_EXCEEDED",
                message="Agent tool byte budget exceeded",
            )
            raise AgentToolGatewayError(
                "Agent tool byte budget exceeded",
                code="AGENT.TOOL.BYTE_BUDGET_EXCEEDED",
            )
        invocation_result = {
            "result": stored["result"],
            "evidence_refs": stored["evidence_refs"],
        }
        try:
            self.store.finish_tool_invocation(
                invocation_id=invocation.invocation_id,
                owner=invocation.owner,
                expected_state_version=invocation.state_version,
                expected_fencing_token=claims.fencing_token,
                result=invocation_result,
                error=None,
                bytes_returned=bytes_returned,
            )
        except AgentSessionConflict:
            raise AgentToolGatewayError(
                "Agent mutation receipt is durable but the Turn is stale or fenced",
                code="AGENT.TOOL.FENCED",
            ) from None
        return ToolResult(
            schema_version=TOOL_RESULT_PROTOCOL_VERSION,
            invocation_id=invocation.invocation_id,
            result=stored["result"],
            error=None,
            evidence_refs=tuple(stored["evidence_refs"]),
            bytes_returned=bytes_returned,
        )

    def _replay_failed_operation(
        self,
        record: AgentOperationRecord,
        invocation: ToolInvocation,
        claims: AgentCapabilityClaims,
    ) -> NoReturn:
        error = record.error or {
            "code": "AGENT.TOOL.READ_FAILED",
            "message": "Agent mutation failed",
            "retryable": False,
        }
        code = str(error.get("code", "AGENT.TOOL.READ_FAILED"))
        message = str(error.get("message", "Agent mutation failed"))
        retryable = bool(error.get("retryable", False))
        self._persist_failure(
            invocation,
            claims,
            code=code,
            message=message,
            retryable=retryable,
        )
        raise AgentToolGatewayError(message, code=code, retryable=retryable)

    def _fail_operation(
        self,
        intent: AgentOperationIntent,
        invocation: ToolInvocation,
        claims: AgentCapabilityClaims,
        exc: AgentToolGatewayError,
    ) -> None:
        assert self.operation_ledger is not None
        try:
            self.operation_ledger.fail(
                intent.operation_key,
                owner=invocation.owner,
                expected_state_version=invocation.state_version,
                expected_fencing_token=claims.fencing_token,
                error={
                    "code": exc.code,
                    "message": str(exc),
                    "retryable": exc.retryable,
                },
            )
        except AgentOperationConflict:
            raise AgentToolGatewayError(
                "Agent mutation failure could not be committed under the current fence",
                code="AGENT.TOOL.FENCED",
            ) from None

    def _unknown_operation(
        self,
        intent: AgentOperationIntent,
        invocation: ToolInvocation,
        claims: AgentCapabilityClaims,
        *,
        code: str,
        message: str,
    ) -> None:
        assert self.operation_ledger is not None
        try:
            self.operation_ledger.mark_unknown(
                intent.operation_key,
                owner=invocation.owner,
                expected_state_version=invocation.state_version,
                expected_fencing_token=claims.fencing_token,
                error={"code": code, "message": message, "retryable": False},
            )
        except AgentOperationConflict:
            # A running record is safer than guessing a terminal outcome. A
            # reconciler must resolve it before any later execution can proceed.
            pass

    def _verify(self, token: str) -> AgentCapabilityClaims:
        try:
            return self.signer.verify(token)
        except AgentCapabilityError as exc:
            raise AgentToolGatewayError(str(exc), code=exc.code) from None

    def _validate_binding(
        self, claims: AgentCapabilityClaims, invocation: ToolInvocation
    ) -> None:
        if (
            invocation.owner != claims.owner
            or invocation.session_id != claims.session_id
            or invocation.turn_id != claims.turn_id
            or invocation.state_version != claims.state_version
            or invocation.profile_id != claims.profile_id
            or invocation.tool_name not in claims.tools
        ):
            raise AgentToolGatewayError(
                "Agent tool invocation is not authorized",
                code="AGENT.TOOL.UNAUTHORIZED",
            )
        if is_project_agent_profile(claims.profile_id):
            required_operations = {
                "project_get": frozenset({"read"}),
                "project_blueprint_save": frozenset({"write"}),
                "workspace_list": frozenset({"read"}),
                "workspace_read": frozenset({"read"}),
                "workspace_diff": frozenset({"read"}),
                "workspace_patch": frozenset({"write"}),
                "sandbox_exec": frozenset({"validate"}),
                "validation_schedule": frozenset({"validate"}),
                "builder_context_get": frozenset({"read"}),
                "builder_build_submit": frozenset({"write", "validate"}),
            }.get(invocation.tool_name)
            project_id = invocation.arguments.get("project_id")
            workspace_id = invocation.arguments.get("workspace_id")
            if (
                required_operations is None
                or not required_operations.issubset(claims.operations)
                or project_id != claims.project_id
                or workspace_id != claims.workspace_id
                or (
                    invocation.tool_name
                    in {
                        "validation_schedule",
                        "builder_context_get",
                        "builder_build_submit",
                    }
                    and (
                        invocation.arguments.get("session_id") != invocation.session_id
                        or (
                            invocation.tool_name
                            in {"validation_schedule", "builder_build_submit"}
                            and invocation.arguments.get("turn_id") != invocation.turn_id
                        )
                    )
                )
            ):
                raise AgentToolGatewayError(
                    "Agent tool invocation exceeds its Project capability",
                    code="AGENT.TOOL.CAPABILITY_DENIED",
                )
        now = self._now()
        try:
            deadline = datetime.fromisoformat(invocation.deadline.replace("Z", "+00:00"))
        except ValueError:
            raise AgentToolGatewayError(
                "Agent tool invocation deadline is invalid",
                code="AGENT.TOOL.INVALID",
            ) from None
        if deadline.tzinfo is None or deadline.astimezone(UTC) < now:
            raise AgentToolGatewayError(
                "Agent tool invocation deadline has expired",
                code="AGENT.TOOL.DEADLINE_EXPIRED",
            )

    def _raise_store_conflict(
        self,
        claims: AgentCapabilityClaims,
        invocation: ToolInvocation,
        cause: AgentSessionConflict,
    ) -> NoReturn:
        del cause
        try:
            self.store.get_turn_tool_usage(
                turn_id=invocation.turn_id,
                owner=invocation.owner,
                expected_state_version=invocation.state_version,
                expected_fencing_token=claims.fencing_token,
            )
        except AgentSessionConflict:
            raise AgentToolGatewayError(
                "Agent Turn capability is stale or fenced", code="AGENT.TOOL.FENCED"
            ) from None
        raise AgentToolGatewayError(
            "Agent tool idempotency key conflicts with different content",
            code="AGENT.TOOL.IDEMPOTENCY_CONFLICT",
        ) from None

    def _raise_operation_conflict(
        self,
        claims: AgentCapabilityClaims,
        invocation: ToolInvocation,
    ) -> NoReturn:
        try:
            self.store.get_turn_tool_usage(
                turn_id=invocation.turn_id,
                owner=invocation.owner,
                expected_state_version=invocation.state_version,
                expected_fencing_token=claims.fencing_token,
            )
        except AgentSessionConflict:
            raise AgentToolGatewayError(
                "Agent Turn capability is stale or fenced", code="AGENT.TOOL.FENCED"
            ) from None
        self._persist_failure(
            invocation,
            claims,
            code="AGENT.TOOL.OPERATION_CONFLICT",
            message="Agent durable operation identity conflicts with different content",
        )
        raise AgentToolGatewayError(
            "Agent durable operation identity conflicts with different content",
            code="AGENT.TOOL.OPERATION_CONFLICT",
        ) from None

    def _replay(self, record: Any, invocation_id: str) -> ToolResult:
        if record.state == "running":
            raise AgentToolGatewayError(
                "Agent tool invocation is already in progress",
                code="AGENT.TOOL.INVOCATION_IN_PROGRESS",
                retryable=True,
            )
        if record.state == "failed":
            error = record.error or {
                "code": "AGENT.TOOL.READ_FAILED",
                "message": "Agent read tool failed",
                "retryable": False,
            }
            raise AgentToolGatewayError(
                str(error.get("message", "Agent read tool failed")),
                code=str(error.get("code", "AGENT.TOOL.READ_FAILED")),
                retryable=bool(error.get("retryable", False)),
            )
        stored = record.result
        if (
            not isinstance(stored, dict)
            or not isinstance(stored.get("result"), dict)
            or not isinstance(stored.get("evidence_refs"), list)
            or not all(isinstance(item, str) for item in stored["evidence_refs"])
        ):
            raise AgentToolGatewayError(
                "Stored Agent tool result is invalid", code="AGENT.TOOL.INVALID_RESULT"
            )
        return ToolResult(
            schema_version=TOOL_RESULT_PROTOCOL_VERSION,
            invocation_id=invocation_id,
            result=stored["result"],
            error=None,
            evidence_refs=tuple(stored["evidence_refs"]),
            bytes_returned=record.bytes_returned,
        )

    def _persist_failure(
        self,
        invocation: ToolInvocation,
        claims: AgentCapabilityClaims,
        *,
        code: str,
        message: str,
        retryable: bool = False,
    ) -> None:
        self.store.finish_tool_invocation(
            invocation_id=invocation.invocation_id,
            owner=invocation.owner,
            expected_state_version=invocation.state_version,
            expected_fencing_token=claims.fencing_token,
            result=None,
            error={"code": code, "message": message, "retryable": retryable},
            bytes_returned=0,
        )

    def _now(self) -> datetime:
        current = self._clock()
        if current.tzinfo is None:
            raise ValueError("Tool Gateway clock must be timezone-aware")
        return current.astimezone(UTC)


def _semantic_operation_digest(invocation: ToolInvocation) -> str:
    # ``turn_id`` is an execution-carrier identity. A durable domain request
    # that resumes in a later Turn must still compare the same mutation intent.
    arguments = dict(invocation.arguments)
    arguments.pop("turn_id", None)
    return hashlib.sha256(_canonical(arguments)).hexdigest()


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise AgentToolGatewayError(
            "Agent tool payload is invalid", code="AGENT.TOOL.INVALID"
        ) from exc
