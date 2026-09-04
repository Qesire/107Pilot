from __future__ import annotations

import json
from pathlib import Path

from pilot107.api.research_workspace_routes import ResearchWorkspaceRoutes
from pilot107.core.identity import UserIdentity
from pilot107.core.research_workspace import SQLiteResearchWorkspaceStore
from pilot107.services.research_workspace_service import ResearchWorkspaceService


def _routes(path: Path) -> ResearchWorkspaceRoutes:
    return ResearchWorkspaceRoutes(
        ResearchWorkspaceService(SQLiteResearchWorkspaceStore(path))
    )


def _body(value: dict[str, object]) -> bytes:
    return json.dumps(value).encode("utf-8")


def test_create_bind_and_lookup_are_explicit_owner_actions(tmp_path: Path) -> None:
    routes = _routes(tmp_path / "workspace.db")
    alice = UserIdentity("alice")

    created = routes.handle_post(
        ["research-workspaces"],
        body=_body(
            {
                "request_key": "workspace-1",
                "title": "WAN sparse",
                "description": "proxy + mask consumption",
            }
        ),
        identity=alice,
    )
    assert created is not None
    assert created.status == 201
    workspace_id = str(created.payload["workspace"]["workspace_id"])

    bound = routes.handle_post(
        ["research-workspaces", workspace_id, "bindings"],
        body=_body({"object_type": "run", "object_id": "run-12"}),
        identity=alice,
    )
    assert bound is not None
    assert bound.status == 201
    assert bound.payload["binding"]["source"] == "user"

    lookup = routes.handle_get(
        ["research-workspaces", "lookup"],
        params={"object_type": ["run"], "object_id": ["run-12"]},
        identity=alice,
    )
    assert lookup is not None
    assert lookup.status == 200
    assert [item["workspace_id"] for item in lookup.payload["items"]] == [workspace_id]


def test_agent_suggestion_binding_requires_explicit_approval_endpoint(tmp_path: Path) -> None:
    routes = _routes(tmp_path / "workspace.db")
    alice = UserIdentity("alice")
    created = routes.handle_post(
        ["research-workspaces"],
        body=_body({"request_key": "workspace-1", "title": "Experiment"}),
        identity=alice,
    )
    assert created is not None
    workspace_id = str(created.payload["workspace"]["workspace_id"])

    approved = routes.handle_post(
        ["research-workspaces", workspace_id, "bindings", "approve-suggestion"],
        body=_body(
            {
                "object_type": "agent_session",
                "object_id": "session-1",
                "suggestion_ref": "advice-1",
            }
        ),
        identity=alice,
    )
    assert approved is not None
    assert approved.status == 201
    assert approved.payload["binding"]["source"] == "approved_agent_suggestion"
    assert approved.payload["binding"]["source_ref"] == "advice-1"


def test_workspace_detail_is_owner_scoped(tmp_path: Path) -> None:
    routes = _routes(tmp_path / "workspace.db")
    created = routes.handle_post(
        ["research-workspaces"],
        body=_body({"request_key": "workspace-1", "title": "Experiment"}),
        identity=UserIdentity("alice"),
    )
    assert created is not None
    workspace_id = str(created.payload["workspace"]["workspace_id"])

    denied = routes.handle_get(
        ["research-workspaces", workspace_id],
        params={},
        identity=UserIdentity("bob"),
    )
    assert denied is not None
    assert denied.status == 404


def test_routes_fail_closed_without_identity(tmp_path: Path) -> None:
    routes = _routes(tmp_path / "workspace.db")

    response = routes.handle_get(
        ["research-workspaces"],
        params={},
        identity=None,
    )
    assert response is not None
    assert response.status == 401
