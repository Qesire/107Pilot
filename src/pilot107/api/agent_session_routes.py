"""Owner-scoped HTTP routes for durable A1 Agent Sessions and Turns."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from pilot107.agent.session import (
    AgentSessionConflict,
    AgentSessionInvariantError,
    AgentSessionRecord,
    AgentSessionState,
    AgentTurnEventRecord,
    AgentTurnRecord,
)
from pilot107.api.http_types import ApiResponse
from pilot107.core.identity import UserIdentity
from pilot107.core.pagination import (
    CursorError,
    CursorPosition,
    cursor_scope,
    decode_cursor,
    encode_cursor,
)
from pilot107.services.agent_session_service import AgentSessionService

_SOURCE_KEYS = frozenset(
    {"run_id", "project_id", "workspace_id", "evidence_id", "resource_envelope"}
)
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,255}$")
_PROTOCOL_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_AUTHORITY_KEYS = frozenset({"authorization", "capability_token"})


class AgentSessionRoutes:
    def __init__(self, service: AgentSessionService) -> None:
        self.service = service

    def handle_get(
        self,
        parts: list[str],
        *,
        params: Mapping[str, list[str]],
        headers: Mapping[str, str] | None,
        identity: UserIdentity | None,
    ) -> ApiResponse | None:
        if not parts or parts[0] != "agent-sessions":
            return None
        if identity is None:
            return _error(401, "AUTH.MISSING", "authenticated identity is required")
        owner = identity.username
        if len(parts) == 1:
            try:
                _reject_unknown_params(params, {"state", "limit", "cursor"})
                states = _query_states(params)
                limit = _query_limit(params)
                scope = cursor_scope(
                    "agent_sessions",
                    {"owner": owner, "states": sorted(state.value for state in states)},
                )
                position = _query_cursor(params, scope=scope)
                before = (
                    None
                    if position is None
                    else f"{position.primary}|{position.secondary}"
                )
                sessions, raw_next = self.service.store.list_sessions_page(
                    owner=owner,
                    states=states or None,
                    before=before,
                    limit=limit,
                )
                next_cursor = _public_cursor(raw_next, scope=scope)
                return ApiResponse(
                    status=200,
                    payload={
                        "items": [_session_payload(session) for session in sessions],
                        "page": {
                            "limit": limit,
                            "has_more": next_cursor is not None,
                            "next_cursor": next_cursor,
                        },
                    },
                )
            except (CursorError, ValueError, AgentSessionInvariantError) as exc:
                return _error(400, "AGENT.SESSION.INVALID_QUERY", str(exc))
        if len(parts) == 2:
            try:
                _reject_unknown_params(params, set())
                session = self.service.store.get_session(parts[1], owner=owner)
                return ApiResponse(status=200, payload=_session_payload(session))
            except KeyError:
                return _error(404, "AGENT.SESSION.NOT_FOUND", "Agent Session not found")
            except ValueError as exc:
                return _error(400, "AGENT.SESSION.INVALID_QUERY", str(exc))
        if len(parts) == 3 and parts[2] == "events":
            try:
                _reject_unknown_params(params, {"limit", "after_event_id"})
                session = self.service.store.get_session(parts[1], owner=owner)
                limit = _query_limit(params)
                after_event_id = _query_after_event_id(params, headers=headers)
                events, next_event_id = self.service.store.list_events_page(
                    session_id=session.session_id,
                    owner=owner,
                    after_event_id=after_event_id,
                    limit=limit,
                )
                return ApiResponse(
                    status=200,
                    payload={
                        "session_id": session.session_id,
                        "items": [_event_payload(event) for event in events],
                        "page": {
                            "limit": limit,
                            "has_more": next_event_id is not None,
                            "next_after_event_id": next_event_id,
                            "last_event_id": events[-1].event_id if events else after_event_id,
                        },
                    },
                )
            except KeyError:
                return _error(404, "AGENT.SESSION.NOT_FOUND", "Agent Session not found")
            except (ValueError, AgentSessionInvariantError) as exc:
                return _error(400, "AGENT.SESSION.INVALID_QUERY", str(exc))
        return None

    def handle_post(
        self,
        parts: list[str],
        *,
        body: bytes,
        identity: UserIdentity | None,
    ) -> ApiResponse | None:
        if not parts or parts[0] != "agent-sessions":
            return None
        if identity is None:
            return _error(401, "AUTH.MISSING", "authenticated identity is required")
        owner = identity.username
        if len(parts) == 1:
            payload, error = _json_body(body, code="AGENT.SESSION.INVALID_REQUEST")
            if error is not None:
                return error
            try:
                _closed_body(
                    payload,
                    {"request_key", "model_profile_id", "source"},
                    optional={"profile_id"},
                )
                session, created = self.service.create_session(
                    owner=owner,
                    request_key=_required_string(payload, "request_key", maximum=256),
                    model_profile_id=_required_protocol_id(payload, "model_profile_id"),
                    source=_source(payload.get("source")),
                    profile_id=(
                        "hpc-readonly-v1"
                        if payload.get("profile_id") is None
                        else _required_protocol_id(payload, "profile_id")
                    ),
                )
                return ApiResponse(
                    status=201 if created else 200,
                    payload=_session_payload(session),
                )
            except AgentSessionConflict as exc:
                return _error(409, "AGENT.SESSION.CONFLICT", str(exc))
            except (TypeError, ValueError, AgentSessionInvariantError) as exc:
                return _error(400, "AGENT.SESSION.INVALID_REQUEST", str(exc))
        if len(parts) == 3 and parts[2] == "turns":
            payload, error = _json_body(body, code="AGENT.TURN.INVALID_REQUEST")
            if error is not None:
                return error
            try:
                _closed_body(
                    payload,
                    {"request_key", "message", "expected_state_version"},
                )
                turn, created = self.service.submit_message(
                    session_id=parts[1],
                    owner=owner,
                    request_key=_required_string(payload, "request_key", maximum=256),
                    message=_required_string(payload, "message", maximum=64_000),
                    expected_state_version=_required_int(payload, "expected_state_version"),
                )
                return ApiResponse(
                    status=202 if created else 200,
                    payload=_turn_payload(turn),
                )
            except KeyError:
                return _error(404, "AGENT.SESSION.NOT_FOUND", "Agent Session not found")
            except AgentSessionConflict as exc:
                return _error(409, "AGENT.SESSION.CONFLICT", str(exc))
            except (TypeError, ValueError, AgentSessionInvariantError) as exc:
                return _error(400, "AGENT.TURN.INVALID_REQUEST", str(exc))
        if len(parts) == 5 and parts[2] == "turns" and parts[4] == "cancel":
            payload, error = _json_body(body, code="AGENT.TURN.INVALID_REQUEST")
            if error is not None:
                return error
            try:
                _closed_body(payload, {"expected_state_version"})
                turn = self.service.store.get_turn(parts[3], owner=owner)
                if turn.session_id != parts[1]:
                    raise KeyError(parts[3])
                cancelled = self.service.store.request_cancel(
                    turn.turn_id,
                    owner=owner,
                    expected_state_version=_required_int(
                        payload, "expected_state_version"
                    ),
                )
                return ApiResponse(status=200, payload=_turn_payload(cancelled))
            except KeyError:
                return _error(404, "AGENT.TURN.NOT_FOUND", "Agent Turn not found")
            except AgentSessionConflict as exc:
                return _error(409, "AGENT.TURN.CONFLICT", str(exc))
            except (TypeError, ValueError, AgentSessionInvariantError) as exc:
                return _error(400, "AGENT.TURN.INVALID_REQUEST", str(exc))
        return None


def _session_payload(session: AgentSessionRecord) -> dict[str, Any]:
    return {
        "session_id": session.session_id,
        "owner": session.owner,
        "request_key": session.request_key,
        "profile_id": session.profile_id,
        "model_profile_id": session.model_profile_id,
        "source": session.source,
        "state": session.state.value,
        "state_version": session.state_version,
        "resource_usage": session.resource_usage,
        "outcome": session.outcome,
        "created_at": session.created_at,
        "updated_at": session.updated_at,
    }


def _turn_payload(turn: AgentTurnRecord) -> dict[str, Any]:
    return {
        "turn_id": turn.turn_id,
        "session_id": turn.session_id,
        "owner": turn.owner,
        "request_key": turn.request_key,
        "message": turn.message,
        "state_version": turn.state_version,
        "state": turn.state.value,
        "cancel_requested": turn.cancel_requested,
        "event_sequence": turn.event_sequence,
        "error": _safe_value(turn.error),
        "created_at": turn.created_at,
        "started_at": turn.started_at,
        "finished_at": turn.finished_at,
    }


def _event_payload(event: AgentTurnEventRecord) -> dict[str, Any]:
    payload: dict[str, Any]
    checkpoint = event.payload.get("checkpoint")
    if event.event_type == "checkpoint":
        digest = checkpoint.get("digest") if isinstance(checkpoint, dict) else None
        payload = {"checkpoint_digest": digest} if isinstance(digest, str) else {}
    else:
        payload = _safe_value(event.payload)
        if not isinstance(payload, dict):
            payload = {}
    return {
        "event_id": event.event_id,
        "turn_id": event.turn_id,
        "session_id": event.session_id,
        "sequence": event.sequence,
        "event_type": event.event_type,
        "payload": payload,
        "created_at": event.created_at,
    }


def _safe_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _safe_value(item)
            for key, item in value.items()
            if key.lower() not in _AUTHORITY_KEYS and key != "checkpoint"
        }
    if isinstance(value, list):
        return [_safe_value(item) for item in value]
    return value


def _json_body(body: bytes, *, code: str) -> tuple[dict[str, Any], ApiResponse | None]:
    try:
        value = json.loads(body.decode() or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}, _error(400, code, "request body must be valid JSON")
    if not isinstance(value, dict):
        return {}, _error(400, code, "request body must be a JSON object")
    return value, None


def _closed_body(
    payload: Mapping[str, Any], required: set[str], *, optional: set[str] | None = None
) -> None:
    optional = optional or set()
    missing = sorted(required - set(payload))
    unknown = sorted(set(payload) - required - optional)
    if missing:
        raise ValueError(f"missing fields: {', '.join(missing)}")
    if unknown:
        raise ValueError(f"unsupported fields: {', '.join(unknown)}")


def _source(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("source must be an object")
    unknown = sorted(set(value) - _SOURCE_KEYS)
    if unknown:
        raise ValueError(f"unsupported source fields: {', '.join(unknown)}")
    source: dict[str, object] = {}
    for key, item in value.items():
        if key == "resource_envelope":
            if not isinstance(item, dict):
                raise ValueError("source.resource_envelope must be an object")
            source[key] = item
            continue
        if not isinstance(item, str) or _IDENTIFIER.fullmatch(item) is None:
            raise ValueError(f"source.{key} must be a valid identifier")
        source[key] = item
    return source


def _required_string(payload: Mapping[str, Any], key: str, *, maximum: int) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or "\0" in value:
        raise ValueError(f"{key} must be a non-empty string of at most {maximum} characters")
    return value.strip()


def _required_int(payload: Mapping[str, Any], key: str) -> int:
    value = payload.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{key} must be a positive integer")
    return value


def _required_protocol_id(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or _PROTOCOL_ID.fullmatch(value) is None:
        raise ValueError(f"{key} must be a valid protocol identifier")
    return value


def _query_states(params: Mapping[str, list[str]]) -> frozenset[AgentSessionState]:
    values = params.get("state", [])
    raw = [item.strip() for value in values for item in value.split(",") if item.strip()]
    return frozenset(AgentSessionState(item) for item in raw)


def _query_limit(params: Mapping[str, list[str]]) -> int:
    values = params.get("limit", [])
    if not values:
        return 50
    if len(values) != 1:
        raise ValueError("limit must be provided once")
    value = int(values[0])
    if value <= 0 or value > 100:
        raise ValueError("limit must be between 1 and 100")
    return value


def _query_cursor(
    params: Mapping[str, list[str]], *, scope: str
) -> CursorPosition | None:
    values = params.get("cursor", [])
    if not values:
        return None
    if len(values) != 1:
        raise ValueError("cursor must be provided once")
    return decode_cursor(value=values[0], kind="agent_sessions", scope=scope)


def _public_cursor(raw: str | None, *, scope: str) -> str | None:
    if raw is None:
        return None
    try:
        primary, secondary = raw.rsplit("|", 1)
    except ValueError as exc:
        raise ValueError("Store returned an invalid Session cursor") from exc
    return encode_cursor(
        kind="agent_sessions",
        scope=scope,
        position=CursorPosition(primary=primary, secondary=secondary),
    )


def _query_after_event_id(
    params: Mapping[str, list[str]], *, headers: Mapping[str, str] | None
) -> int:
    values = params.get("after_event_id", [])
    last_event_id = _header(headers, "last-event-id")
    if values and last_event_id is not None:
        raise ValueError("after_event_id and Last-Event-ID cannot both be provided")
    if not values and last_event_id is not None:
        values = [last_event_id]
    if not values:
        return 0
    if len(values) != 1:
        raise ValueError("after_event_id must be provided once")
    value = int(values[0])
    if value < 0:
        raise ValueError("after_event_id cannot be negative")
    return value


def _header(headers: Mapping[str, str] | None, name: str) -> str | None:
    if headers is None:
        return None
    return next((value for key, value in headers.items() if key.lower() == name), None)


def _reject_unknown_params(params: Mapping[str, list[str]], allowed: set[str]) -> None:
    unknown = sorted(set(params) - allowed)
    if unknown:
        raise ValueError(f"unsupported query parameters: {', '.join(unknown)}")


def _error(status: int, code: str, message: str) -> ApiResponse:
    return ApiResponse(status=status, payload={"error": {"code": code, "message": message}})
