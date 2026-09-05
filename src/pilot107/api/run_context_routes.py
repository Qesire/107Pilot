"""HTTP route for the evidence-first RunContext read model.

The canonical endpoint is ``GET /api/v1/runs/{run_id}/context``. The former
``/workspace`` path remains a read-only compatibility alias while clients
migrate; it must not be used by new product code because Workspace is reserved
for the Agent filesystem concept.
"""

from __future__ import annotations

from collections.abc import Mapping

from pilot107.api.http_types import ApiResponse
from pilot107.core.identity import UserIdentity
from pilot107.services.run_context_service import RunContextService


class RunContextRoutes:
    def __init__(self, service: RunContextService) -> None:
        self.service = service

    def handle_get(
        self,
        parts: list[str],
        *,
        params: Mapping[str, list[str]],
        identity: UserIdentity | None,
    ) -> ApiResponse | None:
        # Canonical: GET /api/v1/runs/{run_id}/context
        # Compatibility: GET /api/v1/runs/{run_id}/workspace
        if (
            len(parts) != 3
            or parts[0] != "runs"
            or parts[2] not in {"context", "workspace"}
        ):
            return None
        if params:
            return _error(
                400,
                "RUN_CONTEXT.INVALID_QUERY",
                "run context does not accept query parameters",
            )
        owner = identity.username if identity is not None else None
        try:
            payload = self.service.get(parts[1], owner=owner)
        except KeyError:
            return _error(404, "RUN_CONTEXT.NOT_FOUND", "run not found")
        except PermissionError:
            return _error(
                403,
                "RUN_CONTEXT.FORBIDDEN",
                "run is owned by another user",
            )
        return ApiResponse(status=200, payload=payload)


def _error(status: int, code: str, message: str) -> ApiResponse:
    return ApiResponse(
        status=status,
        payload={"error": {"code": code, "message": message}},
    )


__all__ = ["RunContextRoutes"]
