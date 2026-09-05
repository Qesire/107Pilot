"""Authenticated synchronous HTTP/NDJSON client for pilot-agentd."""

from __future__ import annotations

import http.client
import json
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, suppress
from typing import Any, Protocol, cast

from pilot107.agent.config import AgentdClientConfig
from pilot107.agent.project import is_project_agent_profile
from pilot107.agent.protocol import (
    MAX_NDJSON_LINE_BYTES,
    AgentdClientError,
    AgentdTurnResult,
    AgentTurnEvent,
    DurableAgentTurnRequest,
    parse_durable_turn_request,
    parse_event_lines,
    result_from_terminal,
    validate_checkpoint,
    validate_json_object,
)
from pilot107.agent.repair_protocol import (
    DURABLE_REPAIR_TURN_PROTOCOL_VERSION,
    ReceiptRepairingDurableAgentTurnRequest,
    serialize_receipt_repair,
    validate_repairing_payload,
)

_TASK_PAIRINGS = {
    "interactive": ("hpc-assistant-v1", "a0-none"),
    "explain": ("agent-explain-v1", "emit-explanation-v1"),
    "contract_patch": ("contract-patch-v1", "emit-contract-patch-v1"),
    "remediation_plan": ("remediation-plan-v1", "emit-remediation-plan-v1"),
}
_HTTP_ERROR_CODES = {
    "unauthorized",
    "method_not_allowed",
    "not_found",
    "unsupported_media_type",
    "invalid_content_length",
    "request_too_large",
    "invalid_body",
    "invalid_utf8",
    "invalid_json",
    "invalid_turn",
    "turn_active",
    "shutting_down",
    "internal_error",
}
_CANCEL_RESPONSE_LIMIT = 64 * 1024


class _ReadableResponse(Protocol):
    headers: Any

    def read(self, size: int = -1) -> bytes: ...

    def readline(self, size: int = -1) -> bytes: ...

    def close(self) -> None: ...


type Opener = Callable[..., AbstractContextManager[_ReadableResponse]]
type EventCallback = Callable[[AgentTurnEvent], None]


