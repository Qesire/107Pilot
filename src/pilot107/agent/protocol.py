"""Strict Python consumer for the pilot-agentd v1 wire protocol."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any

from pilot107.agent.project import is_project_agent_profile

TURN_PROTOCOL_VERSION = "pilot107.agent-turn-request/v1"
DURABLE_TURN_PROTOCOL_VERSION = "pilot107.agent-turn-request/v2"
EVENT_PROTOCOL_VERSION = "pilot107.agent-turn-event/v1"
CHECKPOINT_PROTOCOL_VERSION = "pilot107.agent-checkpoint/v1"
TOOL_INVOCATION_PROTOCOL_VERSION = "pilot107.agent-tool-invocation/v1"
TOOL_RESULT_PROTOCOL_VERSION = "pilot107.agent-tool-result/v1"

MAX_NDJSON_LINE_BYTES = 1024 * 1024
MAX_NDJSON_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_SAFE_INTEGER = 9_007_199_254_740_991

type JsonPrimitive = None | bool | int | float | str
type JsonValue = JsonPrimitive | list[JsonValue] | dict[str, JsonValue]

_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_DIGEST_PATTERN = re.compile(r"^[a-f0-9]{64}$")
_RFC3339_UTC_PATTERN = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z$"
)
_FORBIDDEN_JSON_KEYS = {
    "api_key",
    "authorization",
    "base_url",
    "system_prompt",
    "schema",
    "tools",
}
_TASK_KINDS = {"interactive", "explain", "contract_patch", "remediation_plan"}
_ERROR_CODES = {
    "provider_auth",
    "provider_rate_limited",
    "provider_timeout",
    "provider_unavailable",
    "provider_invalid_response",
    "output_contract_violation",
    "empty_provider_response",
    "tool_step_budget_exhausted",
    "aborted",
    "internal_error",
}
_EVENT_TYPES = {
    "turn_started",
    "message_delta",
    "tool_call_requested",
    "tool_call_started",
    "tool_call_progress",
    "tool_call_completed",
    "checkpoint",
    "turn_completed",
    "turn_failed",
}
_TERMINAL_TYPES = {"turn_completed", "turn_failed"}
_A1_READ_TOOLS = {
    "platform_get_snapshot",
    "platform_observation_get",
    "account_observation_get",
    "run_get",
    "run_log_read",
    "evidence_read",
    "run_resources_get",
}
_A2_PROJECT_TOOLS = {
    "project_get",
    "workspace_list",
    "workspace_read",
    "workspace_patch",
    "workspace_diff",
    "sandbox_exec",
    "validation_schedule",
}


@dataclass(frozen=True)
class DurableAgentTurnRequest:
    session_id: str
    turn_id: str
    owner: str
    state_version: int
    model_profile_id: str
    message: str
    context_refs: tuple[str, ...]
    capability_token: str = field(repr=False)
    checkpoint: dict[str, Any] | None = None
    profile_id: str = "hpc-readonly-v1"


@dataclass(frozen=True)
class ToolInvocation:
    schema_version: str
    invocation_id: str
    idempotency_key: str
    owner: str
    session_id: str
    turn_id: str
    state_version: int
    profile_id: str
    tool_name: str
    arguments: dict[str, Any]
    deadline: str


@dataclass(frozen=True)
class ToolResult:
    schema_version: str
    invocation_id: str
    result: dict[str, Any] | None
    error: dict[str, Any] | None
    evidence_refs: tuple[str, ...]
    bytes_returned: int


@dataclass(frozen=True)
class AgentTurnEvent:
    turn_id: str
    sequence: int
    type: str
    timestamp: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class AgentdTurnResult:
    result: JsonValue
    provider: str
    model: str
    model_profile_id: str
    input_tokens: int | None
    output_tokens: int | None
    cache_read_tokens: int | None
    cache_write_tokens: int | None
    provider_calls: int
    checkpoint_digest: str
    duration_ms: int
    checkpoint: dict[str, Any] | None


class AgentdClientError(RuntimeError):
    """A stable, redacted failure at the Python/Agentd boundary."""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        retryable: bool = False,
        provider_status: int | None = None,
        http_status: int | None = None,
        checkpoint: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.provider_status = provider_status
        self.http_status = http_status
        self.checkpoint = checkpoint


def parse_durable_turn_request(value: object) -> DurableAgentTurnRequest:
    try:
        request = _closed_object(
            value,
            required={
                "schema_version",
                "session_id",
                "turn_id",
                "owner",
                "state_version",
                "task_kind",
                "model_profile_id",
                "prompt_profile_id",
                "toolset_id",
                "input",
                "capability_token",
                "checkpoint",
                "limits",
                "trace",
            },
            label="durable Turn request",
        )
        if request["schema_version"] != DURABLE_TURN_PROTOCOL_VERSION:
            raise ValueError("unsupported durable Turn request version")
        pairing = (
            request["task_kind"] == "interactive_readonly"
            and request["prompt_profile_id"] in {"hpc-readonly-v1", "platform_coach"}
            and request["toolset_id"] == "a1-readonly"
        ) or (
            request["task_kind"]
            in {
                "experiment_builder",
                "run_diagnosis_repair",
                "market_application",
                "template_publication",
            }
            and request["prompt_profile_id"] == request["task_kind"]
            and request["toolset_id"] == "a2-project"
        )
        if not pairing:
            raise ValueError("invalid durable Turn request pairing")
        session_id = _validate_id(request["session_id"], "session_id")
        turn_id = _validate_id(request["turn_id"], "turn_id")
        owner = _validate_id(request["owner"], "owner")
        state_version = _validate_integer(
            request["state_version"], "state_version", minimum=0, maximum=MAX_SAFE_INTEGER
        )
        model_profile_id = _validate_id(request["model_profile_id"], "model_profile_id")
        turn_input = _closed_object(
            request["input"], required={"message", "context_refs"}, label="durable Turn input"
        )
        message = _validate_text(turn_input["message"], "message", minimum=1, maximum=64_000)
        context_refs = tuple(
            _validate_text(item, "context_ref", minimum=1, maximum=4_096)
            for item in _as_list(turn_input["context_refs"], "context_refs", maximum=256)
        )
        capability_token = _validate_text(
            request["capability_token"], "capability_token", minimum=1, maximum=8_192
        )
        checkpoint = request["checkpoint"]
        if checkpoint is not None:
            checkpoint = validate_checkpoint(checkpoint)
        limits = _closed_object(
            request["limits"], required={"timeout_ms", "max_output_tokens"}, label="limits"
        )
        _validate_integer(limits["timeout_ms"], "timeout_ms", minimum=100, maximum=300_000)
        _validate_integer(
            limits["max_output_tokens"], "max_output_tokens", minimum=1, maximum=32_000
        )
        trace = _closed_object(
            request["trace"], required={"correlation_id"}, label="trace"
        )
        _validate_id(trace["correlation_id"], "correlation_id")
        return DurableAgentTurnRequest(
            session_id=session_id,
            turn_id=turn_id,
            owner=owner,
            state_version=state_version,
            model_profile_id=model_profile_id,
            message=message,
            context_refs=context_refs,
            capability_token=capability_token,
            checkpoint=checkpoint,
            profile_id=_validate_id(request["prompt_profile_id"], "prompt_profile_id"),
        )
    except (TypeError, ValueError, RecursionError, UnicodeError):
        raise ValueError("invalid durable Turn request") from None


def parse_tool_invocation(value: object) -> ToolInvocation:
    try:
        invocation = _closed_object(
            value,
            required={
                "schema_version",
                "invocation_id",
                "idempotency_key",
                "owner",
                "session_id",
                "turn_id",
                "state_version",
                "profile_id",
                "tool_name",
                "arguments",
                "deadline",
            },
            label="tool invocation",
        )
        if invocation["schema_version"] != TOOL_INVOCATION_PROTOCOL_VERSION:
            raise ValueError("unsupported tool invocation version")
        tool_name = _as_string(invocation["tool_name"], "tool_name")
        profile_id = _as_string(invocation["profile_id"], "profile_id")
        valid_tools = (
            _A2_PROJECT_TOOLS
            if is_project_agent_profile(profile_id)
            else _A1_READ_TOOLS
            if profile_id in {"hpc-readonly-v1", "platform_coach"}
            else set()
        )
        if tool_name not in valid_tools:
            raise ValueError("tool is invalid for profile")
        arguments = _as_object(invocation["arguments"], "arguments")
        _validate_json_object(arguments)
        deadline = _as_string(invocation["deadline"], "deadline")
        if _RFC3339_UTC_PATTERN.fullmatch(deadline) is None:
            raise ValueError("invalid deadline")
        return ToolInvocation(
            schema_version=TOOL_INVOCATION_PROTOCOL_VERSION,
            invocation_id=_validate_id(invocation["invocation_id"], "invocation_id"),
            idempotency_key=_validate_id(invocation["idempotency_key"], "idempotency_key"),
            owner=_validate_id(invocation["owner"], "owner"),
            session_id=_validate_id(invocation["session_id"], "session_id"),
            turn_id=_validate_id(invocation["turn_id"], "turn_id"),
            state_version=_validate_integer(
                invocation["state_version"],
                "state_version",
                minimum=0,
                maximum=MAX_SAFE_INTEGER,
            ),
            profile_id=profile_id,
            tool_name=tool_name,
            arguments=arguments,
            deadline=deadline,
        )
    except (TypeError, ValueError, RecursionError, UnicodeError):
        raise ValueError("invalid tool invocation") from None


def parse_tool_result(value: object) -> ToolResult:
    try:
        tool_result = _closed_object(
            value,
            required={
                "schema_version",
                "invocation_id",
                "result",
                "error",
                "evidence_refs",
                "bytes_returned",
            },
            label="tool result",
        )
        if tool_result["schema_version"] != TOOL_RESULT_PROTOCOL_VERSION:
            raise ValueError("unsupported tool result version")
        result = tool_result["result"]
        error = tool_result["error"]
        if (result is None) == (error is None):
            raise ValueError("tool result must contain exactly one branch")
        if result is not None:
            result = _as_object(result, "tool result payload")
            _validate_json_object(result)
        if error is not None:
            error = _closed_object(
                error,
                required={"code", "message", "retryable"},
                label="tool result error",
            )
            _validate_id(error["code"], "tool error code")
            _validate_text(error["message"], "tool error message", minimum=1, maximum=4_096)
            _as_bool(error["retryable"], "tool error retryable")
        evidence_refs = tuple(
            _validate_text(item, "evidence_ref", minimum=1, maximum=4_096)
            for item in _as_list(
                tool_result["evidence_refs"], "evidence_refs", maximum=256
            )
        )
        return ToolResult(
            schema_version=TOOL_RESULT_PROTOCOL_VERSION,
            invocation_id=_validate_id(tool_result["invocation_id"], "invocation_id"),
            result=result,
            error=error,
            evidence_refs=evidence_refs,
            bytes_returned=_validate_integer(
                tool_result["bytes_returned"],
                "bytes_returned",
                minimum=0,
                maximum=1_048_576,
            ),
        )
    except (TypeError, ValueError, RecursionError, UnicodeError):
        raise ValueError("invalid tool result") from None


def parse_event_lines(
    turn_id: str,
    lines: Iterable[bytes],
) -> Iterator[AgentTurnEvent]:
    """Parse a complete NDJSON Turn stream and fail closed on any drift."""

    _validate_id(turn_id, "expected turn_id")
    cumulative_bytes = 0
    expected_sequence = 1
    terminal_seen = False

    for raw_line in lines:
        if terminal_seen:
            raise _protocol_error("data follows the terminal event")
        if not isinstance(raw_line, bytes):
            raise _protocol_error("NDJSON lines must be bytes")
        if len(raw_line) > MAX_NDJSON_LINE_BYTES:
            raise _protocol_error("NDJSON line exceeds the byte limit")
        cumulative_bytes += len(raw_line)
        if cumulative_bytes > MAX_NDJSON_RESPONSE_BYTES:
            raise _protocol_error("NDJSON cumulative byte limit exceeded")

        try:
            text = raw_line.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            raise _protocol_error("NDJSON contains invalid UTF-8") from None
        try:
            value = json.loads(text, parse_constant=_reject_json_constant)
        except (json.JSONDecodeError, ValueError):
            raise _protocol_error("NDJSON line is not valid JSON") from None
        if not isinstance(value, dict):
            raise _protocol_error("NDJSON event must be an object")

        try:
            event = _parse_event(value)
        except (TypeError, ValueError, RecursionError, UnicodeError):
            raise _protocol_error("event does not match the closed v1 schema") from None
        if event.turn_id != turn_id:
            raise _protocol_error("event turn_id does not match the requested Turn")
        if event.sequence != expected_sequence:
            raise _protocol_error("event sequence must be contiguous and start at one")

        expected_sequence += 1
        terminal_seen = event.type in _TERMINAL_TYPES
        yield event

    if not terminal_seen:
        raise _protocol_error("event stream ended before a terminal event")


def result_from_terminal(event: AgentTurnEvent | None) -> AgentdTurnResult:
    if event is None:
        raise _protocol_error("event stream did not produce a terminal event")
    if event.type == "turn_failed":
        error = _as_object(event.payload["error"], "turn_failed.error")
        checkpoint = event.payload.get("checkpoint")
        raise AgentdClientError(
            "pilot-agentd Turn failed",
            code=_as_string(error["code"], "turn_failed.error.code"),
            retryable=_as_bool(error["retryable"], "turn_failed.error.retryable"),
            provider_status=_optional_int(error.get("provider_status")),
            checkpoint=checkpoint if isinstance(checkpoint, dict) else None,
        )
    if event.type != "turn_completed":
        raise _protocol_error("event is not terminal")

    payload = event.payload
    usage = _as_object(payload["usage"], "turn_completed.usage")
    checkpoint = payload.get("checkpoint")
    return AgentdTurnResult(
        result=payload["result"],
        provider=_as_string(payload["provider"], "turn_completed.provider"),
        model=_as_string(payload["model"], "turn_completed.model"),
        model_profile_id=_as_string(payload["model_profile_id"], "turn_completed.model_profile_id"),
        input_tokens=_optional_int(usage["input_tokens"]),
        output_tokens=_optional_int(usage["output_tokens"]),
        cache_read_tokens=_optional_int(usage["cache_read_tokens"]),
        cache_write_tokens=_optional_int(usage["cache_write_tokens"]),
        provider_calls=_as_int(payload["provider_calls"], "turn_completed.provider_calls"),
        checkpoint_digest=_as_string(
            payload["checkpoint_digest"], "turn_completed.checkpoint_digest"
        ),
        duration_ms=_as_int(payload["duration_ms"], "turn_completed.duration_ms"),
        checkpoint=checkpoint if isinstance(checkpoint, dict) else None,
    )


def validate_checkpoint(value: object) -> dict[str, Any]:
    checkpoint = _closed_object(
        value,
        required={
            "schema_version",
            "turn_id",
            "lineage",
            "model_profile_id",
            "prompt_profile_id",
            "messages",
            "completed_tools",
            "usage",
            "digest",
        },
        label="checkpoint",
    )
    if checkpoint["schema_version"] != CHECKPOINT_PROTOCOL_VERSION:
        raise ValueError("unsupported checkpoint schema")
    _validate_id(checkpoint["turn_id"], "checkpoint.turn_id")
    _validate_id(checkpoint["model_profile_id"], "checkpoint.model_profile_id")
    _validate_id(checkpoint["prompt_profile_id"], "checkpoint.prompt_profile_id")
    _validate_id_list(checkpoint["lineage"], "checkpoint.lineage", maximum=256)

    messages = _as_list(checkpoint["messages"], "checkpoint.messages", maximum=4_096)
    for index, raw_message in enumerate(messages):
        message = _closed_object(
            raw_message,
            required={"role", "content", "tool_call_id", "tool_name", "is_error"},
            label=f"checkpoint.messages[{index}]",
        )
        if message["role"] not in {"user", "assistant", "tool_result"}:
            raise ValueError("invalid checkpoint message role")
        _validate_text(message["content"], "checkpoint message content", maximum=256_000)
        _validate_nullable_id(message["tool_call_id"], "checkpoint message tool_call_id")
        _validate_nullable_id(message["tool_name"], "checkpoint message tool_name")
        if message["is_error"] is not None:
            _as_bool(message["is_error"], "checkpoint message is_error")

    completed_tools = _as_list(
        checkpoint["completed_tools"], "checkpoint.completed_tools", maximum=4_096
    )
    for index, raw_tool in enumerate(completed_tools):
        tool = _closed_object(
            raw_tool,
            required={"tool_call_id", "tool_name", "arguments", "result", "is_error"},
            label=f"checkpoint.completed_tools[{index}]",
        )
        _validate_id(tool["tool_call_id"], "completed tool call id")
        _validate_id(tool["tool_name"], "completed tool name")
        arguments = _as_object(tool["arguments"], "completed tool arguments")
        _validate_json_object(arguments)
        _validate_json_value(tool["result"])
        _as_bool(tool["is_error"], "completed tool is_error")

    _validate_usage(checkpoint["usage"], "checkpoint.usage")
    digest = _as_string(checkpoint["digest"], "checkpoint.digest")
    if _DIGEST_PATTERN.fullmatch(digest) is None:
        raise ValueError("invalid checkpoint digest")
    return checkpoint


def validate_json_object(value: object) -> dict[str, Any]:
    object_value = _as_object(value, "JSON object")
    _validate_json_object(object_value)
    return object_value


def _parse_event(value: dict[str, Any]) -> AgentTurnEvent:
    event = _closed_object(
        value,
        required={"schema_version", "turn_id", "sequence", "timestamp", "type", "payload"},
        label="event",
    )
    if event["schema_version"] != EVENT_PROTOCOL_VERSION:
        raise ValueError("unsupported event schema")
    turn_id = _validate_id(event["turn_id"], "event.turn_id")
    sequence = _validate_integer(event["sequence"], "event.sequence", minimum=1)
    timestamp = _validate_text(event["timestamp"], "event.timestamp", minimum=1, maximum=64)
    event_type = _as_string(event["type"], "event.type")
    if event_type not in _EVENT_TYPES:
        raise ValueError("unknown event type")
    payload = _as_object(event["payload"], "event.payload")
    _validate_event_payload(event_type, payload)
    return AgentTurnEvent(
        turn_id=turn_id,
        sequence=sequence,
        type=event_type,
        timestamp=timestamp,
        payload=payload,
    )


def _validate_event_payload(event_type: str, raw_payload: dict[str, Any]) -> None:
    if event_type == "turn_started":
        payload = _closed_object(
            raw_payload,
            required={"model_profile_id", "task_kind"},
            label="turn_started.payload",
        )
        _validate_id(payload["model_profile_id"], "turn_started.model_profile_id")
        if payload["task_kind"] not in _TASK_KINDS:
            raise ValueError("invalid task kind")
        return
    if event_type == "message_delta":
        payload = _closed_object(raw_payload, required={"delta"}, label="message_delta.payload")
        _validate_text(payload["delta"], "message_delta.delta", maximum=256_000)
        return
    if event_type == "tool_call_requested":
        payload = _closed_object(
            raw_payload,
            required={"tool_call_id", "tool_name", "arguments"},
            label="tool_call_requested.payload",
        )
        _validate_tool_base(payload)
        _validate_json_object(_as_object(payload["arguments"], "tool arguments"))
        return
    if event_type == "tool_call_started":
        payload = _closed_object(
            raw_payload,
            required={"tool_call_id", "tool_name"},
            label="tool_call_started.payload",
        )
        _validate_tool_base(payload)
        return
    if event_type == "tool_call_progress":
        payload = _closed_object(
            raw_payload,
            required={"tool_call_id", "tool_name", "progress"},
            label="tool_call_progress.payload",
        )
        _validate_tool_base(payload)
        _validate_text(payload["progress"], "tool progress", maximum=256_000)
        return
    if event_type == "tool_call_completed":
        payload = _closed_object(
            raw_payload,
            required={"tool_call_id", "tool_name", "result", "is_error"},
            label="tool_call_completed.payload",
        )
        _validate_tool_base(payload)
        _validate_json_value(payload["result"])
        _as_bool(payload["is_error"], "tool completion is_error")
        return
    if event_type == "checkpoint":
        payload = _closed_object(raw_payload, required={"checkpoint"}, label="checkpoint.payload")
        validate_checkpoint(payload["checkpoint"])
        return
    if event_type == "turn_completed":
        payload = _closed_object(
            raw_payload,
            required={
                "result",
                "provider",
                "model",
                "model_profile_id",
                "usage",
                "provider_calls",
                "checkpoint_digest",
                "duration_ms",
            },
            optional={"checkpoint"},
            label="turn_completed.payload",
        )
        _validate_json_value(payload["result"])
        _validate_id(payload["provider"], "turn_completed.provider")
        _validate_text(payload["model"], "turn_completed.model", minimum=1, maximum=512)
        _validate_id(payload["model_profile_id"], "turn_completed.model_profile_id")
        _validate_usage(payload["usage"], "turn_completed.usage")
        _validate_integer(payload["provider_calls"], "provider_calls", minimum=1, maximum=100)
        digest = _as_string(payload["checkpoint_digest"], "checkpoint_digest")
        if _DIGEST_PATTERN.fullmatch(digest) is None:
            raise ValueError("invalid checkpoint digest")
        _validate_integer(payload["duration_ms"], "duration_ms", minimum=0, maximum=3_600_000)
        if "checkpoint" in payload:
            validate_checkpoint(payload["checkpoint"])
        return
    if event_type == "turn_failed":
        payload = _closed_object(
            raw_payload,
            required={"error"},
            optional={"checkpoint"},
            label="turn_failed.payload",
        )
        error = _closed_object(
            payload["error"],
            required={"code", "retryable", "message"},
            optional={"provider_status"},
            label="turn_failed.error",
        )
        if error["code"] not in _ERROR_CODES:
            raise ValueError("unknown Turn error code")
        _as_bool(error["retryable"], "turn error retryable")
        _validate_text(error["message"], "turn error message", minimum=1, maximum=4_096)
        if "provider_status" in error:
            _validate_integer(error["provider_status"], "provider_status", minimum=100, maximum=599)
        if "checkpoint" in payload:
            validate_checkpoint(payload["checkpoint"])
        return
    raise ValueError("unknown event type")


def _validate_usage(value: object, label: str) -> dict[str, Any]:
    usage = _closed_object(
        value,
        required={
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
        },
        label=label,
    )
    for usage_field in (
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
    ):
        count = usage[usage_field]
        if count is not None:
            _validate_integer(
                count,
                f"{label}.{usage_field}",
                minimum=0,
                maximum=MAX_SAFE_INTEGER,
            )
    return usage


def _validate_tool_base(payload: dict[str, Any]) -> None:
    _validate_id(payload["tool_call_id"], "tool_call_id")
    _validate_id(payload["tool_name"], "tool_name")


def _validate_json_object(value: dict[str, Any], *, depth: int = 0) -> None:
    if len(value) > 4_096:
        raise ValueError("JSON object has too many properties")
    for key, nested in value.items():
        if not isinstance(key, str):
            raise TypeError("JSON object key must be a string")
        if key.lower() in _FORBIDDEN_JSON_KEYS:
            raise ValueError("JSON object contains a forbidden field")
        _validate_json_value(nested, depth=depth + 1)


def _validate_json_value(value: object, *, depth: int = 0) -> None:
    if depth > 256:
        raise ValueError("JSON value is nested too deeply")
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, str):
        _validate_text(value, "JSON string", maximum=256_000)
        return
    if isinstance(value, int):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("JSON number must be finite")
        return
    if isinstance(value, list):
        if len(value) > 4_096:
            raise ValueError("JSON array has too many items")
        for nested in value:
            _validate_json_value(nested, depth=depth + 1)
        return
    if isinstance(value, dict):
        _validate_json_object(value, depth=depth)
        return
    raise TypeError("value is not JSON")


def _closed_object(
    value: object,
    *,
    required: set[str],
    label: str,
    optional: set[str] | None = None,
) -> dict[str, Any]:
    object_value = _as_object(value, label)
    allowed = required | (optional or set())
    fields = set(object_value)
    if not required.issubset(fields) or not fields.issubset(allowed):
        raise ValueError(f"{label} fields do not match the closed schema")
    return object_value


def _as_object(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError(f"{label} must be an object")
    if not all(isinstance(key, str) for key in value):
        raise TypeError(f"{label} keys must be strings")
    return value


def _as_list(value: object, label: str, *, maximum: int) -> list[Any]:
    if not isinstance(value, list) or len(value) > maximum:
        raise TypeError(f"{label} must be a bounded array")
    return value


def _as_string(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be a string")
    return value


def _as_bool(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{label} must be a boolean")
    return value


def _as_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{label} must be an integer")
    return value


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    return _as_int(value, "optional integer")


def _validate_integer(
    value: object,
    label: str,
    *,
    minimum: int,
    maximum: int | None = None,
) -> int:
    integer = _as_int(value, label)
    if integer < minimum or (maximum is not None and integer > maximum):
        raise ValueError(f"{label} is outside the supported range")
    return integer


def _validate_text(
    value: object,
    label: str,
    *,
    minimum: int = 0,
    maximum: int,
) -> str:
    text = _as_string(value, label)
    length = len(text.encode("utf-16-le")) // 2
    if length < minimum or length > maximum:
        raise ValueError(f"{label} is outside the supported range")
    return text


def _validate_id(value: object, label: str) -> str:
    identifier = _as_string(value, label)
    if _ID_PATTERN.fullmatch(identifier) is None:
        raise ValueError(f"{label} is not a valid protocol ID")
    return identifier


def _validate_nullable_id(value: object, label: str) -> None:
    if value is not None:
        _validate_id(value, label)


def _validate_id_list(value: object, label: str, *, maximum: int) -> None:
    for identifier in _as_list(value, label, maximum=maximum):
        _validate_id(identifier, label)


def _reject_json_constant(_value: str) -> None:
    raise ValueError("non-finite JSON number")


def _protocol_error(detail: str) -> AgentdClientError:
    return AgentdClientError(
        f"pilot-agentd protocol error: {detail}",
        code="protocol_error",
        retryable=False,
    )
