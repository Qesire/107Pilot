"""Owner-scoped HTTP routes for isolated Agent experiment projects."""

from __future__ import annotations

import json
from collections.abc import Mapping

from pilot107.agent.project import ProjectConflict, blueprint_from_payload
from pilot107.agent.sandbox import SandboxPolicyError
from pilot107.agent.workspace import WorkspaceConflict, WorkspacePolicyError, change_set_payload
from pilot107.api.http_types import ApiResponse
from pilot107.core.identity import UserIdentity
from pilot107.services.project_agent_service import (
    ProjectAgentService,
    project_view_payload,
)


class ProjectAgentRoutes:
    def __init__(self, service: ProjectAgentService) -> None:
        self.service = service

    def handle_get(
        self,
        parts: list[str],
        *,
        params: Mapping[str, list[str]],
        identity: UserIdentity | None,
    ) -> ApiResponse | None:
        if not parts or parts[0] not in {"agent-projects", "agent-changesets"}:
            return None
        if identity is None:
            return _error(401, "AUTH.MISSING", "authenticated identity is required")
        owner = identity.username
        try:
            if parts == ["agent-projects"]:
                _no_params(params)
                return ApiResponse(
                    status=200,
                    payload={
                        "items": [
                            project_view_payload(item)
                            for item in self.service.list_projects(owner=owner)
                        ]
                    },
                )
            if len(parts) == 2 and parts[0] == "agent-projects":
                _no_params(params)
                return ApiResponse(
                    status=200,
                    payload=project_view_payload(
                        self.service.get_project(parts[1], owner=owner)
                    ),
                )
            if len(parts) == 3 and parts[0] == "agent-changesets" and parts[2] == "diff":
                _exact_params(params, {"project_id", "workspace_id"})
                diff = self.service.get_diff(
                    parts[1],
                    owner=owner,
                    project_id=_single_param(params, "project_id"),
                    workspace_id=_single_param(params, "workspace_id"),
                )
                return ApiResponse(
                    status=200,
                    payload={"change_set_id": parts[1], "unified_diff": diff},
                )
        except KeyError:
            return _error(404, "AGENT.PROJECT.NOT_FOUND", "Agent Project not found")
        except ValueError as exc:
            return _error(400, "AGENT.PROJECT.INVALID_REQUEST", str(exc))
        return None

    def handle_post(
        self,
        parts: list[str],
        *,
        body: bytes,
        identity: UserIdentity | None,
    ) -> ApiResponse | None:
        if not parts or parts[0] not in {"agent-projects", "agent-workspaces"}:
            return None
        if identity is None:
            return _error(401, "AUTH.MISSING", "authenticated identity is required")
        owner = identity.username
        if parts == ["agent-projects"]:
            payload, error = _body(body)
            if error is not None:
                return error
            try:
                _closed(payload, {"origin", "goal", "request_key"}, {"source_ref"})
                view = self.service.create_project(
                    owner=owner,
                    origin=_required_string(payload, "origin"),
                    goal=_required_string(payload, "goal"),
                    request_key=_required_string(payload, "request_key"),
                    source_ref=_optional_string(payload.get("source_ref")),
                )
                return ApiResponse(status=201, payload=project_view_payload(view))
            except ProjectConflict as exc:
                return _error(409, "AGENT.PROJECT.CONFLICT", str(exc))
            except (TypeError, ValueError, RuntimeError) as exc:
                return _error(400, "AGENT.PROJECT.INVALID_REQUEST", str(exc))
        if len(parts) == 3 and parts[0] == "agent-projects" and parts[2] == "blueprint":
            payload, error = _body(body)
            if error is not None:
                return error
            try:
                _closed(payload, {"expected_version", "blueprint"})
                raw_blueprint = payload["blueprint"]
                if not isinstance(raw_blueprint, Mapping):
                    raise TypeError("blueprint must be an object")
                view = self.service.save_blueprint(
                    parts[1],
                    owner=owner,
                    expected_version=_required_integer(payload, "expected_version"),
                    blueprint=blueprint_from_payload(raw_blueprint),
                )
                return ApiResponse(status=200, payload=project_view_payload(view))
            except KeyError:
                return _error(404, "AGENT.PROJECT.NOT_FOUND", "Agent Project not found")
            except ProjectConflict as exc:
                return _error(409, "AGENT.PROJECT.CONFLICT", str(exc))
            except (TypeError, ValueError) as exc:
                return _error(400, "AGENT.PROJECT.INVALID_REQUEST", str(exc))
        if len(parts) == 3 and parts[0] == "agent-workspaces" and parts[2] == "patch":
            payload, error = _body(body)
            if error is not None:
                return error
            try:
                _closed(
                    payload,
                    {"project_id", "path", "expected_source_digest", "operation", "content"},
                )
                change_set = self.service.apply_patch(
                    project_id=_required_string(payload, "project_id"),
                    workspace_id=parts[1],
                    owner=owner,
                    relative_path=_required_string(payload, "path"),
                    expected_source_digest=_optional_string(payload["expected_source_digest"]),
                    operation=_required_string(payload, "operation"),
                    content=_optional_string(payload["content"]),
                )
                return ApiResponse(status=201, payload=change_set_payload(change_set))
            except KeyError:
                return _error(404, "AGENT.PROJECT.NOT_FOUND", "Agent Project not found")
            except WorkspaceConflict as exc:
                return _error(409, "AGENT.WORKSPACE.CONFLICT", str(exc))
            except (TypeError, ValueError, WorkspacePolicyError) as exc:
                return _error(400, "AGENT.WORKSPACE.INVALID_REQUEST", str(exc))
        if len(parts) == 3 and parts[0] == "agent-workspaces" and parts[2] == "sandbox":
            payload, error = _body(body)
            if error is not None:
                return error
            try:
                _closed(payload, {"project_id", "change_set_id", "argv", "timeout"})
                argv = payload["argv"]
                if not isinstance(argv, list) or not all(isinstance(item, str) for item in argv):
                    raise TypeError("argv must be an array of strings")
                timeout = payload["timeout"]
                if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
                    raise TypeError("timeout must be a number")
                result = self.service.execute_sandbox(
                    project_id=_required_string(payload, "project_id"),
                    workspace_id=parts[1],
                    owner=owner,
                    change_set_id=_required_string(payload, "change_set_id"),
                    argv=tuple(argv),
                    timeout=timeout,
                )
                return ApiResponse(
                    status=200,
                    payload={
                        "result_id": result.result_id,
                        "status": result.status,
                        "exit_code": result.exit_code,
                        "stdout": result.stdout,
                        "stderr": result.stderr,
                        "stdout_sha256": result.stdout_sha256,
                        "stderr_sha256": result.stderr_sha256,
                        "limit_reason": result.limit_reason,
                    },
                )
            except KeyError:
                return _error(404, "AGENT.PROJECT.NOT_FOUND", "Agent Project not found")
            except (TypeError, ValueError, SandboxPolicyError) as exc:
                return _error(400, "AGENT.SANDBOX.INVALID_REQUEST", str(exc))
        return None


