"""Authoritative reservation, authorization, and budgets for Agent read tools."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pilot107.agent.capabilities import (
    AgentCapabilityClaims,
    AgentCapabilityError,
    AgentCapabilitySigner,
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
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store
        self.signer = signer
        self.handlers = dict(handlers)
        self.profile_handlers = {
            profile: dict(values) for profile, values in (profile_handlers or {}).items()
        }
        self._clock = clock or (lambda: datetime.now(UTC))

    def invoke(self, token: str, invocation: ToolInvocation) -> ToolResult:
        claims = self._verify(token)
        self._validate_binding(claims, invocation)
        arguments_digest = hashlib.sha256(_canonical(invocation.arguments)).hexdigest()
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
        if invocation.tool_name == "sandbox_exec" and usage.commands > claims.max_commands:
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
        try:
            read_result = handler(invocation.owner, invocation.arguments)
        except AgentToolGatewayError as exc:
            self._persist_failure(
                invocation, claims, code=exc.code, message=str(exc), retryable=exc.retryable
            )
            raise
        except Exception:
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
        stored_result = {
            "result": read_result.result,
            "evidence_refs": list(read_result.evidence_refs),
        }
        self.store.finish_tool_invocation(
            invocation_id=invocation.invocation_id,
            owner=invocation.owner,
            expected_state_version=invocation.state_version,
            expected_fencing_token=claims.fencing_token,
            result=stored_result,
            error=None,
            bytes_returned=bytes_returned,
        )
        return ToolResult(
            schema_version=TOOL_RESULT_PROTOCOL_VERSION,
            invocation_id=invocation.invocation_id,
            result=read_result.result,
            error=None,
            evidence_refs=read_result.evidence_refs,
            bytes_returned=bytes_returned,
        )

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
            required_operation = {
                "project_get": "read",
                "project_blueprint_save": "write",
                "workspace_list": "read",
                "workspace_read": "read",
                "workspace_diff": "read",
                "workspace_patch": "write",
                "sandbox_exec": "validate",
                "validation_schedule": "validate",
            }.get(invocation.tool_name)
            project_id = invocation.arguments.get("project_id")
            workspace_id = invocation.arguments.get("workspace_id")
            if (
                required_operation is None
                or required_operation not in claims.operations
                or project_id != claims.project_id
                or workspace_id != claims.workspace_id
                or (
                    invocation.tool_name == "validation_schedule"
                    and (
                        invocation.arguments.get("session_id") != invocation.session_id
                        or invocation.arguments.get("turn_id") != invocation.turn_id
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
    ) -> None:
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
