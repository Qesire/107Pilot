"""HTTP boundary for persistent remediation sessions."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from pilot107.api.http_types import ApiResponse
from pilot107.core.identity import UserIdentity
from pilot107.core.pagination import (
    CursorError,
    CursorPosition,
    cursor_scope,
    decode_cursor,
    encode_cursor,
)
from pilot107.core.remediation import (
    RemediationBudget,
    RemediationConflict,
    RemediationInvariantError,
    RemediationState,
)
from pilot107.services.remediation_service import (
    RemediationService,
    RemediationServiceError,
    remediation_session_payload,
)


class RemediationRoutes:
    def __init__(self, service: RemediationService) -> None:
        self.service = service

    def handle_get(
        self,
        parts: list[str],
        *,
        params: Mapping[str, list[str]],
        identity: UserIdentity | None,
    ) -> ApiResponse | None:
        if len(parts) == 1 and parts[0] == "remediation-sessions":
            try:
                _reject_unknown_params(params, {"state", "limit", "cursor"})
                owner = _query_owner(params, identity)
                states = _query_states(params)
                limit = _query_limit(params)
                scope = cursor_scope(
                    "remediation_sessions",
                    {"owner": owner, "states": sorted(state.value for state in states)},
                )
                cursor = _query_cursor(params, scope=scope)
                items, next_position = self.service.remediation_store.list_sessions_page(
                    owner=owner,
                    states=states,
                    before=(cursor.primary, cursor.secondary) if cursor is not None else None,
                    limit=limit,
                )
                next_cursor = (
                    None
                    if next_position is None
                    else encode_cursor(
                        kind="remediation_sessions",
                        scope=scope,
                        position=CursorPosition(
                            primary=next_position[0],
                            secondary=next_position[1],
                        ),
                    )
                )
                return ApiResponse(
                    status=200,
                    payload={
                        "items": [remediation_session_payload(item) for item in items],
                        "page": {
                            "limit": limit,
                            "has_more": next_cursor is not None,
                            "next_cursor": next_cursor,
                        },
                    },
                )
            except (CursorError, ValueError, RemediationInvariantError) as exc:
                return _error(400, "REMEDIATION.INVALID_QUERY", str(exc))
        if len(parts) == 2 and parts[0] == "remediation-sessions":
            try:
                session = self.service.remediation_store.get_session(parts[1])
                owner = identity.username if identity is not None else session.owner
                return ApiResponse(status=200, payload=self.service.detail(parts[1], owner=owner))
            except KeyError:
                return _error(404, "REMEDIATION.NOT_FOUND", "remediation session not found")
            except RemediationServiceError as exc:
                return _service_error(exc)
        if (
            len(parts) == 3
            and parts[0] == "remediation-sessions"
            and parts[2] == "events"
        ):
            try:
                _reject_unknown_params(params, {"limit", "after_event_id"})
                session = self.service.remediation_store.get_session(parts[1])
                owner = identity.username if identity is not None else session.owner
                if session.owner != owner:
                    raise RemediationServiceError(
                        "session belongs to another owner",
                        code="AUTH.FORBIDDEN",
                    )
                limit = _query_limit(params)
                after_event_id = _query_after_event_id(params)
                events, next_event_id = self.service.remediation_store.list_events_page(
                    session.session_id,
                    after_event_id=after_event_id,
                    limit=limit,
                )
                return ApiResponse(
                    status=200,
                    payload={
                        "session_id": session.session_id,
                        "items": [
                            {
                                "event_id": event.event_id,
                                "event_type": event.event_type,
                                "payload": event.payload,
                                "created_at": event.created_at,
                            }
                            for event in events
                        ],
                        "page": {
                            "limit": limit,
                            "has_more": next_event_id is not None,
                            "next_after_event_id": next_event_id,
                            "last_event_id": (
                                events[-1].event_id if events else after_event_id
                            ),
                        },
                    },
                )
            except KeyError:
                return _error(404, "REMEDIATION.NOT_FOUND", "remediation session not found")
            except (ValueError, RemediationInvariantError) as exc:
                return _error(400, "REMEDIATION.INVALID_QUERY", str(exc))
            except RemediationServiceError as exc:
                return _service_error(exc)
        return None

    def handle_post(
        self,
        parts: list[str],
        *,
        body: bytes,
        identity: UserIdentity | None,
    ) -> ApiResponse | None:
        if len(parts) == 3 and parts[0] == "runs" and parts[2] == "remediation-sessions":
            payload, error = _json_body(body)
            if error is not None:
                return error
            try:
                run = self.service.run_store.get_run(parts[1])
                owner = identity.username if identity is not None else run.owner
                budget_payload = payload.get("budget", {})
                if not isinstance(budget_payload, dict):
                    raise ValueError("budget must be an object")
                session, created = self.service.create(
                    owner=owner,
                    source_run_id=parts[1],
                    request_key=_required_string(payload, "request_key"),
                    automation_policy=str(payload.get("automation_policy", "manual_approval")),
                    budget=RemediationBudget.from_payload(budget_payload),
                )
                return ApiResponse(
                    status=201 if created else 200,
                    payload=remediation_session_payload(session),
                )
            except KeyError:
                return _error(404, "REMEDIATION.RUN_NOT_FOUND", "source run not found")
            except (ValueError, RemediationInvariantError) as exc:
                return _error(400, "REMEDIATION.INVALID_REQUEST", str(exc))
            except RemediationServiceError as exc:
                return _service_error(exc)

        if len(parts) != 3 or parts[0] != "remediation-sessions":
            return None
        session_id, action = parts[1], parts[2]
        if action not in {"advance", "approve", "reject", "execute", "cancel", "takeover"}:
            return None
        payload, error = _json_body(body)
        if error is not None:
            return error
        try:
            session = self.service.remediation_store.get_session(session_id)
            actor = identity.username if identity is not None else session.owner
            if action == "advance":
                updated = self.service.advance(
                    session_id,
                    worker_id=f"api:{actor}",
                    provider=str(payload.get("provider", "none")),
                )
                return ApiResponse(status=200, payload=remediation_session_payload(updated))
            if action == "approve":
                updated = self.service.approve(
                    session_id,
                    proposal_id=_required_string(payload, "proposal_id"),
                    actor=actor,
                    expected_version=_required_int(payload, "expected_version"),
                    note=_optional_string(payload, "note"),
                )
                return ApiResponse(status=200, payload=remediation_session_payload(updated))
            if action == "reject":
                updated = self.service.reject(
                    session_id,
                    proposal_id=_required_string(payload, "proposal_id"),
                    actor=actor,
                    expected_version=_required_int(payload, "expected_version"),
                    note=_optional_string(payload, "note"),
                )
                return ApiResponse(status=200, payload=remediation_session_payload(updated))
            if action == "cancel":
                updated = self.service.cancel(
                    session_id,
                    actor=actor,
                    expected_version=_required_int(payload, "expected_version"),
                    note=_optional_string(payload, "note"),
                )
                return ApiResponse(status=200, payload=remediation_session_payload(updated))
            if action == "takeover":
                updated = self.service.takeover(
                    session_id,
                    actor=actor,
                    expected_version=_required_int(payload, "expected_version"),
                    note=_required_string(payload, "note"),
                )
                return ApiResponse(status=200, payload=remediation_session_payload(updated))
            updated, execution = self.service.execute(
                session_id,
                proposal_id=_required_string(payload, "proposal_id"),
                actor=actor,
                expected_version=_required_int(payload, "expected_version"),
                submit=_optional_bool(payload, "submit", True),
            )
            response = remediation_session_payload(updated)
            response["execution_id"] = execution.execution_id
            status = 202 if updated.state == RemediationState.EXECUTING else 200
            return ApiResponse(status=status, payload=response)
        except KeyError:
            return _error(404, "REMEDIATION.NOT_FOUND", "remediation resource not found")
        except RemediationConflict as exc:
            return _error(409, "REMEDIATION.CONFLICT", str(exc))
        except (ValueError, RemediationInvariantError) as exc:
            return _error(400, "REMEDIATION.INVALID_REQUEST", str(exc))
        except RemediationServiceError as exc:
            return _service_error(exc)


def _json_body(body: bytes) -> tuple[dict[str, Any], ApiResponse | None]:
    try:
        value = json.loads(body.decode() or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}, _error(400, "invalid_json", "request body must be valid JSON")
    if not isinstance(value, dict):
        return {}, _error(400, "invalid_json", "request body must be a JSON object")
    return value, None


def _query_owner(params: Mapping[str, list[str]], identity: UserIdentity | None) -> str:
    if identity is not None:
        return identity.username
    values = params.get("owner", [])
    if len(values) != 1 or not values[0].strip():
        raise ValueError("owner query parameter is required when authentication is disabled")
    return values[0].strip()


def _query_states(params: Mapping[str, list[str]]) -> tuple[RemediationState, ...]:
    values = params.get("state", [])
    if not values:
        return ()
    raw = [item.strip() for value in values for item in value.split(",") if item.strip()]
    return tuple(RemediationState(item) for item in raw)


def _query_limit(params: Mapping[str, list[str]]) -> int:
    values = params.get("limit", [])
    if not values:
        return 50
    if len(values) != 1:
        raise ValueError("limit must be provided once")
    return int(values[0])


def _query_after_event_id(params: Mapping[str, list[str]]) -> int:
    values = params.get("after_event_id", [])
    if not values:
        return 0
    if len(values) != 1:
        raise ValueError("after_event_id must be provided once")
    value = int(values[0])
    if value < 0:
        raise ValueError("after_event_id cannot be negative")
    return value


def _query_cursor(
    params: Mapping[str, list[str]],
    *,
    scope: str,
) -> CursorPosition | None:
    values = params.get("cursor", [])
    if not values:
        return None
    if len(values) != 1:
        raise ValueError("cursor must be provided once")
    return decode_cursor(
        value=values[0],
        kind="remediation_sessions",
        scope=scope,
    )


def _reject_unknown_params(params: Mapping[str, list[str]], allowed: set[str]) -> None:
    unknown = sorted(set(params) - allowed)
    if unknown:
        raise ValueError(f"unsupported query parameters: {', '.join(unknown)}")


def _required_string(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return value.strip()


def _required_int(payload: Mapping[str, Any], key: str) -> int:
    value = payload.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{key} must be an integer")
    return value


def _optional_string(payload: Mapping[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    return value


def _optional_bool(payload: Mapping[str, Any], key: str, default: bool) -> bool:
    value = payload.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"{key} must be a boolean")
    return value


def _service_error(exc: RemediationServiceError) -> ApiResponse:
    if exc.code == "AUTH.FORBIDDEN":
        return _error(403, exc.code, str(exc))
    if exc.code.endswith("NOT_FOUND"):
        return _error(404, exc.code, str(exc))
    if exc.code == "REMEDIATION.BUDGET_EXHAUSTED":
        return _error(409, exc.code, str(exc))
    return _error(400, exc.code, str(exc))


def _error(status: int, code: str, message: str) -> ApiResponse:
    return ApiResponse(status=status, payload={"error": {"code": code, "message": message}})