def _body(body: bytes) -> tuple[dict[str, object], ApiResponse | None]:
    if len(body) > 1024 * 1024:
        return {}, _error(413, "AGENT.PROJECT.REQUEST_TOO_LARGE", "request is too large")
    try:
        value = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeError):
        return {}, _error(400, "AGENT.PROJECT.INVALID_REQUEST", "invalid JSON body")
    if not isinstance(value, dict):
        return {}, _error(400, "AGENT.PROJECT.INVALID_REQUEST", "body must be an object")
    return value, None


def _closed(
    payload: Mapping[str, object],
    required: set[str],
    optional: set[str] | None = None,
) -> None:
    optional = optional or set()
    if not required.issubset(payload) or set(payload) - required - optional:
        raise ValueError("request body is not a closed object")


def _required_string(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value or len(value) > 64_000 or "\0" in value:
        raise ValueError(f"{key} is invalid")
    return value


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 64_000 or "\0" in value:
        raise ValueError("optional string is invalid")
    return value


def _required_integer(payload: Mapping[str, object], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{key} is invalid")
    return value


def _no_params(params: Mapping[str, list[str]]) -> None:
    if params:
        raise ValueError("query parameters are not supported")


def _exact_params(params: Mapping[str, list[str]], expected: set[str]) -> None:
    if set(params) != expected:
        raise ValueError("query parameters are invalid")


def _single_param(params: Mapping[str, list[str]], key: str) -> str:
    values = params.get(key)
    if values is None or len(values) != 1 or not values[0]:
        raise ValueError(f"{key} query parameter is invalid")
    return values[0]


def _error(status: int, code: str, message: str) -> ApiResponse:
    return ApiResponse(status=status, payload={"error": {"code": code, "message": message}})
