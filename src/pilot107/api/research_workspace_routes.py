"""HTTP routes for user-selected Research Workspace bindings."""

from __future__ import annotations

import json
from collections.abc import Mapping

from pilot107.api.http_types import ApiResponse
from pilot107.core.identity import UserIdentity
from pilot107.core.research_workspace import ResearchWorkspaceConflict
from pilot107.services.research_workspace_service import ResearchWorkspaceService


class ResearchWorkspaceRoutes:
    def __init__(self, service: ResearchWorkspaceService) -> None:
        self.service = service

    def handle_get(
        self,
        parts: list[str],
        *,
        params: Mapping[str, list[str]],
        identity: UserIdentity | None,
    ) -> ApiResponse | None:
        if not parts or parts[0] != "research-workspaces":
            return None
        if identity is None:
            return _error(401, "RESEARCH_WORKSPACE.AUTH_REQUIRED", "identity required")
        owner = identity.username

        if len(parts) == 1:
            if params:
                return _error(
                    400,
                    "RESEARCH_WORKSPACE.INVALID_QUERY",
                    "workspace list does not accept query parameters",
                )
            return ApiResponse(status=200, payload=self.service.list(owner=owner))

        if len(parts) == 2 and parts[1] == "lookup":
            object_type = _first(params, "object_type")
            object_id = _first(params, "object_id")
            if not object_type or not object_id:
                return _error(
                    400,
                    "RESEARCH_WORKSPACE.INVALID_QUERY",
                    "object_type and object_id are required",
                )
            try:
                payload = self.service.lookup(
                    owner=owner,
                    object_type=object_type,
                    object_id=object_id,
                )
            except (ValueError, KeyError) as exc:
                return _error(400, "RESEARCH_WORKSPACE.INVALID_QUERY", str(exc))
            return ApiResponse(status=200, payload=payload)

        if len(parts) == 2:
            if params:
                return _error(
                    400,
                    "RESEARCH_WORKSPACE.INVALID_QUERY",
                    "workspace detail does not accept query parameters",
                )
            try:
                payload = self.service.get(parts[1], owner=owner)
            except KeyError:
                return _error(404, "RESEARCH_WORKSPACE.NOT_FOUND", "workspace not found")
            return ApiResponse(status=200, payload=payload)

        return None

    def handle_post(
        self,
        parts: list[str],
        *,
        body: bytes,
        identity: UserIdentity | None,
    ) -> ApiResponse | None:
        if not parts or parts[0] != "research-workspaces":
            return None
        if identity is None:
            return _error(401, "RESEARCH_WORKSPACE.AUTH_REQUIRED", "identity required")
        owner = identity.username
        payload, error = _json_body(body)
        if error is not None:
            return error

        if len(parts) == 1:
            request_key = _string(payload, "request_key")
            title = _string(payload, "title")
            description = _string(payload, "description", default="")
            if not request_key or not title:
                return _error(
                    400,
                    "RESEARCH_WORKSPACE.INVALID_REQUEST",
                    "request_key and title are required",
                )
            try:
                result = self.service.create(
                    owner=owner,
                    request_key=request_key,
                    title=title,
                    description=description,
                )
            except (ValueError, ResearchWorkspaceConflict) as exc:
                return _error(409, "RESEARCH_WORKSPACE.CONFLICT", str(exc))
            return ApiResponse(status=201 if result["created"] else 200, payload=result)

        if len(parts) == 3 and parts[2] == "bindings":
            object_type = _string(payload, "object_type")
            object_id = _string(payload, "object_id")
            if not object_type or not object_id:
                return _error(
                    400,
                    "RESEARCH_WORKSPACE.INVALID_REQUEST",
                    "object_type and object_id are required",
                )
            try:
                result = self.service.bind_user_selected(
                    parts[1],
                    owner=owner,
                    object_type=object_type,
                    object_id=object_id,
                )
            except KeyError:
                return _error(404, "RESEARCH_WORKSPACE.NOT_FOUND", "workspace not found")
            except (ValueError, ResearchWorkspaceConflict) as exc:
                return _error(409, "RESEARCH_WORKSPACE.BINDING_CONFLICT", str(exc))
            return ApiResponse(status=201 if result["created"] else 200, payload=result)

        if len(parts) == 4 and parts[2] == "bindings" and parts[3] == "approve-suggestion":
            object_type = _string(payload, "object_type")
            object_id = _string(payload, "object_id")
            suggestion_ref = _string(payload, "suggestion_ref")
            if not object_type or not object_id or not suggestion_ref:
                return _error(
                    400,
                    "RESEARCH_WORKSPACE.INVALID_REQUEST",
                    "object_type, object_id and suggestion_ref are required",
                )
            try:
                result = self.service.approve_agent_suggestion(
                    parts[1],
                    owner=owner,
                    object_type=object_type,
                    object_id=object_id,
                    suggestion_ref=suggestion_ref,
                )
            except KeyError:
                return _error(404, "RESEARCH_WORKSPACE.NOT_FOUND", "workspace not found")
            except (ValueError, ResearchWorkspaceConflict) as exc:
                return _error(409, "RESEARCH_WORKSPACE.BINDING_CONFLICT", str(exc))
            return ApiResponse(status=201 if result["created"] else 200, payload=result)

        return None


def _json_body(body: bytes) -> tuple[dict[str, object], ApiResponse | None]:
    try:
        value = json.loads(body.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}, _error(400, "RESEARCH_WORKSPACE.INVALID_JSON", "invalid JSON body")
    if not isinstance(value, dict):
        return {}, _error(400, "RESEARCH_WORKSPACE.INVALID_JSON", "JSON body must be an object")
    return value, None


def _string(payload: Mapping[str, object], key: str, *, default: str = "") -> str:
    value = payload.get(key, default)
    return value if isinstance(value, str) else ""


def _first(params: Mapping[str, list[str]], key: str) -> str | None:
    values = params.get(key)
    return values[0] if values else None


def _error(status: int, code: str, message: str) -> ApiResponse:
    return ApiResponse(status=status, payload={"error": {"code": code, "message": message}})
