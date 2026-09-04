"""Application service for user-owned Research Workspaces."""

from __future__ import annotations

from dataclasses import asdict

from pilot107.core.research_workspace import (
    SQLiteResearchWorkspaceStore,
    WorkspaceObjectType,
)


class ResearchWorkspaceService:
    def __init__(self, store: SQLiteResearchWorkspaceStore) -> None:
        self.store = store

    def create(
        self,
        *,
        owner: str,
        request_key: str,
        title: str,
        description: str = "",
    ) -> dict[str, object]:
        workspace, created = self.store.create_workspace(
            owner=owner,
            request_key=request_key,
            title=title,
            description=description,
        )
        return {"workspace": asdict(workspace), "created": created}

    def list(self, *, owner: str) -> dict[str, object]:
        return {"items": [asdict(item) for item in self.store.list_workspaces(owner=owner)]}

    def get(self, workspace_id: str, *, owner: str) -> dict[str, object]:
        workspace = self.store.get_workspace(workspace_id, owner=owner)
        bindings = self.store.list_bindings(workspace_id, owner=owner)
        return {
            "workspace": asdict(workspace),
            "bindings": [asdict(item) for item in bindings],
        }

    def bind_user_selected(
        self,
        workspace_id: str,
        *,
        owner: str,
        object_type: str,
        object_id: str,
    ) -> dict[str, object]:
        binding, created = self.store.bind_user_selected(
            workspace_id=workspace_id,
            owner=owner,
            object_type=WorkspaceObjectType(object_type),
            object_id=object_id,
            actor=owner,
        )
        return {"binding": asdict(binding), "created": created}

    def approve_agent_suggestion(
        self,
        workspace_id: str,
        *,
        owner: str,
        object_type: str,
        object_id: str,
        suggestion_ref: str,
    ) -> dict[str, object]:
        binding, created = self.store.bind_approved_agent_suggestion(
            workspace_id=workspace_id,
            owner=owner,
            object_type=WorkspaceObjectType(object_type),
            object_id=object_id,
            actor=owner,
            suggestion_ref=suggestion_ref,
        )
        return {"binding": asdict(binding), "created": created}

    def lookup(
        self,
        *,
        owner: str,
        object_type: str,
        object_id: str,
    ) -> dict[str, object]:
        workspaces = self.store.find_object_workspaces(
            owner=owner,
            object_type=WorkspaceObjectType(object_type),
            object_id=object_id,
        )
        return {"items": [asdict(item) for item in workspaces]}
