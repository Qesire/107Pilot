"""HTTP routes for Run workspaces and user-selected Research Workspaces."""

from __future__ import annotations

from collections.abc import Mapping

from pilot107.api.http_types import ApiResponse
from pilot107.api.research_workspace_routes import ResearchWorkspaceRoutes
from pilot107.core.identity import UserIdentity
from pilot107.core.research_workspace import SQLiteResearchWorkspaceStore
from pilot107.services.research_workspace_service import ResearchWorkspaceService
from pilot107.services.run_workspace_service import RunWorkspaceService


class RunWorkspaceRoutes:
    """Existing Run route plus a compatibility composition point for workspace APIs.

    ``Pilot107HttpApi`` already delegates GET routing to this object.  Until the
    larger HTTP composition root is refactored, the Research Workspace API is
    attached here without changing Run semantics.  SQLite can be discovered
    from the existing RunStore ``db_path``; PostgreSQL deliberately fails closed
    until a native ResearchWorkspaceStore is injected.
    """

    def __init__(
        self,
        service: RunWorkspaceService,
        *,
        research_routes: ResearchWorkspaceRoutes | None = None,
    ) -> None:
        self.service = service
        self.research_routes = research_routes or _sqlite_research_routes(service)

    def handle_get(
        self,
        parts: list[str],
        *,
        params: Mapping[str, list[str]],
        identity: UserIdentity | None,
    ) -> ApiResponse | None:
        if parts and parts[0] == "research-workspaces":
            if self.research_routes is None:
                return _research_store_unavailable()
            response = self.research_routes.handle_get(
                parts,
                params=params,
                identity=identity,
            )
            if response is not None:
                return response

        # GET /api/v1/runs/{run_id}/workspace
        if len(parts) != 3 or parts[0] != "runs" or parts[2] != "workspace":
            return None
        if params:
            return _error(
                400,
                "RUN_WORKSPACE.INVALID_QUERY",
                "workspace does not accept query parameters",
            )
        owner = identity.username if identity is not None else None
        try:
            payload = self.service.get(parts[1], owner=owner)
        except KeyError:
            return _error(404, "RUN_WORKSPACE.NOT_FOUND", "run not found")
        except PermissionError:
            return _error(403, "RUN_WORKSPACE.FORBIDDEN", "run is owned by another user")
        return ApiResponse(status=200, payload=payload)

    def handle_post(
        self,
        parts: list[str],
        *,
        body: bytes,
        identity: UserIdentity | None,
    ) -> ApiResponse | None:
        """Delegate Research Workspace writes once the composition root calls us.

        The method is intentionally inert for all non-workspace paths.  Adding
        this method does not itself expose writes; ``Pilot107HttpApi._handle_post``
        must explicitly delegate to it, which is kept as a separate landing step.
        """

        if not parts or parts[0] != "research-workspaces":
            return None
        if self.research_routes is None:
            return _research_store_unavailable()
        return self.research_routes.handle_post(
            parts,
            body=body,
            identity=identity,
        )


def _sqlite_research_routes(
    service: RunWorkspaceService,
) -> ResearchWorkspaceRoutes | None:
    # PostgreSQL domain stores intentionally expose ``db_path`` as a compatibility
    # path while keeping ``dsn`` as the real authority.  Never mistake that
    # compatibility path for the active database or Research Workspace bindings
    # would silently fork into a sidecar SQLite file.
    if getattr(service.store, "dsn", None) is not None:
        return None
    db_path = getattr(service.store, "db_path", None)
    if db_path is None:
        return None
    try:
        store = SQLiteResearchWorkspaceStore(db_path)
    except (TypeError, ValueError):
        return None
    return ResearchWorkspaceRoutes(ResearchWorkspaceService(store))


def _research_store_unavailable() -> ApiResponse:
    return _error(
        503,
        "RESEARCH_WORKSPACE.STORE_UNAVAILABLE",
        "Research Workspace storage is not configured for this backend",
    )


def _error(status: int, code: str, message: str) -> ApiResponse:
    return ApiResponse(
        status=status,
        payload={"error": {"code": code, "message": message}},
    )
