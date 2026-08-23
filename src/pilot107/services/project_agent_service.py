"""Owner-scoped application service for isolated experiment project editing."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from pilot107.agent.project import (
    ExperimentProjectOrigin,
    ExperimentProjectSessionRecord,
    ProjectBlueprint,
    ProjectSource,
)
from pilot107.agent.project_store import ProjectStore
from pilot107.agent.publisher import WorkspacePublication, WorkspacePublisher
from pilot107.agent.sandbox import SandboxExecutionResult, SandboxExecutor
from pilot107.agent.tool_gateway import AgentReadHandler, AgentReadResult, AgentToolGatewayError
from pilot107.agent.workspace import (
    AgentWorkspaceRecord,
    WorkspaceApproval,
    WorkspaceChangeSet,
    WorkspaceChangeSetState,
    WorkspaceEditor,
    WorkspaceImporter,
    WorkspacePatch,
    WorkspacePolicyError,
    WorkspaceSnapshot,
    change_set_payload,
    workspace_payload,
)

_MAX_LIST_ITEMS = 500
_MAX_READ_BYTES = 64 * 1024


@dataclass(frozen=True)
class ProjectAgentView:
    project: ExperimentProjectSessionRecord
    workspace: AgentWorkspaceRecord
    change_sets: tuple[WorkspaceChangeSet, ...]
    publish_available: bool


class ProjectAgentService:
    def __init__(
        self,
        *,
        store: ProjectStore,
        workspace_root: Path,
        sandbox: SandboxExecutor,
        importer: WorkspaceImporter | None = None,
        publisher: WorkspacePublisher | None = None,
    ) -> None:
        self.store = store
        self.workspace_root = workspace_root.resolve()
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        self.sandbox = sandbox
        self.importer = importer
        self.publisher = publisher
        self.editor = WorkspaceEditor(store=store)

    def create_project(
        self,
        *,
        owner: str,
        origin: ExperimentProjectOrigin | str,
        goal: str,
        request_key: str,
        source_ref: str | None = None,
    ) -> ProjectAgentView:
        normalized_origin = ExperimentProjectOrigin(origin)
        if normalized_origin == ExperimentProjectOrigin.BLANK:
            if source_ref is not None:
                raise ValueError("blank projects cannot declare source_ref")
            source = None
        else:
            if source_ref is None:
                raise ValueError("non-blank projects require source_ref")
            source = ProjectSource(
                kind=normalized_origin.value,  # type: ignore[arg-type]
                ref_id=f"source-{hashlib.sha256(source_ref.encode()).hexdigest()[:24]}",
                cluster_path=None,
            )
        project = self.store.create_project(
            owner=owner,
            origin=normalized_origin,
            goal=goal,
            request_key=request_key,
            source=source,
        )
        existing = self.store.list_workspaces(project.project_id, owner=owner)
        if existing:
            workspace = existing[0]
        elif normalized_origin == ExperimentProjectOrigin.BLANK:
            workspace = self._create_blank_workspace(project)
        else:
            if self.importer is None:
                raise RuntimeError("Workspace importer is unavailable")
            assert source_ref is not None
            workspace = self.importer.create(project, source_ref=source_ref)
        return ProjectAgentView(
            project=project,
            workspace=workspace,
            change_sets=(),
            publish_available=self.publisher is not None,
        )

    def list_projects(self, *, owner: str, limit: int = 100) -> list[ProjectAgentView]:
        projects = self.store.list_projects(owner=owner, limit=limit)
        return [self.get_project(item.project_id, owner=owner) for item in projects]

    def get_project(
        self,
        project_id: str,
        *,
        owner: str,
        workspace_id: str | None = None,
    ) -> ProjectAgentView:
        project = self.store.get_project(project_id, owner=owner)
        workspaces = self.store.list_workspaces(project_id, owner=owner)
        if not workspaces:
            raise KeyError(project_id)
        workspace = (
            workspaces[0]
            if workspace_id is None
            else next(
                (item for item in workspaces if item.workspace_id == workspace_id),
                None,
            )
        )
        if workspace is None:
            raise KeyError(workspace_id)
        change_sets = self.store.list_change_sets(project_id, owner=owner)
        return ProjectAgentView(
            project=project,
            workspace=workspace,
            change_sets=tuple(
                item
                for item in change_sets
                if workspace_id is None or item.workspace_id == workspace_id
            ),
            publish_available=self.publisher is not None,
        )

    def save_blueprint(
        self,
        project_id: str,
        *,
        owner: str,
        expected_version: int,
        blueprint: ProjectBlueprint,
    ) -> ProjectAgentView:
        self.store.save_blueprint(
            project_id,
            owner,
            expected_version,
            blueprint,
        )
        return self.get_project(project_id, owner=owner)

    def apply_patch(
        self,
        *,
        project_id: str,
        workspace_id: str,
        owner: str,
        relative_path: str,
        expected_source_digest: str | None,
        operation: str,
        content: str | None,
    ) -> WorkspaceChangeSet:
        self._workspace(project_id, workspace_id, owner)
        return self.editor.apply_patch(
            workspace_id,
            owner,
            relative_path,
            expected_source_digest,
            WorkspacePatch(operation=operation, content=content),  # type: ignore[arg-type]
        )

    def apply_patches(
        self,
        *,
        project_id: str,
        workspace_id: str,
        owner: str,
        patches: tuple[tuple[str, str | None, str, str | None], ...],
    ) -> WorkspaceChangeSet:
        self._workspace(project_id, workspace_id, owner)
        return self.editor.apply_patches(
            workspace_id,
            owner,
            tuple(
                (
                    path,
                    expected_digest,
                    WorkspacePatch(operation=operation, content=content),  # type: ignore[arg-type]
                )
                for path, expected_digest, operation, content in patches
            ),
        )

    def get_diff(
        self,
        change_set_id: str,
        *,
        owner: str,
        project_id: str,
        workspace_id: str,
    ) -> str:
        self._workspace(project_id, workspace_id, owner)
        change_set = self.store.get_change_set(change_set_id, owner=owner)
        if change_set.project_id != project_id or change_set.workspace_id != workspace_id:
            raise KeyError(change_set_id)
        return self.store.get_change_set_diff(change_set_id, owner=owner)

    def execute_sandbox(
        self,
        *,
        project_id: str,
        workspace_id: str,
        owner: str,
        change_set_id: str,
        argv: tuple[str, ...],
        timeout: int | float,
    ) -> SandboxExecutionResult:
        workspace = self._workspace(project_id, workspace_id, owner)
        return self.sandbox.execute(
            workspace,
            argv=argv,
            timeout=timeout,
            change_set_id=change_set_id,
        )

    def publish_change_set(
        self,
        *,
        project_id: str,
        workspace_id: str,
        owner: str,
        change_set_id: str,
        expected_version: int,
        approved_digest: str,
        target_root: str | None = None,
    ) -> WorkspacePublication:
        """Approve an exact reviewed digest and synchronously publish it."""

        if self.publisher is None:
            raise RuntimeError("Workspace publisher is unavailable")
        self._workspace(project_id, workspace_id, owner)
        change_set = self.store.get_change_set(change_set_id, owner=owner)
        if change_set.project_id != project_id or change_set.workspace_id != workspace_id:
            raise KeyError(change_set_id)
        if change_set.digest != approved_digest:
            raise ValueError("approved_digest does not match the ChangeSet")
        if change_set.state is WorkspaceChangeSetState.REVIEWABLE:
            if change_set.version != expected_version:
                raise ValueError("ChangeSet version changed before approval")
            now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
            change_set = self.store.replace_change_set(
                replace(
                    change_set,
                    state=WorkspaceChangeSetState.APPROVED,
                    approval=WorkspaceApproval(
                        actor=owner,
                        approved_digest=approved_digest,
                        approved_at=now,
                    ),
                    updated_at=now,
                ),
                expected_version=expected_version,
            )
        else:
            approval = change_set.approval
            if (
                approval is None
                or approval.actor != owner
                or approval.approved_digest != approved_digest
                or change_set.state
                not in {
                    WorkspaceChangeSetState.APPROVED,
                    WorkspaceChangeSetState.PUBLISHING,
                    WorkspaceChangeSetState.PUBLISHED,
                    WorkspaceChangeSetState.CONFLICTED,
                }
            ):
                raise ValueError("ChangeSet is not reviewable or exactly approved")
        if change_set.state in {
            WorkspaceChangeSetState.PUBLISHED,
            WorkspaceChangeSetState.CONFLICTED,
        }:
            return self.store.get_workspace_publication(change_set_id, owner=owner)
        self.publisher.prepare(change_set_id, actor=owner, target_root=target_root)
        return self.publisher.publish(change_set_id, actor=owner)

    def build_tool_handlers(self) -> dict[str, AgentReadHandler]:
        return {
            "project_get": self._tool_project_get,
            "workspace_list": self._tool_workspace_list,
            "workspace_read": self._tool_workspace_read,
            "workspace_patch": self._tool_workspace_patch,
            "workspace_diff": self._tool_workspace_diff,
            "sandbox_exec": self._tool_sandbox_exec,
        }

    def _create_blank_workspace(
        self, project: ExperimentProjectSessionRecord
    ) -> AgentWorkspaceRecord:
        snapshot_digest = hashlib.sha256(f"blank\0{project.project_id}".encode()).hexdigest()
        workspace_id = f"workspace-{snapshot_digest[:24]}"
        owner_root = (self.workspace_root / project.owner).resolve()
        local_root = (owner_root / workspace_id).resolve()
        if local_root.parent != owner_root:
            raise WorkspacePolicyError("Workspace destination escaped the owner root")
        local_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        return self.store.save_workspace(
            AgentWorkspaceRecord(
                workspace_id=workspace_id,
                project_id=project.project_id,
                owner=project.owner,
                local_root=str(local_root),
                snapshot=WorkspaceSnapshot(
                    source_ref=f"/__pilot107_blank__/{project.project_id}",
                    digest=snapshot_digest,
                    entries=(),
                    captured_at=now,
                ),
                created_at=now,
                updated_at=now,
            )
        )

    def _workspace(self, project_id: str, workspace_id: str, owner: str) -> AgentWorkspaceRecord:
        self.store.get_project(project_id, owner=owner)
        workspace = self.store.get_workspace(workspace_id, owner=owner)
        if workspace.project_id != project_id:
            raise KeyError("Workspace is not bound to the requested Project")
        return workspace

    def _tool_project_get(self, owner: str, arguments: Mapping[str, object]) -> AgentReadResult:
        _closed(arguments, {"project_id", "workspace_id"})
        project_id, workspace_id = _scope(arguments)
        view = self.get_project(
            project_id,
            owner=owner,
            workspace_id=workspace_id,
        )
        return AgentReadResult(
            result=project_view_payload(view),
            evidence_refs=(f"project:{project_id}",),
        )

    def _tool_workspace_list(self, owner: str, arguments: Mapping[str, object]) -> AgentReadResult:
        _closed(arguments, {"project_id", "workspace_id"})
        project_id, workspace_id = _scope(arguments)
        workspace = self._workspace(project_id, workspace_id, owner)
        items: list[dict[str, object]] = []
        root = Path(workspace.local_root).resolve(strict=True)
        for directory, names, files in os.walk(root, followlinks=False):
            names[:] = sorted(name for name in names if not Path(directory, name).is_symlink())
            for name in sorted(files):
                path = Path(directory, name)
                if path.is_symlink() or not path.is_file():
                    continue
                relative = path.relative_to(root).as_posix()
                items.append({"path": relative, "kind": "file", "size_bytes": path.stat().st_size})
                if len(items) >= _MAX_LIST_ITEMS:
                    return AgentReadResult(
                        result={"workspace_id": workspace_id, "items": items, "truncated": True},
                        evidence_refs=(f"workspace:{workspace_id}:index",),
                    )
        return AgentReadResult(
            result={"workspace_id": workspace_id, "items": items, "truncated": False},
            evidence_refs=(f"workspace:{workspace_id}:index",),
        )

    def _tool_workspace_read(self, owner: str, arguments: Mapping[str, object]) -> AgentReadResult:
        _closed(arguments, {"project_id", "workspace_id", "path"})
        project_id, workspace_id = _scope(arguments)
        workspace = self._workspace(project_id, workspace_id, owner)
        relative = _relative(_required_string(arguments, "path"))
        target = Path(workspace.local_root).joinpath(*PurePosixPath(relative).parts)
        root = Path(workspace.local_root).resolve(strict=True)
        if target.is_symlink():
            raise _tool_error("Workspace file cannot be read", "AGENT.TOOL.PATH_FORBIDDEN")
        try:
            resolved = target.resolve(strict=True)
            if not resolved.is_relative_to(root) or not resolved.is_file():
                raise OSError
            data = resolved.read_bytes()
            text = data[:_MAX_READ_BYTES].decode("utf-8")
        except (OSError, UnicodeError):
            raise _tool_error(
                "Workspace file cannot be read", "AGENT.TOOL.PATH_FORBIDDEN"
            ) from None
        return AgentReadResult(
            result={
                "workspace_id": workspace_id,
                "path": relative,
                "content": text,
                "sha256": hashlib.sha256(data).hexdigest(),
                "truncated": len(data) > _MAX_READ_BYTES,
            },
            evidence_refs=(f"workspace:{workspace_id}:{relative}",),
        )

    def _tool_workspace_patch(self, owner: str, arguments: Mapping[str, object]) -> AgentReadResult:
        _closed(arguments, {"project_id", "workspace_id", "patches"})
        project_id, workspace_id = _scope(arguments)
        raw_patches = arguments.get("patches")
        if not isinstance(raw_patches, list) or not 1 <= len(raw_patches) <= 256:
            raise _tool_error("workspace patches are invalid", "AGENT.TOOL.INVALID")
        patches: list[tuple[str, str | None, str, str | None]] = []
        for raw_patch in raw_patches:
            if not isinstance(raw_patch, Mapping):
                raise _tool_error("workspace patch is invalid", "AGENT.TOOL.INVALID")
            _closed(
                raw_patch,
                {"path", "expected_source_digest", "operation", "content"},
            )
            patches.append(
                (
                    _required_string(raw_patch, "path"),
                    _optional_string(raw_patch.get("expected_source_digest")),
                    _required_string(raw_patch, "operation"),
                    _optional_string(raw_patch.get("content")),
                )
            )
        change_set = self.apply_patches(
            project_id=project_id,
            workspace_id=workspace_id,
            owner=owner,
            patches=tuple(patches),
        )
        return AgentReadResult(
            result=change_set_payload(change_set),
            evidence_refs=(f"changeset:{change_set.change_set_id}",),
        )

    def _tool_workspace_diff(self, owner: str, arguments: Mapping[str, object]) -> AgentReadResult:
        _closed(arguments, {"project_id", "workspace_id", "change_set_id"})
        project_id, workspace_id = _scope(arguments)
        change_set_id = _required_string(arguments, "change_set_id")
        diff = self.get_diff(
            change_set_id,
            owner=owner,
            project_id=project_id,
            workspace_id=workspace_id,
        )
        return AgentReadResult(
            result={"change_set_id": change_set_id, "unified_diff": diff},
            evidence_refs=(f"changeset:{change_set_id}:diff",),
        )

    def _tool_sandbox_exec(self, owner: str, arguments: Mapping[str, object]) -> AgentReadResult:
        _closed(
            arguments,
            {"project_id", "workspace_id", "change_set_id", "argv", "timeout"},
        )
        project_id, workspace_id = _scope(arguments)
        raw_argv = arguments.get("argv")
        if not isinstance(raw_argv, list) or not all(isinstance(item, str) for item in raw_argv):
            raise _tool_error("sandbox argv is invalid", "AGENT.TOOL.INVALID")
        timeout = arguments.get("timeout")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise _tool_error("sandbox timeout is invalid", "AGENT.TOOL.INVALID")
        result = self.execute_sandbox(
            project_id=project_id,
            workspace_id=workspace_id,
            owner=owner,
            change_set_id=_required_string(arguments, "change_set_id"),
            argv=tuple(raw_argv),
            timeout=timeout,
        )
        return AgentReadResult(
            result=asdict(result),
            evidence_refs=(f"sandbox:{result.result_id}",),
        )


def project_view_payload(view: ProjectAgentView) -> dict[str, Any]:
    project = view.project
    return {
        "project": {
            "schema_version": project.schema_version,
            "project_id": project.project_id,
            "owner": project.owner,
            "origin": project.origin.value,
            "state": project.state.value,
            "version": project.version,
            "goal": project.goal,
            "source": None if project.source is None else asdict(project.source),
            "blueprint": (
                None if project.blueprint is None else _blueprint_payload(project.blueprint)
            ),
            "created_at": project.created_at,
            "updated_at": project.updated_at,
        },
        "workspace": _public_workspace_payload(view.workspace),
        "change_sets": [change_set_payload(item) for item in view.change_sets],
        "risk_summary": _risk_summary(
            view.change_sets, publish_available=view.publish_available
        ),
    }


def _blueprint_payload(blueprint: ProjectBlueprint) -> dict[str, Any]:
    from pilot107.agent.project import blueprint_payload

    return blueprint_payload(blueprint)


def _public_workspace_payload(workspace: AgentWorkspaceRecord) -> dict[str, Any]:
    payload = workspace_payload(workspace)
    payload.pop("local_root", None)
    snapshot = payload.get("snapshot")
    if isinstance(snapshot, dict):
        snapshot.pop("source_ref", None)
    return payload


def _risk_summary(
    change_sets: tuple[WorkspaceChangeSet, ...], *, publish_available: bool
) -> dict[str, object]:
    files = [file for item in change_sets for file in item.files]
    return {
        "level": "medium" if any(item.operation == "delete" for item in files) else "low",
        "changed_files": len(files),
        "deletions": sum(item.operation == "delete" for item in files),
        "sandbox_failures": sum(
            result.status != "succeeded" for item in change_sets for result in item.sandbox_results
        ),
        "publish_available": publish_available,
    }


def _closed(arguments: Mapping[str, object], expected: set[str]) -> None:
    if set(arguments) != expected:
        raise _tool_error("tool arguments are not a closed object", "AGENT.TOOL.INVALID")


def _scope(arguments: Mapping[str, object]) -> tuple[str, str]:
    return (
        _required_string(arguments, "project_id"),
        _required_string(arguments, "workspace_id"),
    )


def _required_string(arguments: Mapping[str, object], name: str) -> str:
    value = arguments.get(name)
    if not isinstance(value, str) or not value or len(value) > 64_000 or "\0" in value:
        raise _tool_error(f"{name} is invalid", "AGENT.TOOL.INVALID")
    return value


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 64_000 or "\0" in value:
        raise _tool_error("optional string is invalid", "AGENT.TOOL.INVALID")
    return value


def _relative(value: str) -> str:
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or ".git" in path.parts or "\\" in value:
        raise _tool_error("Workspace path is forbidden", "AGENT.TOOL.PATH_FORBIDDEN")
    return value


def _tool_error(message: str, code: str) -> AgentToolGatewayError:
    return AgentToolGatewayError(message, code=code)
