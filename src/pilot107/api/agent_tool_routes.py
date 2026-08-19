"""Private HTTP boundary for one capability-authorized Agent tool invocation."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict

from pilot107.agent.protocol import parse_tool_invocation
from pilot107.agent.tool_gateway import AgentToolGateway, AgentToolGatewayError
from pilot107.api.http_types import ApiResponse

_MAX_BODY_BYTES = 1024 * 1024


class AgentToolRoutes:
    def __init__(self, gateway: AgentToolGateway) -> None:
        self.gateway = gateway

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
            return _gateway_error(exc)
        except Exception:
            return _error(500, "AGENT.TOOL.INTERNAL", "Agent tool invocation failed")
        payload = asdict(result)
        payload["evidence_refs"] = list(result.evidence_refs)
        return ApiResponse(status=200, payload=payload)


def _gateway_error(exc: AgentToolGatewayError) -> ApiResponse:
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
    }:
        status = 409
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
    elif code == "AGENT.TOOL.UNAVAILABLE":
        status = 503
    else:
        status = 502
        code = "AGENT.TOOL.READ_FAILED"
    message = _public_message(code)
    return _error(status, code, message, retryable=exc.retryable)


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
        "AGENT.TOOL.INVOCATION_BUDGET_EXCEEDED": "Agent tool invocation budget exceeded",
        "AGENT.TOOL.BYTE_BUDGET_EXCEEDED": "Agent tool byte budget exceeded",
        "AGENT.TOOL.COMMAND_BUDGET_EXCEEDED": "Agent sandbox command budget exceeded",
        "AGENT.TOOL.DEADLINE_EXPIRED": "Agent tool invocation deadline expired",
        "AGENT.TOOL.INVALID": "Agent tool invocation is invalid",
        "AGENT.TOOL.INVALID_RESULT": "Agent tool result is invalid",
        "AGENT.TOOL.UNAVAILABLE": "Agent tool is unavailable",
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
