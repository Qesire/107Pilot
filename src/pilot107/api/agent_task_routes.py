"""Owner-scoped HTTP routes for durable AgentTask lifecycle records."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from pilot107.agent.tasks import AgentTaskConflict, agent_task_payload
from pilot107.api.http_types import ApiResponse
from pilot107.core.identity import UserIdentity
from pilot107.services.agent_task_service import AgentTaskService


class AgentTaskRoutes:
    def __init__(self, service: AgentTaskService) -> None:
        self.service = service

    def handle_get(
        self,
        parts: list[str],
        *,
        params: Mapping[str, list[str]],
        identity: UserIdentity | None,
    ) -> ApiResponse | None:
        if identity is None:
            if _matches_get(parts):
                return _error(401, "AUTH.MISSING", "authenticated identity is required")
            return None
        owner = identity.username
        if len(parts) == 3 and parts[0] == "agent-sessions" and parts[2] == "tasks":
            try:
                _reject_query(params)
                self.service.session_store.get_session(parts[1], owner=owner)
                tasks = self.service.store.list_tasks(
                    owner=owner,
                    session_id=parts[1],
                )
                return ApiResponse(
                    status=200,
                    payload={"items": [agent_task_payload(task) for task in tasks]},
                )
            except ValueError as exc:
                return _error(400, "AGENT.TASK.INVALID_QUERY", str(exc))
            except KeyError:
                return _error(404, "AGENT.SESSION.NOT_FOUND", "Agent Session not found")
        if len(parts) == 2 and parts[0] == "agent-tasks":
            try:
                _reject_query(params)
                task = self.service.store.get_task(parts[1], owner=owner)
                return ApiResponse(status=200, payload=agent_task_payload(task))
            except KeyError:
                return _error(404, "AGENT.TASK.NOT_FOUND", "AgentTask not found")
            except ValueError as exc:
                return _error(400, "AGENT.TASK.INVALID_QUERY", str(exc))
        return None

    def handle_post(
        self,
        parts: list[str],
        *,
        body: bytes,
        identity: UserIdentity | None,
    ) -> ApiResponse | None:
        if len(parts) != 3 or parts[0] != "agent-tasks" or parts[2] != "cancel":
            return None
        if identity is None:
            return _error(401, "AUTH.MISSING", "authenticated identity is required")
        payload, error = _json_body(body)
        if error is not None:
            return error
        try:
            _closed_body(payload, {"expected_version"})
            task = self.service.request_cancel(
                parts[1],
                owner=identity.username,
                expected_version=_required_version(payload, "expected_version"),
            )
            return ApiResponse(status=200, payload=agent_task_payload(task))
        except KeyError:
            return _error(404, "AGENT.TASK.NOT_FOUND", "AgentTask not found")
        except AgentTaskConflict as exc:
            return _error(409, "AGENT.TASK.CONFLICT", str(exc))
        except (TypeError, ValueError) as exc:
            return _error(400, "AGENT.TASK.INVALID_REQUEST", str(exc))


def _matches_get(parts: list[str]) -> bool:
    return (len(parts) == 2 and parts[0] == "agent-tasks") or (
        len(parts) == 3 and parts[0] == "agent-sessions" and parts[2] == "tasks"
    )


def _reject_query(params: Mapping[str, list[str]]) -> None:
    if params:
        raise ValueError(f"unsupported query parameters: {', '.join(sorted(params))}")


def _json_body(body: bytes) -> tuple[dict[str, Any], ApiResponse | None]:
    try:
        value = json.loads(body.decode() or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}, _error(400, "AGENT.TASK.INVALID_REQUEST", "request body must be valid JSON")
    if not isinstance(value, dict):
        return {}, _error(400, "AGENT.TASK.INVALID_REQUEST", "request body must be an object")
    return value, None


def _closed_body(payload: Mapping[str, Any], required: set[str]) -> None:
    missing = sorted(required - set(payload))
    unknown = sorted(set(payload) - required)
    if missing:
        raise ValueError(f"missing fields: {', '.join(missing)}")
    if unknown:
        raise ValueError(f"unsupported fields: {', '.join(unknown)}")


def _required_version(payload: Mapping[str, Any], key: str) -> int:
    value = payload.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{key} must be a non-negative integer")
    return value


def _error(status: int, code: str, message: str) -> ApiResponse:
    return ApiResponse(status=status, payload={"error": {"code": code, "message": message}})
