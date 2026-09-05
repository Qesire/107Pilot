"""HTTP boundary for repair tickets and artifact manifests (M2)."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from pilot107.api.http_types import ApiResponse
from pilot107.core.identity import UserIdentity
from pilot107.core.repair_ticket import RepairTicketInvariantError, RepairTicketState
from pilot107.services.repair_ticket_service import (
    RepairTicketService,
    RepairTicketServiceError,
)


class RepairTicketRoutes:
    def __init__(self, service: RepairTicketService) -> None:
        self.service = service

    def handle_get(
        self,
        parts: list[str],
        *,
        params: Mapping[str, list[str]],
        identity: UserIdentity | None,
    ) -> ApiResponse | None:
        # GET /api/v1/repair-tickets
        if len(parts) == 1 and parts[0] == "repair-tickets":
            try:
                _reject_unknown_params(params, {"state", "session_id", "limit", "cursor"})
                owner = _query_owner(params, identity)
                states = _query_states(params)
                session_id = _first_param(params, "session_id")
                limit = _query_limit(params)
                items, next_position = self.service.repair_ticket_store.list_tickets_page(
                    owner=owner,
                    states=states,
                    session_id=session_id,
                    limit=limit,
                )
                return ApiResponse(
                    status=200,
                    payload={
                        "items": [item.to_payload() for item in items],
                        "page": {
                            "limit": limit,
                            "has_more": next_position is not None,
                            "next_cursor": (
                                f"{next_position[0]}|{next_position[1]}" if next_position else None
                            ),
                        },
                    },
                )
            except (ValueError, RepairTicketInvariantError) as exc:
                return _error(400, "REPAIR_TICKET.INVALID_QUERY", str(exc))
        # GET /api/v1/repair-tickets/{ticket_id}
        if len(parts) == 2 and parts[0] == "repair-tickets":
            try:
                owner = identity.username if identity is not None else ""
                return ApiResponse(
                    status=200,
                    payload=self.service.detail(parts[1], owner=owner),
                )
            except RepairTicketServiceError as exc:
                return _service_error(exc)
        # GET /api/v1/runs/{run_id}/artifact-manifests
        if len(parts) == 3 and parts[0] == "runs" and parts[2] == "artifact-manifests":
            try:
                _reject_unknown_params(params, set())
                manifests = self.service.repair_ticket_store.list_manifests_for_run(parts[1])
                return ApiResponse(
                    status=200,
                    payload={"items": [m.to_payload() for m in manifests]},
                )
            except ValueError as exc:
                return _error(400, "REPAIR_TICKET.INVALID_QUERY", str(exc))
        return None

    def handle_post(
        self,
        parts: list[str],
        *,
        body: bytes,
        identity: UserIdentity | None,
    ) -> ApiResponse | None:
        # POST /api/v1/repair-tickets
        if len(parts) == 1 and parts[0] == "repair-tickets":
            payload, error = _json_body(body)
            if error is not None:
                return error
            try:
                owner = identity.username if identity is not None else ""
                session_id = _optional_string(payload, "session_id")
                source_run_id = _optional_string(payload, "source_run_id")
                request_key = _required_string(payload, "request_key")
                if session_id:
                    ticket, created = self.service.create_from_session(
                        session_id, owner=owner, request_key=request_key
                    )
                elif source_run_id:
                    ticket, created = self.service.create_direct(
                        owner=owner,
                        source_run_id=source_run_id,
                        request_key=request_key,
                        requested_change=_optional_string(payload, "requested_change"),
                    )
                else:
                    return _error(
                        400,
                        "REPAIR_TICKET.INVALID_REQUEST",
                        "either session_id or source_run_id is required",
                    )
                return ApiResponse(
                    status=201 if created else 200,
                    payload=ticket.to_payload(),
                )
            except (ValueError, RepairTicketInvariantError) as exc:
                return _error(400, "REPAIR_TICKET.INVALID_REQUEST", str(exc))
            except RepairTicketServiceError as exc:
                return _service_error(exc)
        # POST /api/v1/repair-tickets/{ticket_id}/resolve
        if len(parts) == 3 and parts[0] == "repair-tickets" and parts[2] == "resolve":
            payload, error = _json_body(body)
            if error is not None:
                return error
            try:
                owner = identity.username if identity is not None else ""
                ticket = self.service.resolve(
                    parts[1],
                    owner=owner,
                    manifest_id=_required_string(payload, "manifest_id"),
                    derived_run_id=_required_string(payload, "derived_run_id"),
                )
                return ApiResponse(status=200, payload=ticket.to_payload())
            except (ValueError, RepairTicketInvariantError) as exc:
                return _error(400, "REPAIR_TICKET.INVALID_REQUEST", str(exc))
            except RepairTicketServiceError as exc:
                return _service_error(exc)
        # POST /api/v1/repair-tickets/{ticket_id}/abandon
        if len(parts) == 3 and parts[0] == "repair-tickets" and parts[2] == "abandon":
            payload, error = _json_body(body)
            if error is not None:
                return error
            try:
                owner = identity.username if identity is not None else ""
                ticket = self.service.abandon(
                    parts[1],
                    owner=owner,
                    reason=_optional_string(payload, "reason"),
                )
                return ApiResponse(status=200, payload=ticket.to_payload())
            except (ValueError, RepairTicketInvariantError) as exc:
                return _error(400, "REPAIR_TICKET.INVALID_REQUEST", str(exc))
            except RepairTicketServiceError as exc:
                return _service_error(exc)
        # POST /api/v1/artifact-manifests
        if len(parts) == 1 and parts[0] == "artifact-manifests":
            payload, error = _json_body(body)
            if error is not None:
                return error
            try:
                owner = identity.username if identity is not None else ""
                manifest = self.service.create_manifest(
                    owner=owner,
                    revision=_required_string(payload, "revision"),
                    run_id=_optional_string(payload, "run_id"),
                    dirty_diff_digest=_optional_string(payload, "dirty_diff_digest"),
                    bundle_digest=_optional_string(payload, "bundle_digest"),
                    remote_workdir=_optional_string(payload, "remote_workdir"),
                    local_test_summary=_optional_string(payload, "local_test_summary"),
                    disclosure=str(payload.get("disclosure", "metadata_only")),
                )
                return ApiResponse(status=201, payload=manifest.to_payload())
            except (ValueError, RepairTicketInvariantError) as exc:
                return _error(400, "REPAIR_TICKET.INVALID_REQUEST", str(exc))
            except RepairTicketServiceError as exc:
                return _service_error(exc)
        return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _json_body(body: bytes) -> tuple[dict[str, Any], ApiResponse | None]:
    if not body:
        return {}, None
    try:
        parsed = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}, _error(400, "INVALID_JSON", "request body is not valid JSON")
    if not isinstance(parsed, dict):
        return {}, _error(400, "INVALID_JSON", "request body must be a JSON object")
    return parsed, None


def _query_owner(params: Mapping[str, list[str]], identity: UserIdentity | None) -> str:
    if identity is not None:
        return identity.username
    values = params.get("owner", [])
    if values and values[0].strip():
        return values[0].strip()
    return ""


def _query_states(
    params: Mapping[str, list[str]],
) -> frozenset[RepairTicketState] | None:
    values = params.get("state", [])
    if not values:
        return None
    states: set[RepairTicketState] = set()
    for raw in values:
        for part in raw.split(","):
            part = part.strip()
            if part:
                states.add(RepairTicketState(part))
    return frozenset(states) if states else None


def _query_limit(params: Mapping[str, list[str]]) -> int:
    values = params.get("limit", [])
    if not values:
        return 20
    try:
        limit = int(values[0])
    except ValueError:
        raise ValueError("limit must be an integer") from None
    return max(1, min(limit, 100))


def _first_param(params: Mapping[str, list[str]], key: str) -> str | None:
    values = params.get(key, [])
    if values and values[0].strip():
        return values[0].strip()
    return None


def _reject_unknown_params(params: Mapping[str, list[str]], allowed: set[str]) -> None:
    unknown = sorted(set(params) - allowed)
    if unknown:
        raise ValueError(f"unsupported query parameters: {', '.join(unknown)}")


def _required_string(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return value.strip()


def _optional_string(payload: Mapping[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    return value


def _service_error(exc: RepairTicketServiceError) -> ApiResponse:
    if exc.code == "AUTH.FORBIDDEN":
        return _error(403, exc.code, str(exc))
    if "NOT_FOUND" in exc.code:
        return _error(404, exc.code, str(exc))
    return _error(400, exc.code, str(exc))


def _error(status: int, code: str, message: str) -> ApiResponse:
    return ApiResponse(status=status, payload={"error": {"code": code, "message": message}})
