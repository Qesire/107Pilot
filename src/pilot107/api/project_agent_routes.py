"""Owner-scoped HTTP routes for isolated Agent experiment projects."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Protocol

from pilot107.agent.project import ProjectConflict, blueprint_from_payload
from pilot107.agent.publisher import WorkspacePublicationState, publication_payload
from pilot107.agent.sandbox import SandboxPolicyError
from pilot107.agent.workspace import WorkspaceConflict, WorkspacePolicyError, change_set_payload
from pilot107.api.http_types import ApiResponse
from pilot107.core.identity import UserIdentity
from pilot107.core.remediation import RemediationConflict
from pilot107.services.project_agent_service import (
    FormalProjectRun,
    ProjectAgentService,
    formal_project_run_payload,
    formal_run_approval_payload,
    project_view_payload,
)
from pilot107.services.remediation_service import RemediationServiceError


class FormalRunObserver(Protocol):
    def preflight_formal_project_run(
        self,
        *,
        project_id: str,
        workspace_id: str,
        change_set_id: str,
        agent_session_id: str,
        actor: str,
    ) -> object: ...

    def observe_formal_project_run(
        self,
        *,
        project_id: str,
        workspace_id: str,
        change_set_id: str,
        agent_session_id: str,
        actor: str,
        formal: FormalProjectRun,
    ) -> object: ...


class ProjectAgentRoutes:
    def __init__(
        self,
        service: ProjectAgentService,
        *,
        formal_run_observer: FormalRunObserver | None = None,
    ) -> None:
        self.service = service
        self.formal_run_observer = formal_run_observer

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
        if not parts or parts[0] not in {"agent-projects", "agent-workspaces", "agent-changesets"}:
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
        if len(parts) == 3 and parts[0] == "agent-changesets" and parts[2] == "publish":
            payload, error = _body(body)
            if error is not None:
                return error
            try:
                _closed(
                    payload,
                    {"project_id", "workspace_id", "expected_version", "approved_digest"},
                    {"target_root"},
                )
                publication = self.service.publish_change_set(
                    project_id=_required_string(payload, "project_id"),
                    workspace_id=_required_string(payload, "workspace_id"),
                    owner=owner,
                    change_set_id=parts[1],
                    expected_version=_required_integer(payload, "expected_version"),
                    approved_digest=_required_string(payload, "approved_digest"),
                    target_root=_optional_string(payload.get("target_root")),
                )
                status = (
                    409
                    if publication.state is WorkspacePublicationState.CONFLICTED
                    else 200
                )
                response = publication_payload(publication)
                if status == 409:
                    response["error"] = {
                        "code": "AGENT.WORKSPACE.CONFLICT",
                        "message": "Workspace source changed before publication",
                    }
                return ApiResponse(status=status, payload=response)
            except KeyError:
                return _error(404, "AGENT.PROJECT.NOT_FOUND", "Agent Project not found")
            except ProjectConflict as exc:
                return _error(409, "AGENT.PROJECT.CONFLICT", str(exc))
            except RuntimeError as exc:
                if str(exc) == "Workspace publisher is unavailable":
                    return _error(503, "AGENT.PUBLISHER.UNAVAILABLE", str(exc))
                raise
            except (TypeError, ValueError, WorkspacePolicyError) as exc:
                return _error(400, "AGENT.WORKSPACE.INVALID_REQUEST", str(exc))
        if (
            len(parts) == 3
            and parts[0] == "agent-changesets"
            and parts[2] in {"formal-preview", "formal-submit"}
        ):
            payload, error = _body(body)
            if error is not None:
                return error
            try:
                required = {
                    "project_id",
                    "workspace_id",
                    "session_id",
                    "validation_contract_id",
                    "validation_run_id",
                    "validation_evidence_refs",
                    "formal_contract",
                }
                if parts[2] == "formal-submit":
                    required.add("approved_digest")
                _closed(payload, required)
                raw_refs = payload["validation_evidence_refs"]
                raw_contract = payload["formal_contract"]
                if not isinstance(raw_refs, list) or not all(
                    isinstance(item, str) and item for item in raw_refs
                ):
                    raise TypeError("validation_evidence_refs must contain strings")
                if not isinstance(raw_contract, Mapping):
                    raise TypeError("formal_contract must be an object")
                project_id = _required_string(payload, "project_id")
                workspace_id = _required_string(payload, "workspace_id")
                session_id = _required_string(payload, "session_id")
                validation_contract_id = _required_string(
                    payload, "validation_contract_id"
                )
                validation_run_id = _required_string(payload, "validation_run_id")
                evidence_refs = tuple(raw_refs)
                contract_value = dict(raw_contract)
                if parts[2] == "formal-preview":
                    approval = self.service.prepare_formal_run(
                        project_id=project_id,
                        workspace_id=workspace_id,
                        change_set_id=parts[1],
                        owner=owner,
                        session_id=session_id,
                        validation_contract_id=validation_contract_id,
                        validation_run_id=validation_run_id,
                        validation_evidence_refs=evidence_refs,
                        formal_contract_payload=contract_value,
                    )
                    return ApiResponse(
                        status=200, payload=formal_run_approval_payload(approval)
                    )
                if self.formal_run_observer is not None:
                    self.formal_run_observer.preflight_formal_project_run(
                        project_id=project_id,
                        workspace_id=workspace_id,
                        change_set_id=parts[1],
                        agent_session_id=session_id,
                        actor=owner,
                    )
                formal = self.service.approve_and_submit_formal_run(
                    project_id=project_id,
                    workspace_id=workspace_id,
                    change_set_id=parts[1],
                    owner=owner,
                    session_id=session_id,
                    validation_contract_id=validation_contract_id,
                    validation_run_id=validation_run_id,
                    validation_evidence_refs=evidence_refs,
                    formal_contract_payload=contract_value,
                    approved_digest=_required_string(payload, "approved_digest"),
                )
                if self.formal_run_observer is not None:
                    self.formal_run_observer.observe_formal_project_run(
                        project_id=project_id,
                        workspace_id=workspace_id,
                        change_set_id=parts[1],
                        agent_session_id=session_id,
                        actor=owner,
                        formal=formal,
                    )
                return ApiResponse(status=201, payload=formal_project_run_payload(formal))
            except KeyError:
                return _error(404, "AGENT.PROJECT.NOT_FOUND", "Agent Project not found")
            except RemediationConflict as exc:
                return _error(409, "REMEDIATION.CONFLICT", str(exc))
            except RemediationServiceError as exc:
                status = 403 if exc.code == "AUTH.FORBIDDEN" else 409
                return _error(status, exc.code, str(exc))
            except RuntimeError as exc:
                if str(exc) == "formal Project Run services are unavailable":
                    return _error(503, "AGENT.FORMAL_RUN.UNAVAILABLE", str(exc))
                raise
            except ProjectConflict as exc:
                return _error(409, "AGENT.PROJECT.CONFLICT", str(exc))
            except (TypeError, ValueError) as exc:
                return _error(400, "AGENT.FORMAL_RUN.INVALID_REQUEST", str(exc))
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