class AgentdClient:
    def __init__(
        self,
        config: AgentdClientConfig,
        *,
        opener: Opener | None = None,
    ) -> None:
        self.config = config
        self._opener = opener or cast(Opener, urllib.request.urlopen)

    def stream_turn(
        self,
        *,
        turn_id: str | None = None,
        task_kind: str,
        prompt_profile_id: str,
        toolset_id: str,
        input_payload: dict[str, Any],
        checkpoint: dict[str, Any] | None = None,
        on_event: EventCallback | None = None,
    ) -> Iterator[AgentTurnEvent]:
        actual_turn_id = turn_id or str(uuid.uuid4())
        payload = _build_turn_request(
            self.config,
            actual_turn_id,
            task_kind,
            prompt_profile_id,
            toolset_id,
            input_payload,
            checkpoint,
        )
        yield from self._stream_payload(actual_turn_id, payload, on_event=on_event)

    def stream_durable_turn(
        self,
        request: DurableAgentTurnRequest,
        on_event: EventCallback | None = None,
    ) -> Iterator[AgentTurnEvent]:
        payload = _build_durable_turn_request(self.config, request)
        yield from self._stream_payload(request.turn_id, payload, on_event=on_event)

    def _stream_payload(
        self,
        turn_id: str,
        payload: dict[str, Any],
        *,
        on_event: EventCallback | None,
    ) -> Iterator[AgentTurnEvent]:
        http_request = urllib.request.Request(
            f"{self.config.base_url}/internal/v1/turns",
            data=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.config.token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with self._opener(http_request, timeout=float(self.config.timeout_seconds)) as response:
                if not _is_content_type(response, "application/x-ndjson"):
                    raise _protocol_error("Turn response content type is invalid")
                for event in parse_event_lines(turn_id, _bounded_lines(response)):
                    if on_event is not None:
                        on_event(event)
                    yield event
        except AgentdClientError:
            raise
        except urllib.error.HTTPError as exc:
            raise _map_http_error(exc) from None
        except (
            TimeoutError,
            urllib.error.URLError,
            ConnectionError,
            http.client.HTTPException,
            OSError,
        ):
            raise AgentdClientError(
                "pilot-agentd transport failed",
                code="transport_error",
                retryable=True,
            ) from None

    def run_turn(self, **kwargs: Any) -> AgentdTurnResult:
        terminal: AgentTurnEvent | None = None
        for event in self.stream_turn(**kwargs):
            if event.type in {"turn_completed", "turn_failed"}:
                terminal = event
        return result_from_terminal(terminal)

    def cancel_turn(self, turn_id: str) -> str:
        if not _is_protocol_id(turn_id):
            raise AgentdClientError(
                "pilot-agentd cancel request is invalid",
                code="invalid_request",
            )
        encoded_turn_id = urllib.parse.quote(turn_id, safe="")
        request = urllib.request.Request(
            f"{self.config.base_url}/internal/v1/turns/{encoded_turn_id}/cancel",
            data=b"{}",
            headers={
                "Authorization": f"Bearer {self.config.token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with self._opener(request, timeout=float(self.config.timeout_seconds)) as response:
                if not _is_content_type(response, "application/json"):
                    raise _protocol_error("cancel response content type is invalid")
                payload = _read_json_object(response, _CANCEL_RESPONSE_LIMIT)
        except AgentdClientError:
            raise
        except urllib.error.HTTPError as exc:
            raise _map_http_error(exc) from None
        except (
            TimeoutError,
            urllib.error.URLError,
            ConnectionError,
            http.client.HTTPException,
            OSError,
        ):
            raise AgentdClientError(
                "pilot-agentd transport failed",
                code="transport_error",
                retryable=True,
            ) from None
        if set(payload) != {"status"} or payload["status"] not in {"accepted", "not_active"}:
            raise _protocol_error("cancel response does not match the closed schema")
        return cast(str, payload["status"])


def _build_turn_request(
    config: AgentdClientConfig,
    turn_id: str,
    task_kind: str,
    prompt_profile_id: str,
    toolset_id: str,
    input_payload: dict[str, Any],
    checkpoint: dict[str, Any] | None,
) -> dict[str, Any]:
    expected_pairing = _TASK_PAIRINGS.get(task_kind)
    if (
        not _is_protocol_id(turn_id)
        or expected_pairing is None
        or expected_pairing != (prompt_profile_id, toolset_id)
    ):
        raise AgentdClientError(
            "pilot-agentd Turn request is invalid",
            code="invalid_request",
        )
    try:
        validate_json_object(input_payload)
        if checkpoint is not None:
            validate_checkpoint(checkpoint)
    except (TypeError, ValueError, RecursionError, UnicodeError):
        raise AgentdClientError(
            "pilot-agentd Turn request is invalid",
            code="invalid_request",
        ) from None
    return {
        "schema_version": "pilot107.agent-turn-request/v1",
        "turn_id": turn_id,
        "task_kind": task_kind,
        "model_profile_id": config.model_profile_id,
        "prompt_profile_id": prompt_profile_id,
        "toolset_id": toolset_id,
        "input": input_payload,
        "checkpoint": checkpoint,
        "limits": {
            "timeout_ms": round(float(config.timeout_seconds) * 1_000),
            "max_output_tokens": config.max_output_tokens,
        },
        "trace": {"correlation_id": turn_id},
    }


def _build_durable_turn_request(
    config: AgentdClientConfig,
    request: DurableAgentTurnRequest,
) -> dict[str, Any]:
    project_profile = is_project_agent_profile(request.profile_id)
    if request.profile_id not in {
        "hpc-readonly-v1",
        "platform_coach",
        "experiment_builder",
        "run_diagnosis_repair",
        "market_application",
        "template_publication",
    }:
        raise AgentdClientError(
            "pilot-agentd durable Turn profile is invalid",
            code="invalid_request",
        )
    repairs = (
        request.receipt_repairs
        if isinstance(request, ReceiptRepairingDurableAgentTurnRequest)
        else ()
    )
    payload: dict[str, Any] = {
        "schema_version": (
            DURABLE_REPAIR_TURN_PROTOCOL_VERSION if repairs else "pilot107.agent-turn-request/v2"
        ),
        "session_id": request.session_id,
        "turn_id": request.turn_id,
        "owner": request.owner,
        "state_version": request.state_version,
        "task_kind": request.profile_id if project_profile else "interactive_readonly",
        "model_profile_id": request.model_profile_id,
        "prompt_profile_id": request.profile_id,
        "toolset_id": "a2-project" if project_profile else "a1-readonly",
        "input": {
            "message": request.message,
            "context_refs": list(request.context_refs),
        },
        "capability_token": request.capability_token,
        "checkpoint": request.checkpoint,
        "limits": {
            "timeout_ms": round(float(config.timeout_seconds) * 1_000),
            "max_output_tokens": config.max_output_tokens,
        },
        "trace": {"correlation_id": request.turn_id},
    }
    if repairs:
        payload["receipt_repairs"] = [serialize_receipt_repair(repair) for repair in repairs]
    try:
        if request.model_profile_id != config.model_profile_id:
            raise ValueError("model profile mismatch")
        if repairs:
            validate_repairing_payload(payload)
        else:
            parse_durable_turn_request(payload)
    except (TypeError, ValueError, RecursionError, UnicodeError):
        raise AgentdClientError(
            "pilot-agentd durable Turn request is invalid",
            code="invalid_request",
        ) from None
    return payload


def _bounded_lines(response: _ReadableResponse) -> Iterator[bytes]:
    while True:
        line = response.readline(MAX_NDJSON_LINE_BYTES + 1)
        if line == b"":
            return
        yield line


def _read_json_object(response: _ReadableResponse, limit: int) -> dict[str, Any]:
    body = response.read(limit + 1)
    if len(body) > limit:
        raise _protocol_error("JSON response exceeds the byte limit")
    try:
        text = body.decode("utf-8", errors="strict")
        value = json.loads(text, parse_constant=_reject_json_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise _protocol_error("JSON response is invalid") from None
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise _protocol_error("JSON response must be an object")
    return value


def _map_http_error(error: urllib.error.HTTPError) -> AgentdClientError:
    code = "http_error"
    status = error.code
    try:
        content_type = error.headers.get("Content-Type", "") if error.headers else ""
        parts = [part.strip().lower() for part in str(content_type).split(";")]
        if parts == ["application/json", "charset=utf-8"]:
            body = error.read(_CANCEL_RESPONSE_LIMIT + 1)
            if len(body) <= _CANCEL_RESPONSE_LIMIT:
                try:
                    value = json.loads(
                        body.decode("utf-8", errors="strict"),
                        parse_constant=_reject_json_constant,
                    )
                except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
                    value = None
                if isinstance(value, dict) and set(value) == {"error"}:
                    nested = value["error"]
                    if isinstance(nested, dict) and set(nested) == {"code"}:
                        candidate = nested["code"]
                        if isinstance(candidate, str) and candidate in _HTTP_ERROR_CODES:
                            code = candidate
    except (TimeoutError, ConnectionError, http.client.HTTPException, OSError):
        pass
    finally:
        with suppress(OSError):
            error.close()
    return AgentdClientError(
        "pilot-agentd HTTP request failed",
        code=code,
        retryable=status == 503,
        http_status=status,
    )


def _is_content_type(response: _ReadableResponse, expected: str) -> bool:
    raw = response.headers.get("Content-Type", "")
    parts = [part.strip().lower() for part in str(raw).split(";")]
    return parts == [expected, "charset=utf-8"]


def _is_protocol_id(value: object) -> bool:
    if not isinstance(value, str) or not 1 <= len(value) <= 128:
        return False
    return all(char.isascii() and (char.isalnum() or char in "._:-") for char in value)


def _reject_json_constant(_value: str) -> None:
    raise ValueError("non-finite JSON number")


def _protocol_error(detail: str) -> AgentdClientError:
    return AgentdClientError(
        f"pilot-agentd protocol error: {detail}",
        code="protocol_error",
    )
