"""Private HTTP boundary for one capability-authorized Agent tool invocation."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict

from pilot107.agent.operation_authority import DurableOperationController
from pilot107.agent.operation_ledger import SQLiteAgentOperationLedger
from pilot107.agent.operation_ledger_postgres import PostgresAgentOperationLedger
from pilot107.agent.operation_results import SQLiteAgentOperationResultStore
from pilot107.agent.operation_results_postgres import PostgresAgentOperationResultStore
from pilot107.agent.protocol import TOOL_RESULT_PROTOCOL_VERSION, parse_tool_invocation
from pilot107.agent.tool_gateway import AgentToolGateway, AgentToolGatewayError
from pilot107.api.http_types import ApiResponse
from pilot107.api.metrics import ControlPlaneMetrics

_MAX_BODY_BYTES = 1024 * 1024


class AgentToolRoutes:
    def __init__(
        self,
        gateway: AgentToolGateway,
        metrics: ControlPlaneMetrics | None = None,
        *,
        operation_authority_enabled: bool = True,
    ) -> None:
        if operation_authority_enabled:
            _enable_operation_authority(gateway)
        self.gateway = gateway
        self.metrics = metrics

    def handle_post(
        self,
        parts: list[str],
        *,
        body: bytes,
        headers: Mapping[str, str] | None,
    ) -> ApiResponse | None:
        if parts != ["internal", "v1", "agent-tools", "invoke"]:
            return None
        if len(body) > _MAX_BODY_BYTES:
            return _error(
                413,
                "AGENT.TOOL.REQUEST_TOO_LARGE",
                "Agent tool request exceeds 1 MiB",
            )
        authorization = _header(headers, "authorization")
        if authorization is None or not authorization.startswith("Bearer "):
            return _error(
                401,
                "AGENT.CAPABILITY.MISSING",
                "Bearer capability required",
            )
        token = authorization[7:]
        if not token or len(token) > 8_192 or token.strip() != token:
            return _error(401, "AGENT.CAPABILITY.INVALID", "Agent capability is invalid")
        content_type = _header(headers, "content-type")
        if content_type is not None and content_type.split(";", 1)[0].strip().lower() != (
            "application/json"
        ):
            return _error(415, "AGENT.TOOL.CONTENT_TYPE", "application/json required")
        try:
            value = json.loads(body.decode("utf-8"))
            invocation = parse_tool_invocation(value)
        except (json.JSONDecodeError, UnicodeError, ValueError):
            return _error(400, "AGENT.TOOL.INVALID", "Agent tool invocation is invalid")
        try:
            result = self.gateway.invoke(token, invocation)
        except AgentToolGatewayError as exc:
            self._observe_error(invocation.profile_id, invocation.tool_name, exc.code)
            return _gateway_error(exc, invocation_id=invocation.invocation_id)
        except Exception:
            self._observe_error(invocation.profile_id, invocation.tool_name, None)
            return _error(500, "AGENT.TOOL.INTERNAL", "Agent tool invocation failed")
        self._observe_success(invocation.profile_id, invocation.tool_name, result.result)
        payload = asdict(result)
        payload["evidence_refs"] = list(result.evidence_refs)
        return ApiResponse(status=200, payload=payload)

    def _observe_error(
        self,
        profile: str,
        tool: str,
        code: str | None,
    ) -> None:
        if self.metrics is None:
            return
        outcome = "no_progress" if code == "AGENT.BUILDER.NO_PROGRESS" else "error"
        self.metrics.observe_agent_tool(profile=profile, tool=tool, outcome=outcome)
        if tool == "builder_build_submit":
            self.metrics.observe_builder_submission(outcome=outcome, phase=None)

    def _observe_success(
        self,
        profile: str,
        tool: str,
        result: Mapping[str, object] | None,
    ) -> None:
        if self.metrics is None:
            return
        self.metrics.observe_agent_tool(profile=profile, tool=tool, outcome="success")
        if tool != "builder_build_submit" or result is None:
            return
        outcome = result.get("status")
        phase = result.get("phase")
        self.metrics.observe_builder_submission(
            outcome=outcome if isinstance(outcome, str) else "error",
            phase=phase if isinstance(phase, str) else None,
        )


def _enable_operation_authority(gateway: AgentToolGateway) -> None:
    """Bind the durable controller to the same database as AgentSessionStore."""

    store = getattr(gateway, "store", None)
    if store is None or not hasattr(gateway, "set_operation_controller"):
        return
    dsn = getattr(store, "dsn", None)
    if isinstance(dsn, str) and dsn:
        gateway.set_operation_controller(
            DurableOperationController(
                ledger=PostgresAgentOperationLedger(dsn),
                results=PostgresAgentOperationResultStore(dsn),
            )
        )
        return
    db_path = getattr(store, "db_path", None)
    if db_path is None:
        return
    gateway.set_operation_controller(
        DurableOperationController(
            ledger=SQLiteAgentOperationLedger(db_path),
            results=SQLiteAgentOperationResultStore(db_path),
        )
    )


def _gateway_error(
    exc: AgentToolGatewayError,
    *,
    invocation_id: str,
) -> ApiResponse:
    code = exc.code
    if code in {"AGENT.CAPABILITY.INVALID", "AGENT.CAPABILITY.EXPIRED"}:
        status = 401
    elif code in {
        "AGENT.TOOL.UNAUTHORIZED",
        "AGENT.TOOL.CAPABILITY_DENIED",
        "AGENT.TOOL.PATH_FORBIDDEN",
    }:
        status = 403
    elif code == "AGENT.TOOL.NOT_FOUND":
        status = 404
    elif code in {
        "AGENT.TOOL.FENCED",
        "AGENT.TOOL.IDEMPOTENCY_CONFLICT",
        "AGENT.TOOL.INVOCATION_IN_PROGRESS",
        "AGENT.OPERATION.IN_PROGRESS",
        "AGENT.OPERATION.RECONCILIATION_REQUIRED",
        "AGENT.OPERATION.STATE_CONFLICT",
        "AGENT.BUILDER.IDEMPOTENCY_CONFLICT",
        "AGENT.BUILDER.NO_PROGRESS",
        "AGENT.BUILDER.SNAPSHOT_INVALID",
    }:
        status = 409
    elif code in {
        "AGENT.BUILDER.BINDING_INVALID",
        "AGENT.TOOL.RESOURCE_ENVELOPE_EXCEEDED",
    }:
        status = 403
    elif code in {
        "AGENT.TOOL.INVOCATION_BUDGET_EXCEEDED",
        "AGENT.TOOL.BYTE_BUDGET_EXCEEDED",
        "AGENT.TOOL.COMMAND_BUDGET_EXCEEDED",
    }:
        status = 429
    elif code == "AGENT.TOOL.DEADLINE_EXPIRED":
        status = 408
    elif code in {"AGENT.TOOL.INVALID", "AGENT.TOOL.INVALID_RESULT"}:
        status = 400
    elif code == "AGENT.BUILDER.VALIDATIONS_INVALID":
        status = 400
    elif code in {
        "AGENT.TOOL.UNAVAILABLE",
        "AGENT.BUILDER.ENVELOPE_UNAVAILABLE",
        "AGENT.OPERATION.RESULT_UNAVAILABLE",
        "AGENT.OPERATION.RESULT_MISMATCH",
    }:
        status = 503
    else:
        status = 502
        code = "AGENT.TOOL.READ_FAILED"
    message = _public_message(code)
    return ApiResponse(
        status=status,
        payload={
            "schema_version": TOOL_RESULT_PROTOCOL_VERSION,
            "invocation_id": invocation_id,
            "result": None,
            "error": {
                "code": code,
                "message": message,
                "retryable": exc.retryable,
            },
            "evidence_refs": [],
            "bytes_returned": 0,
        },
    )


def _public_message(code: str) -> str:
    messages = {
        "AGENT.CAPABILITY.INVALID": "Agent capability is invalid",
        "AGENT.CAPABILITY.EXPIRED": "Agent capability has expired",
        "AGENT.TOOL.UNAUTHORIZED": "Agent tool invocation is not authorized",
        "AGENT.TOOL.CAPABILITY_DENIED": "Agent tool capability scope was denied",
        "AGENT.TOOL.PATH_FORBIDDEN": "Agent tool path is not authorized",
        "AGENT.TOOL.NOT_FOUND": "Agent tool resource was not found",
        "AGENT.TOOL.FENCED": "Agent Turn capability is stale or fenced",
        "AGENT.TOOL.IDEMPOTENCY_CONFLICT": "Agent tool idempotency conflict",
        "AGENT.TOOL.INVOCATION_IN_PROGRESS": "Agent tool invocation is in progress",
        "AGENT.OPERATION.IN_PROGRESS": "Agent operation is already in progress",
        "AGENT.OPERATION.RECONCILIATION_REQUIRED": (
            "Agent operation requires reconciliation before retry"
        ),
        "AGENT.OPERATION.STATE_CONFLICT": "Agent operation state does not permit execution",
        "AGENT.OPERATION.RESULT_UNAVAILABLE": "Agent operation replay result is unavailable",
        "AGENT.OPERATION.RESULT_MISMATCH": "Agent operation replay result failed integrity checks",
        "AGENT.TOOL.INVOCATION_BUDGET_EXCEEDED": "Agent tool invocation budget exceeded",
        "AGENT.TOOL.BYTE_BUDGET_EXCEEDED": "Agent tool byte budget exceeded",
        "AGENT.TOOL.COMMAND_BUDGET_EXCEEDED": "Agent sandbox command budget exceeded",
        "AGENT.TOOL.DEADLINE_EXPIRED": "Agent tool invocation deadline expired",
        "AGENT.TOOL.INVALID": "Agent tool invocation is invalid",
        "AGENT.TOOL.INVALID_RESULT": "Agent tool result is invalid",
        "AGENT.TOOL.UNAVAILABLE": "Agent tool is unavailable",
        "AGENT.BUILDER.IDEMPOTENCY_CONFLICT": (
            "Builder request key conflicts with different content"
        ),
        "AGENT.BUILDER.NO_PROGRESS": (
            "Builder submission does not advance the latest workflow phase"
        ),
        "AGENT.BUILDER.SNAPSHOT_INVALID": "Builder Workspace snapshot is stale",
        "AGENT.BUILDER.BINDING_INVALID": "Builder Project binding is invalid",
        "AGENT.BUILDER.ENVELOPE_UNAVAILABLE": (
            "Builder resource envelope is unavailable"
        ),
        "AGENT.BUILDER.VALIDATIONS_INVALID": (
            "Builder Blueprint must declare exactly one sandbox validation "
            "and one Slurm validation"
        ),
        "AGENT.TOOL.RESOURCE_ENVELOPE_EXCEEDED": (
            "Builder resources exceed the approved envelope"
        ),
        "AGENT.TOOL.READ_FAILED": "Agent read tool failed",
    }
    return messages[code]


def _header(headers: Mapping[str, str] | None, name: str) -> str | None:
    if headers is None:
        return None
    lowered = name.lower()
    return next((value for key, value in headers.items() if key.lower() == lowered), None)


def _error(
    status: int,
    code: str,
    message: str,
    *,
    retryable: bool = False,
) -> ApiResponse:
    payload: dict[str, object] = {"code": code, "message": message}
    if retryable:
        payload["retryable"] = True
    return ApiResponse(status=status, payload={"error": payload})
