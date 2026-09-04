from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from pilot107.api.run_workspace_routes import RunWorkspaceRoutes
from pilot107.core.identity import UserIdentity
from pilot107.core.research_workspace import SQLiteResearchWorkspaceStore
from pilot107.services.run_workspace_service import RunWorkspaceService


class FakeRunStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path


class FakePostgresRunStore:
    def __init__(self, db_path: Path, dsn: str) -> None:
        self.db_path = db_path
        self.dsn = dsn


def test_run_workspace_router_exposes_research_workspace_reads_on_sqlite(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "pilot107.db"
    workspace_store = SQLiteResearchWorkspaceStore(db_path)
    workspace, created = workspace_store.create_workspace(
        owner="alice",
        request_key="workspace-1",
        title="Sparse FFN",
    )
    assert created

    routes = RunWorkspaceRoutes(
        RunWorkspaceService(store=FakeRunStore(db_path))  # type: ignore[arg-type]
    )
    response = routes.handle_get(
        ["research-workspaces"],
        params={},
        identity=UserIdentity(username="alice"),
    )

    assert response is not None
    assert response.status == 200
    assert response.payload["items"][0]["workspace_id"] == workspace.workspace_id


def test_run_workspace_router_post_delegate_uses_same_sqlite_authority(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "pilot107.db"
    routes = RunWorkspaceRoutes(
        RunWorkspaceService(store=FakeRunStore(db_path))  # type: ignore[arg-type]
    )

    created = routes.handle_post(
        ["research-workspaces"],
        body=json.dumps(
            {
                "request_key": "workspace-create-1",
                "title": "First GPU experiment",
            }
        ).encode("utf-8"),
        identity=UserIdentity(username="alice"),
    )
    assert created is not None
    assert created.status == 201
    workspace_id = created.payload["workspace"]["workspace_id"]

    bound = routes.handle_post(
        ["research-workspaces", workspace_id, "bindings"],
        body=json.dumps(
            {"object_type": "run", "object_id": "run-1"}
        ).encode("utf-8"),
        identity=UserIdentity(username="alice"),
    )
    assert bound is not None
    assert bound.status == 201

    persisted = SQLiteResearchWorkspaceStore(db_path).list_bindings(
        workspace_id,
        owner="alice",
    )
    assert [(item.object_type.value, item.object_id) for item in persisted] == [
        ("run", "run-1")
    ]


def test_postgres_run_store_selects_native_workspace_store(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls: list[tuple[str, Path]] = []

    class FakePostgresWorkspaceStore:
        def __init__(self, dsn: str, *, compatibility_path: Path) -> None:
            calls.append((dsn, compatibility_path))

        def list_workspaces(self, *, owner: str):
            return []

    monkeypatch.setattr(
        "pilot107.api.run_workspace_routes.PostgresResearchWorkspaceStore",
        FakePostgresWorkspaceStore,
    )
    compatibility_path = tmp_path / "compatibility.db"
    routes = RunWorkspaceRoutes(
        RunWorkspaceService(
            store=FakePostgresRunStore(
                compatibility_path,
                "postgresql://pilot@db/pilot107",
            )
        )  # type: ignore[arg-type]
    )

    assert calls == [("postgresql://pilot@db/pilot107", compatibility_path)]
    response = routes.handle_get(
        ["research-workspaces"],
        params={},
        identity=UserIdentity(username="alice"),
    )
    assert response is not None
    assert response.status == 200
    assert response.payload == {"items": []}
