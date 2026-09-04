"""HTTP routes for evidence-first Run workspace read models."""

from __future__ import annotations

from collections.abc import Mapping

from pilot107.api.http_types import ApiResponse
from pilot107.core.identity import UserIdentity
from pilot107.services.repair_workspace_service import RepairWorkspaceService
from pilot107.services.run_workspace_service import RunWorkspaceService


class RunWorkspaceRoutes:
    def __init__(
        self,
        service: RunWorkspaceService,
        repair_service: RepairWorkspaceService | None = None,
    ) -> None:
        self.service = service
        self.repair_service = repair_service or RepairWorkspaceService(service.store)

    def handle_get(
        self,
        parts: list[str],
        *,
        params: Mapping[str, list[str]],
        identity: UserIdentity | None,
    ) -> ApiResponse | None:
        # GET /api/v1/runs/{run_id}/workspace
        # GET /api/v1/runs/{run_id}/repair-workspace
        if len(parts) != 3 or parts[0] != "runs" or parts[2] not in {
            "workspace",
            "repair-workspace",
        }:
            return None
        repair = parts[2] == "repair-workspace"
        prefix = "REPAIR_WORKSPACE" if repair else "RUN_WORKSPACE"
        if params:
            return _error(
                400,
                f"{prefix}.INVALID_QUERY",
                f"{parts[2]} does not accept query parameters",
            )
        owner = identity.username if identity is not None else None
        try:
            payload = (
                self.repair_service.get(parts[1], owner=owner)
                if repair
                else self.service.get(parts[1], owner=owner)
            )
        except KeyError:
            return _error(404, f"{prefix}.NOT_FOUND", "run not found")
        except PermissionError:
            return _error(403, f"{prefix}.FORBIDDEN", "run is owned by another user")
        return ApiResponse(status=200, payload=payload)


def _error(status: int, code: str, message: str) -> ApiResponse:
    return ApiResponse(
        status=status,
        payload={"error": {"code": code, "message": message}},
    )
