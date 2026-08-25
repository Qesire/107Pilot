"""Owner-scoped application service for isolated experiment project editing."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import shlex
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any

from pilot107.agent.project import (
    ExperimentProjectOrigin,
    ExperimentProjectSessionRecord,
    ProjectBlueprint,
    ProjectSource,
    blueprint_from_payload,
)
from pilot107.agent.project_store import ProjectStore
from pilot107.agent.publisher import (
    WorkspacePublication,
    WorkspacePublicationState,
    WorkspacePublisher,
    publication_snapshot_digest,
)
from pilot107.agent.sandbox import SandboxExecutionResult, SandboxExecutor
from pilot107.agent.session import AgentTurnRecord
from pilot107.agent.task_store import AgentTaskStore
from pilot107.agent.tasks import AgentTaskState
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
from pilot107.core.contracts import ContractRecord, ContractService
from pilot107.core.contracts import contract_payload as formal_contract_record_payload
from pilot107.core.evidence_binding import EvidenceBinder, EvidenceBundle
from pilot107.core.run_service import RunService
from pilot107.core.run_store import RunRecord
from pilot107.core.states import TERMINAL_RUN_STATES, ResultStatus, RunState
from pilot107.runtime_watch.model import (
    RuntimeWatchRecord,
    RuntimeWatchState,
    runtime_watch_payload,
)
from pilot107.runtime_watch.service import RuntimeWatchRegistrar, RuntimeWatchService
from pilot107.services.agent_session_service import AgentSessionService

_MAX_LIST_ITEMS = 500
_MAX_READ_BYTES = 64 * 1024


@dataclass(frozen=True)
class ProjectAgentView:
    project: ExperimentProjectSessionRecord
    workspace: AgentWorkspaceRecord
    change_sets: tuple[WorkspaceChangeSet, ...]
    publish_available: bool


@dataclass(frozen=True)
class FormalRunApproval:
    approval_digest: str
    approved_by: str
    change_set_digest: str
    published_snapshot_digest: str
    contract_digest: str
    validation_contract_id: str
    validation_run_id: str
    validation_evidence_digest: str
    session_id: str


@dataclass(frozen=True)
class FormalRunCandidate:
    validation_task_id: str
    validation_contract_id: str
    validation_run_id: str
    validation_evidence_refs: tuple[str, ...]
    published_workdir: str
    default_command: str
    resource_hints: Mapping[str, str | int]

    def __post_init__(self) -> None:
        object.__setattr__(self, "resource_hints", MappingProxyType(dict(self.resource_hints)))


@dataclass(frozen=True)
class FormalProjectRun:
    approval: FormalRunApproval
    contract: ContractRecord
    run: RunRecord
    watch: RuntimeWatchRecord


@dataclass(frozen=True)
class FormalRunCompletion:
    run: RunRecord
    watch: RuntimeWatchRecord
    evidence_bundle: EvidenceBundle
    explanation_turn: AgentTurnRecord


class ProjectAgentService:
    def __init__(
        self,
        *,
        store: ProjectStore,
        workspace_root: Path,
        sandbox: SandboxExecutor,
        importer: WorkspaceImporter | None = None,
        publisher: WorkspacePublisher | None = None,
        contract_service: ContractService | None = None,
        run_service: RunService | None = None,
        runtime_watch_service: RuntimeWatchService | RuntimeWatchRegistrar | None = None,
        agent_session_service: AgentSessionService | None = None,
        evidence_binder: EvidenceBinder | None = None,
        agent_task_store: AgentTaskStore | None = None,
    ) -> None:
        self.store = store
        self.workspace_root = workspace_root.resolve()
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        self.sandbox = sandbox
        self.importer = importer
        self.publisher = publisher
        self.contract_service = contract_service
        self.run_service = run_service
        self.runtime_watch_service = runtime_watch_service
        self.agent_session_service = agent_session_service
        self.evidence_binder = evidence_binder
        self.agent_task_store = agent_task_store
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

    def create_failed_run_project(
        self,
        *,
        owner: str,
        source_run_id: str,
        source_workdir: str,
        goal: str,
        request_key: str,
    ) -> ProjectAgentView:
        """Import a failed Run's authoritative workdir into an isolated Project."""

        if self.importer is None:
            raise RuntimeError("Workspace importer is unavailable")
        project = self.store.create_project(
            owner=owner,
            origin=ExperimentProjectOrigin.FAILED_RUN,
            goal=goal,
            request_key=request_key,
            source=ProjectSource(
                kind="failed_run",
                ref_id=source_run_id,
                cluster_path=None,
            ),
        )
        existing = self.store.list_workspaces(project.project_id, owner=owner)
        workspace = (
            existing[0]
            if existing
            else self.importer.create(project, source_ref=source_workdir)
        )
        return ProjectAgentView(
            project=project,
            workspace=workspace,
            change_sets=tuple(
                self.store.list_change_sets(project.project_id, owner=owner)
            ),
            publish_available=self.publisher is not None,
        )

    def create_market_application_project(
        self,
        *,
        owner: str,
        source_item_id: str,
        goal: str,
        request_key: str,
        contract_payload: Mapping[str, object],
    ) -> ProjectAgentView:
        """Materialize a market Contract plan in an isolated reviewable Workspace."""

        project = self.store.create_project(
            owner=owner,
            origin=ExperimentProjectOrigin.TEMPLATE,
            goal=goal,
            request_key=request_key,
            source=ProjectSource(
                kind="template",
                ref_id=source_item_id,
                cluster_path=None,
            ),
        )
        workspaces = self.store.list_workspaces(project.project_id, owner=owner)
        workspace = workspaces[0] if workspaces else self._create_blank_workspace(project)
        change_sets = self.store.list_change_sets(project.project_id, owner=owner)
        if change_sets:
            change_set = change_sets[0]
        else:
            change_set = self.apply_patch(
                project_id=project.project_id,
                workspace_id=workspace.workspace_id,
                owner=owner,
                relative_path="contract.json",
                expected_source_digest=None,
                operation="create",
                content=json.dumps(
                    contract_payload,
                    sort_keys=True,
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
            )
        if change_set.state is WorkspaceChangeSetState.DRAFT:
            self.execute_sandbox(
                project_id=project.project_id,
                workspace_id=workspace.workspace_id,
                owner=owner,
                change_set_id=change_set.change_set_id,
                argv=("python", "-m", "json.tool", "contract.json"),
                timeout=5,
            )
        return self.get_project(
            project.project_id,
            owner=owner,
            workspace_id=workspace.workspace_id,
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

    def prepare_formal_run(
        self,
        *,
        project_id: str,
        workspace_id: str,
        change_set_id: str,
        owner: str,
        session_id: str,
        validation_contract_id: str,
        validation_run_id: str,
        validation_evidence_refs: tuple[str, ...],
        formal_contract_payload: Mapping[str, Any],
    ) -> FormalRunApproval:
        """Recompute the exact digest a user must approve for a formal Run."""

        contract_service, run_service, _, _, evidence_binder = self._formal_services()
        self._workspace(project_id, workspace_id, owner)
        change_set = self.store.get_change_set(change_set_id, owner=owner)
        if (
            change_set.project_id != project_id
            or change_set.workspace_id != workspace_id
            or change_set.state is not WorkspaceChangeSetState.PUBLISHED
        ):
            raise ValueError("formal Run requires the published ChangeSet")
        publication = self.store.get_workspace_publication(change_set_id, owner=owner)
        if (
            publication.state is not WorkspacePublicationState.PUBLISHED
            or publication.approved_digest != change_set.digest
            or publication.approved_by != owner
            or publication.project_id != project_id
            or publication.workspace_id != workspace_id
        ):
            raise ValueError("formal Run publication binding is invalid")
        validation_contract = contract_service.get(validation_contract_id)
        validation_run = run_service.store.get_run(validation_run_id)
        if (
            validation_contract.owner != owner
            or validation_run.owner != owner
            or validation_run.contract_id != validation_contract.contract_id
            or validation_run.state is not RunState.SUCCEEDED
            or validation_run.result_status is not ResultStatus.COMPLETE
        ):
            raise ValueError("formal Run requires successful validation lineage")
        validation_bundle = evidence_binder.bind(validation_run_id, validation_evidence_refs)
        if not validation_bundle.objects or validation_bundle.rejected_refs:
            raise ValueError("validation Evidence is incomplete or untrusted")
        contract_result = contract_service.validate(dict(formal_contract_payload))
        if contract_result.status != "OK":
            raise ValueError("formal Contract validation is blocked")
        if contract_result.effective_request["workdir"] != publication.target_root:
            raise ValueError("formal Contract workdir must equal the published target")
        published_digest = (
            publication_snapshot_digest(publication, change_set.digest)
            if self.publisher is None
            else self.publisher.verify_published_snapshot(change_set_id, actor=owner)
        )
        approval_payload = {
            "schema_version": "pilot107.formal-run-approval/v1",
            "approved_by": owner,
            "project_id": project_id,
            "workspace_id": workspace_id,
            "session_id": session_id,
            "change_set_id": change_set_id,
            "change_set_digest": change_set.digest,
            "publication_id": publication.publication_id,
            "published_snapshot_digest": published_digest,
            "contract_digest": str(contract_result.effective_request["contract_digest"]),
            "validation_contract_id": validation_contract.contract_id,
            "validation_contract_digest": validation_contract.digest,
            "validation_run_id": validation_run.run_id,
            "validation_evidence_digest": validation_bundle.sha256,
        }
        approval_digest = hashlib.sha256(
            json.dumps(
                approval_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        return FormalRunApproval(
            approval_digest=approval_digest,
            approved_by=owner,
            change_set_digest=change_set.digest,
            published_snapshot_digest=published_digest,
            contract_digest=str(contract_result.effective_request["contract_digest"]),
            validation_contract_id=validation_contract.contract_id,
            validation_run_id=validation_run.run_id,
            validation_evidence_digest=validation_bundle.sha256,
            session_id=session_id,
        )

    def prepare_formal_run_candidate(
        self,
        *,
        project_id: str,
        workspace_id: str,
        change_set_id: str,
        session_id: str,
        validation_task_id: str,
        owner: str,
    ) -> FormalRunCandidate:
        """Derive trusted validation lineage and formal defaults from one AgentTask."""

        contract_service, run_service, _, session_service, evidence_binder = (
            self._formal_services()
        )
        if self.agent_task_store is None:
            raise RuntimeError("AgentTask store is unavailable")
        self._workspace(project_id, workspace_id, owner)
        session = session_service.store.get_session(session_id, owner=owner)
        if (
            session.source.get("project_id") != project_id
            or session.source.get("workspace_id") != workspace_id
        ):
            raise ValueError("AgentTask Session lineage is not bound to the Project")

        task = self.agent_task_store.get_task(validation_task_id, owner=owner)
        if (
            task.owner != owner
            or task.session_id != session_id
            or task.project_id != project_id
            or task.workspace_id != workspace_id
            or task.state is not AgentTaskState.SUCCEEDED
            or task.result is None
            or task.result.status != "succeeded"
            or task.linked_run_id is None
        ):
            raise ValueError("AgentTask lineage is not successful and fully bound")
        if not task.result.evidence_refs:
            raise ValueError("AgentTask Evidence is missing")
        linked_run_id = task.linked_run_id
        assert linked_run_id is not None

        change_set = self.store.get_change_set(change_set_id, owner=owner)
        publication = self.store.get_workspace_publication(change_set_id, owner=owner)
        if (
            change_set.project_id != project_id
            or change_set.workspace_id != workspace_id
            or change_set.state is not WorkspaceChangeSetState.PUBLISHED
            or publication.state is not WorkspacePublicationState.PUBLISHED
            or publication.project_id != project_id
            or publication.workspace_id != workspace_id
            or publication.approved_digest != change_set.digest
            or publication.approved_by != owner
        ):
            raise ValueError("formal Run candidate requires the published ChangeSet")

        validation_run = run_service.store.get_run(linked_run_id)
        validation_contract_id = validation_run.contract_id
        if validation_contract_id is None:
            raise ValueError("AgentTask validation Run lacks Contract lineage")
        validation_contract = contract_service.get(validation_contract_id)
        if (
            validation_run.owner != owner
            or validation_contract.owner != owner
            or validation_run.state is not RunState.SUCCEEDED
            or validation_run.result_status is not ResultStatus.COMPLETE
        ):
            raise ValueError("AgentTask validation Run lineage is not successful")
        bundle = evidence_binder.bind(validation_run.run_id, task.result.evidence_refs)
        if not bundle.objects or bundle.rejected_refs:
            raise ValueError("AgentTask Evidence is incomplete or untrusted")

        project = self.store.get_project(project_id, owner=owner)
        if project.blueprint is None:
            raise ValueError("formal Run candidate requires a saved Blueprint")
        validations = tuple(
            validation
            for validation in project.blueprint.validations
            if validation.execution == "slurm"
        )
        if len(validations) != 1:
            raise ValueError("Blueprint must contain exactly one Slurm validation")
        return FormalRunCandidate(
            validation_task_id=task.task_id,
            validation_contract_id=validation_contract.contract_id,
            validation_run_id=validation_run.run_id,
            validation_evidence_refs=task.result.evidence_refs,
            published_workdir=publication.target_root,
            default_command=shlex.join(validations[0].argv),
            resource_hints=project.blueprint.contract_intent.resource_hints,
        )

    def approve_and_submit_formal_run(
        self,
        *,
        project_id: str,
        workspace_id: str,
        change_set_id: str,
        owner: str,
        session_id: str,
        validation_contract_id: str,
        validation_run_id: str,
        validation_evidence_refs: tuple[str, ...],
        formal_contract_payload: Mapping[str, Any],
        approved_digest: str,
    ) -> FormalProjectRun:
        """Verify exact approval, rerun preflight, submit once, and start Watch."""

        contract_service, run_service, watch_service, session_service, _ = self._formal_services()
        session = session_service.store.get_session(session_id, owner=owner)
        if (
            session.source.get("project_id") != project_id
            or session.source.get("workspace_id") != workspace_id
        ):
            raise ValueError("Agent Session is not bound to the formal Project")
        approval = self.prepare_formal_run(
            project_id=project_id,
            workspace_id=workspace_id,
            change_set_id=change_set_id,
            owner=owner,
            session_id=session_id,
            validation_contract_id=validation_contract_id,
            validation_run_id=validation_run_id,
            validation_evidence_refs=validation_evidence_refs,
            formal_contract_payload=formal_contract_payload,
        )
        if not hmac.compare_digest(approved_digest, approval.approval_digest):
            raise ValueError("formal Run approval digest is stale")
        validation_contract = contract_service.get(validation_contract_id)
        contract_id = f"contract_formal_{approval.approval_digest[:24]}"
        contract = contract_service.create_agent_formal(
            validation_contract=validation_contract,
            payload=dict(formal_contract_payload),
            contract_id=contract_id,
            approval_binding={
                "approval_digest": approval.approval_digest,
                "approved_by": owner,
                "project_id": project_id,
                "workspace_id": workspace_id,
                "session_id": session_id,
                "change_set_id": change_set_id,
                "change_set_digest": approval.change_set_digest,
                "published_snapshot_digest": approval.published_snapshot_digest,
                "validation_run_id": validation_run_id,
                "validation_evidence_digest": approval.validation_evidence_digest,
            },
        )
        request = contract_service.to_submit_request(
            contract,
            parent_run_id=validation_run_id,
            lineage_reason="agent_formal_run",
        )
        run = run_service.submit_agent_formal(request, approval_digest=approval.approval_digest)
        watch = watch_service.ensure_submitted_run(run)
        return FormalProjectRun(
            approval=approval,
            contract=contract,
            run=run,
            watch=watch,
        )

    def complete_formal_run(
        self,
        *,
        run_id: str,
        owner: str,
        session_id: str,
        evidence_refs: tuple[str, ...],
    ) -> FormalRunCompletion:
        """Bind terminal facts and enqueue one evidence-grounded explanation."""

        _, run_service, watch_service, session_service, evidence_binder = self._formal_services()
        run = run_service.store.get_run(run_id)
        if (
            run.owner != owner
            or run.lineage_reason != "agent_formal_run"
            or run.state not in TERMINAL_RUN_STATES
        ):
            raise ValueError("formal Run is not terminal")
        watch = watch_service.store.get_watch_for_run(run_id, owner=owner)
        if watch.state is not RuntimeWatchState.STOPPED:
            raise ValueError("formal Run Watch has not drained")
        bundle = evidence_binder.bind(run_id, evidence_refs)
        if not bundle.objects or bundle.rejected_refs:
            raise ValueError("terminal Evidence is incomplete or untrusted")
        turn = session_service.enqueue_result_explanation(
            session_id=session_id,
            owner=owner,
            run_id=run_id,
            evidence_bundle_sha256=bundle.sha256,
        )
        return FormalRunCompletion(
            run=run,
            watch=watch,
            evidence_bundle=bundle,
            explanation_turn=turn,
        )

    def _formal_services(
        self,
    ) -> tuple[
        ContractService,
        RunService,
        RuntimeWatchService | RuntimeWatchRegistrar,
        AgentSessionService,
        EvidenceBinder,
    ]:
        services = (
            self.contract_service,
            self.run_service,
            self.runtime_watch_service,
            self.agent_session_service,
            self.evidence_binder,
        )
        if any(service is None for service in services):
            raise RuntimeError("formal Project Run services are unavailable")
        return services  # type: ignore[return-value]

    def build_tool_handlers(self) -> dict[str, AgentReadHandler]:
        return {
            "project_get": self._tool_project_get,
            "project_blueprint_save": self._tool_project_blueprint_save,
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

    def _tool_project_blueprint_save(
        self, owner: str, arguments: Mapping[str, object]
    ) -> AgentReadResult:
        _closed(
            arguments,
            {"project_id", "workspace_id", "expected_version", "blueprint"},
        )
        project_id, workspace_id = _scope(arguments)
        self._workspace(project_id, workspace_id, owner)
        expected_version = arguments.get("expected_version")
        raw_blueprint = arguments.get("blueprint")
        if (
            isinstance(expected_version, bool)
            or not isinstance(expected_version, int)
            or expected_version < 1
            or not isinstance(raw_blueprint, Mapping)
        ):
            raise _tool_error("Blueprint arguments are invalid", "AGENT.TOOL.INVALID")
        self.save_blueprint(
            project_id,
            owner=owner,
            expected_version=expected_version,
            blueprint=blueprint_from_payload(raw_blueprint),
        )
        view = self.get_project(project_id, owner=owner, workspace_id=workspace_id)
        return AgentReadResult(
            result=project_view_payload(view),
            evidence_refs=(f"project:{project_id}:blueprint",),
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
        "risk_summary": _risk_summary(view.change_sets, publish_available=view.publish_available),
    }


def formal_run_approval_payload(value: FormalRunApproval) -> dict[str, str]:
    return asdict(value)


def formal_run_candidate_payload(value: FormalRunCandidate) -> dict[str, Any]:
    return {
        "validation_task_id": value.validation_task_id,
        "validation_contract_id": value.validation_contract_id,
        "validation_run_id": value.validation_run_id,
        "validation_evidence_refs": list(value.validation_evidence_refs),
        "published_workdir": value.published_workdir,
        "default_command": value.default_command,
        "resource_hints": dict(value.resource_hints),
    }


def formal_project_run_payload(value: FormalProjectRun) -> dict[str, Any]:
    return {
        "approval": formal_run_approval_payload(value.approval),
        "contract": formal_contract_record_payload(value.contract),
        "run": {
            "run_id": value.run.run_id,
            "owner": value.run.owner,
            "state": value.run.state.value,
            "job_id": value.run.job_id,
            "contract_id": value.run.contract_id,
            "parent_run_id": value.run.parent_run_id,
            "lineage_reason": value.run.lineage_reason,
            "result_status": value.run.result_status.value,
            "created_at": value.run.created_at,
            "updated_at": value.run.updated_at,
        },
        "watch": runtime_watch_payload(value.watch),
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
