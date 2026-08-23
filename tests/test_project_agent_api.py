from __future__ import annotations

import json
from pathlib import Path

from pilot107.agent.project_store import SQLiteProjectStore
from pilot107.agent.sandbox import SandboxExecutor
from pilot107.api.project_agent_routes import ProjectAgentRoutes
from pilot107.core.identity import UserIdentity
from pilot107.services.project_agent_service import ProjectAgentService


def _routes(tmp_path: Path) -> ProjectAgentRoutes:
    store = SQLiteProjectStore(tmp_path / "projects.db")
    service = ProjectAgentService(
        store=store,
        workspace_root=tmp_path / "workspaces",
        sandbox=SandboxExecutor(store=store),
    )
    return ProjectAgentRoutes(service)


def _identity(owner: str) -> UserIdentity:
    return UserIdentity(username=owner)


def test_project_api_create_get_and_cross_owner_masking(tmp_path: Path) -> None:
    routes = _routes(tmp_path)
    created = routes.handle_post(
        ["agent-projects"],
        body=json.dumps(
            {
                "origin": "blank",
                "goal": "build an experiment",
                "request_key": "api-project-1",
            }
        ).encode(),
        identity=_identity("alice"),
    )

    assert created is not None and created.status == 201
    project_id = str(created.payload["project"]["project_id"])
    fetched = routes.handle_get(
        ["agent-projects", project_id],
        params={},
        identity=_identity("alice"),
    )
    masked = routes.handle_get(
        ["agent-projects", project_id],
        params={},
        identity=_identity("bob"),
    )

    assert fetched is not None and fetched.status == 200
    assert "local_root" not in fetched.payload["workspace"]
    assert "source_ref" not in fetched.payload["workspace"]["snapshot"]
    assert masked is not None and masked.status == 404


def test_project_api_is_closed_and_publish_route_fails_closed_without_publisher(
    tmp_path: Path,
) -> None:
    routes = _routes(tmp_path)
    invalid = routes.handle_post(
        ["agent-projects"],
        body=b'{"origin":"blank","goal":"x","request_key":"k","publish":true}',
        identity=_identity("alice"),
    )
    publish = routes.handle_post(
        ["agent-changesets", "changeset-unknown", "publish"],
        body=json.dumps(
            {
                "project_id": "project-unknown",
                "workspace_id": "workspace-unknown",
                "expected_version": 1,
                "approved_digest": "a" * 64,
            }
        ).encode(),
        identity=_identity("alice"),
    )

    assert invalid is not None and invalid.status == 400
    assert publish is not None and publish.status == 503
    assert publish.payload["error"]["code"] == "AGENT.PUBLISHER.UNAVAILABLE"
