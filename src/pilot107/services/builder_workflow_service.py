"""Phase-aware facade for bounded Experiment Builder orchestration."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from pilot107.agent.builder_workflow import (
    BuilderPhase,
    BuilderSubmissionConflict,
    BuilderSubmissionRecord,
    BuilderSubmissionState,
)
from pilot107.agent.project import (
    ProjectBlueprint,
    ProjectConflict,
    ProjectValidation,
    blueprint_from_payload,
    blueprint_payload,
)
from pilot107.agent.project_store import ProjectStore
from pilot107.agent.sandbox import SandboxExecutionResult
from pilot107.agent.tasks import (
    AgentResourceEnvelope,
    ResourceEnvelopeExceeded,
    parse_timestamp,
)
from pilot107.agent.tool_gateway import (
    AgentReadHandler,
    AgentReadResult,
    AgentToolGatewayError,
)
from pilot107.agent.workspace import WorkspaceChangeSet, WorkspaceConflict
from pilot107.services.agent_task_service import AgentTaskService
from pilot107.services.project_agent_service import ProjectAgentService, ProjectAgentView

type EnvelopeResolver = Callable[[str, str], AgentResourceEnvelope]

_MAX_MANIFEST_FILES = 500
_MAX_DIAGNOSTIC_BYTES = 16 * 1024
_MAX_REPAIR_SOURCE_FILES = 16
_MAX_REPAIR_SOURCE_BYTES = 64 * 1024


@dataclass(frozen=True)
class _BuildRequest:
    project_id: str
    workspace_id: str
    session_id: str
    turn_id: str
    request_key: str
    approval_summary_zh: str
    expected_project_version: int
    expected_workspace_snapshot_digest: str
    base_change_set_id: str | None
    blueprint: ProjectBlueprint
    patches: tuple[tuple[str, str | None, str, str | None], ...]
    input_digest: str


class BuilderWorkflowService:
    def __init__(
        self,
        *,
        project_service: ProjectAgentService,
        store: ProjectStore,
        envelope_resolver: EnvelopeResolver,
        agent_task_service: AgentTaskService,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.project_service = project_service
        self.store = store
        self.envelope_resolver = envelope_resolver
        self.agent_task_service = agent_task_service
        self._clock = clock or (lambda: datetime.now(UTC))

    def context(
        self,
        *,
        owner: str,
        project_id: str,
        workspace_id: str,
        session_id: str,
    ) -> AgentReadResult:
        try:
            view = self.project_service.get_project(
                project_id,
                owner=owner,
                workspace_id=workspace_id,
            )
        except (KeyError, ValueError):
            raise _error(
                "Builder Project or Workspace binding is invalid",
                "AGENT.BUILDER.BINDING_INVALID",
            ) from None
        self._assert_session_binding(
            owner=owner,
            session_id=session_id,
            project_id=project_id,
            workspace_id=workspace_id,
        )
        try:
            envelope = self.envelope_resolver(owner, session_id)
        except (KeyError, TypeError, ValueError):
            raise _error(
                "Builder resource envelope is unavailable",
                "AGENT.BUILDER.ENVELOPE_UNAVAILABLE",
            ) from None
        now = self._clock()
        if now.tzinfo is None:
            raise ValueError("Builder workflow clock must be timezone-aware")
        if envelope.approved_by != owner or parse_timestamp(
            envelope.expires_at, "expires_at"
        ) <= now.astimezone(UTC):
            raise _error(
                "Builder resource envelope is unavailable",
                "AGENT.BUILDER.ENVELOPE_UNAVAILABLE",
            )
        if envelope.workspace_snapshot_digest != view.workspace.snapshot.digest:
            raise _error(
                "Builder Workspace snapshot is not approved",
                "AGENT.BUILDER.SNAPSHOT_INVALID",
            )
        try:
            manifest = _live_manifest(Path(view.workspace.local_root))
        except OSError:
            raise _error(
                "Builder Workspace is unavailable",
                "AGENT.BUILDER.BINDING_INVALID",
            ) from None
        latest = self.store.get_latest_builder_submission(
            owner=owner,
            session_id=session_id,
            project_id=project_id,
            workspace_id=workspace_id,
        )
        phase = "drafting" if latest is None else latest.phase.value
        repair_sources: dict[str, object] = {"items": [], "truncated": False}
        repair_change_set_id = (
            None
            if latest is None
            else latest.change_set_id
            if latest.state is BuilderSubmissionState.SANDBOX_FAILED
            else latest.base_change_set_id
            if (
                latest.state is BuilderSubmissionState.RUNNING
                and latest.change_set_id is None
                and latest.sandbox_result_id is None
                and latest.task_id is None
                and latest.receipt is None
            )
            else None
        )
        if repair_change_set_id is not None:
            try:
                failed_change_set = self.store.get_change_set(
                    repair_change_set_id,
                    owner=owner,
                )
                repair_sources = _repair_sources(
                    Path(view.workspace.local_root),
                    failed_change_set,
                )
            except KeyError:
                # Older durable records may retain only the phase/receipt after
                # their ChangeSet has been compacted.  Preserve the phase
                # status; a repair can still be rejected later without
                # manufacturing source content.
                repair_sources = {"items": [], "truncated": False}
            except (OSError, UnicodeError):
                raise _error(
                    "Builder repair sources are unavailable",
                    "AGENT.BUILDER.BINDING_INVALID",
                ) from None
        return AgentReadResult(
            result={
                "phase": phase,
                "next_action": (
                    None if phase == "validation_scheduled" else "builder_build_submit"
                ),
                "project": {
                    "version": view.project.version,
                    "goal": view.project.goal,
                    "blueprint": (
                        None
                        if view.project.blueprint is None
                        else blueprint_payload(view.project.blueprint)
                    ),
                },
                "manifest": manifest,
                "resource_envelope": _envelope_payload(envelope),
                "last_submission": _submission_context(latest),
                "repair_sources": repair_sources,
            },
            evidence_refs=(
                f"project:{project_id}",
                f"workspace:{workspace_id}:manifest",
            ),
        )

    def build_tool_handlers(self) -> dict[str, AgentReadHandler]:
        return {
            "builder_context_get": self._tool_context_get,
            "builder_build_submit": self.submit,
        }

    def submit(self, owner: str, arguments: Mapping[str, object]) -> AgentReadResult:
        try:
            request = _parse_build_request(arguments)
            return self._submit(owner, request)
        except AgentToolGatewayError:
            raise
        except BuilderSubmissionConflict:
            raise _error(
                "Builder request key conflicts with different content",
                "AGENT.BUILDER.IDEMPOTENCY_CONFLICT",
            ) from None
        except ResourceEnvelopeExceeded:
            raise _error(
                "Builder resources exceed the approved envelope",
                "AGENT.TOOL.RESOURCE_ENVELOPE_EXCEEDED",
            ) from None
        except (ProjectConflict, WorkspaceConflict):
            raise _error(
                "Builder request made no progress against the current Workspace",
                "AGENT.BUILDER.NO_PROGRESS",
            ) from None
        except (KeyError, TypeError, ValueError):
            raise _error("Builder submission is invalid", "AGENT.TOOL.INVALID") from None

    def _submit(self, owner: str, request: _BuildRequest) -> AgentReadResult:
        view = self._bound_view(owner, request)
        envelope = self._bound_envelope(owner, request)
        sandbox_validation, _ = _validations(request.blueprint)
        existing = self.store.get_builder_submission_by_request_key(owner, request.request_key)
        if existing is not None:
            if existing.input_digest != request.input_digest:
                raise BuilderSubmissionConflict("Builder request content changed")
            if existing.receipt is not None:
                return _record_result(existing)
            record = existing
        else:
            latest = self.store.get_latest_builder_submission(
                owner=owner,
                session_id=request.session_id,
                project_id=request.project_id,
                workspace_id=request.workspace_id,
            )
            if latest is None and request.base_change_set_id is not None:
                raise _error(
                    "Initial Builder submission cannot name a base ChangeSet",
                    "AGENT.BUILDER.NO_PROGRESS",
                )
            continuing_repair = latest is not None and (
                latest.state is BuilderSubmissionState.SANDBOX_FAILED
                and request.base_change_set_id == latest.change_set_id
            )
            recovering_unfinished_draft = latest is not None and (
                latest.state is BuilderSubmissionState.RUNNING
                and latest.change_set_id is None
                and latest.sandbox_result_id is None
                and latest.task_id is None
                and latest.receipt is None
                and request.base_change_set_id == latest.base_change_set_id
            )
            if latest is not None and not (continuing_repair or recovering_unfinished_draft):
                raise _error(
                    "Builder submission does not continue the latest phase",
                    "AGENT.BUILDER.NO_PROGRESS",
                )
            if view.project.version != request.expected_project_version:
                raise _error(
                    "Builder Project version is stale",
                    "AGENT.BUILDER.NO_PROGRESS",
                )
            if not _patches_make_progress(Path(view.workspace.local_root), request.patches):
                raise _error(
                    "Builder patches do not change the Workspace",
                    "AGENT.BUILDER.NO_PROGRESS",
                )
            now = self._now()
            digest = hashlib.sha256(f"{owner}\0{request.request_key}".encode()).hexdigest()
            record = self.store.create_builder_submission(
                BuilderSubmissionRecord(
                    submission_id=f"builder-submission-{digest[:24]}",
                    owner=owner,
                    session_id=request.session_id,
                    turn_id=request.turn_id,
                    project_id=request.project_id,
                    workspace_id=request.workspace_id,
                    request_key=request.request_key,
                    input_digest=request.input_digest,
                    phase=BuilderPhase.DRAFTING,
                    state=BuilderSubmissionState.RUNNING,
                    version=1,
                    base_change_set_id=request.base_change_set_id,
                    change_set_id=None,
                    sandbox_result_id=None,
                    task_id=None,
                    receipt=None,
                    created_at=now,
                    updated_at=now,
                )
            )

        project = view.project
        if project.version == request.expected_project_version:
            view = self.project_service.save_blueprint(
                request.project_id,
                owner=owner,
                expected_version=request.expected_project_version,
                blueprint=request.blueprint,
            )
        elif not (
            project.version == request.expected_project_version + 1
            and project.blueprint == request.blueprint
        ):
            raise _error(
                "Builder Project version is stale",
                "AGENT.BUILDER.NO_PROGRESS",
            )

        if record.change_set_id is None:
            change_set = _matching_change_set(view.change_sets, request.patches)
            if change_set is None:
                change_set = self.project_service.apply_patches(
                    project_id=request.project_id,
                    workspace_id=request.workspace_id,
                    owner=owner,
                    patches=request.patches,
                )
            record = self.store.replace_builder_submission(
                replace(
                    record,
                    version=record.version + 1,
                    change_set_id=change_set.change_set_id,
                    updated_at=self._now(),
                ),
                expected_version=record.version,
            )
        else:
            change_set = self.store.get_change_set(record.change_set_id, owner=owner)

        sandbox_result = _persisted_sandbox_result(record, change_set, sandbox_validation.argv)
        if sandbox_result is None:
            sandbox_result = self.project_service.execute_sandbox(
                project_id=request.project_id,
                workspace_id=request.workspace_id,
                owner=owner,
                change_set_id=change_set.change_set_id,
                argv=sandbox_validation.argv,
                timeout=min(300, envelope.walltime_seconds),
            )
        if record.sandbox_result_id is None:
            record = self.store.replace_builder_submission(
                replace(
                    record,
                    version=record.version + 1,
                    sandbox_result_id=sandbox_result.result_id,
                    updated_at=self._now(),
                ),
                expected_version=record.version,
            )

        diff_sha256 = hashlib.sha256(
            self.project_service.get_diff(
                change_set.change_set_id,
                owner=owner,
                project_id=request.project_id,
                workspace_id=request.workspace_id,
            ).encode()
        ).hexdigest()
        if sandbox_result.status != "succeeded":
            repair_sources = _repair_sources(
                Path(view.workspace.local_root),
                change_set,
            )
            receipt: dict[str, object] = {
                "submission_id": record.submission_id,
                "status": "repair_required",
                "phase": BuilderPhase.SANDBOX_FAILED.value,
                "next_action": "builder_build_submit",
                "approval_summary_zh": request.approval_summary_zh,
                "change_set_id": change_set.change_set_id,
                "change_set_digest": change_set.digest,
                "diff_sha256": diff_sha256,
                "sandbox_result_id": sandbox_result.result_id,
                "diagnostics": _sandbox_diagnostics(sandbox_result),
                "next_submission": {
                    "expected_project_version": view.project.version,
                    "expected_workspace_snapshot_digest": (
                        request.expected_workspace_snapshot_digest
                    ),
                    "base_change_set_id": change_set.change_set_id,
                    "expected_source_digests": {
                        item.path: item.after_sha256
                        for item in change_set.files
                        if item.after_sha256 is not None
                    },
                    "repair_sources": repair_sources,
                    "request_key_policy": "new_for_changed_content",
                },
            }
            failed = self.store.replace_builder_submission(
                replace(
                    record,
                    phase=BuilderPhase.SANDBOX_FAILED,
                    state=BuilderSubmissionState.SANDBOX_FAILED,
                    version=record.version + 1,
                    receipt=receipt,
                    updated_at=self._now(),
                ),
                expected_version=record.version,
            )
            return _record_result(failed)

        task, _ = self.agent_task_service.schedule_blueprint_validation(
            owner=owner,
            session_id=request.session_id,
            turn_id=request.turn_id,
            project_id=request.project_id,
            workspace_id=request.workspace_id,
            request_key=f"builder-validation:{record.submission_id}",
            blueprint=request.blueprint,
            envelope=envelope,
        )
        receipt = {
            "submission_id": record.submission_id,
            "status": "scheduled",
            "phase": BuilderPhase.VALIDATION_SCHEDULED.value,
            "next_action": None,
            "approval_summary_zh": request.approval_summary_zh,
            "change_set_id": change_set.change_set_id,
            "change_set_digest": change_set.digest,
            "diff_sha256": diff_sha256,
            "sandbox_result_id": sandbox_result.result_id,
            "task_id": task.task_id,
            "task_state": task.state.value,
            "next_submission": None,
        }
        scheduled = self.store.replace_builder_submission(
            replace(
                record,
                phase=BuilderPhase.VALIDATION_SCHEDULED,
                state=BuilderSubmissionState.SCHEDULED,
                version=record.version + 1,
                task_id=task.task_id,
                receipt=receipt,
                updated_at=self._now(),
            ),
            expected_version=record.version,
        )
        return _record_result(scheduled)

    def _bound_view(self, owner: str, request: _BuildRequest) -> ProjectAgentView:
        try:
            view = self.project_service.get_project(
                request.project_id,
                owner=owner,
                workspace_id=request.workspace_id,
            )
        except KeyError:
            raise _error(
                "Builder Project or Workspace binding is invalid",
                "AGENT.BUILDER.BINDING_INVALID",
            ) from None
        self._assert_session_binding(
            owner=owner,
            session_id=request.session_id,
            project_id=request.project_id,
            workspace_id=request.workspace_id,
            turn_id=request.turn_id,
        )
        if request.expected_workspace_snapshot_digest != view.workspace.snapshot.digest:
            raise _error(
                "Builder Workspace snapshot is stale",
                "AGENT.BUILDER.SNAPSHOT_INVALID",
            )
        return view

    def _bound_envelope(self, owner: str, request: _BuildRequest) -> AgentResourceEnvelope:
        try:
            envelope = self.envelope_resolver(owner, request.session_id)
        except (KeyError, TypeError, ValueError):
            raise _error(
                "Builder resource envelope is unavailable",
                "AGENT.BUILDER.ENVELOPE_UNAVAILABLE",
            ) from None
        if (
            envelope.approved_by != owner
            or envelope.workspace_snapshot_digest != request.expected_workspace_snapshot_digest
            or parse_timestamp(envelope.expires_at, "expires_at") <= self._clock().astimezone(UTC)
        ):
            raise _error(
                "Builder resource envelope is unavailable",
                "AGENT.BUILDER.ENVELOPE_UNAVAILABLE",
            )
        return envelope

    def _assert_session_binding(
        self,
        *,
        owner: str,
        session_id: str,
        project_id: str,
        workspace_id: str,
        turn_id: str | None = None,
    ) -> None:
        try:
            session = self.agent_task_service.session_store.get_session(session_id, owner=owner)
            turn = (
                None
                if turn_id is None
                else self.agent_task_service.session_store.get_turn(
                    turn_id,
                    owner=owner,
                )
            )
        except KeyError:
            raise _error(
                "Builder Session binding is invalid",
                "AGENT.BUILDER.BINDING_INVALID",
            ) from None
        if (
            session.profile_id != "experiment_builder"
            or session.source.get("project_id") != project_id
            or session.source.get("workspace_id") != workspace_id
            or (turn is not None and turn.session_id != session_id)
        ):
            raise _error(
                "Builder Session binding is invalid",
                "AGENT.BUILDER.BINDING_INVALID",
            )

    def _now(self) -> str:
        value = self._clock()
        if value.tzinfo is None:
            raise ValueError("Builder workflow clock must be timezone-aware")
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")

    def _tool_context_get(self, owner: str, arguments: Mapping[str, object]) -> AgentReadResult:
        required = {"project_id", "workspace_id", "session_id"}
        if set(arguments) != required:
            raise _error("Builder context fields are invalid", "AGENT.TOOL.INVALID")
        return self.context(
            owner=owner,
            project_id=_required_string(arguments, "project_id"),
            workspace_id=_required_string(arguments, "workspace_id"),
            session_id=_required_string(arguments, "session_id"),
        )


def _parse_build_request(arguments: Mapping[str, object]) -> _BuildRequest:
    required = {
        "project_id",
        "workspace_id",
        "session_id",
        "turn_id",
        "request_key",
        "approval_summary_zh",
        "expected_project_version",
        "expected_workspace_snapshot_digest",
        "base_change_set_id",
        "blueprint",
        "patches",
    }
    if set(arguments) != required:
        raise ValueError("Builder submission fields are invalid")
    expected_version = arguments.get("expected_project_version")
    if (
        isinstance(expected_version, bool)
        or not isinstance(expected_version, int)
        or expected_version < 1
    ):
        raise ValueError("Builder Project version is invalid")
    snapshot_digest = _digest_string(
        arguments.get("expected_workspace_snapshot_digest"),
        "Workspace snapshot digest",
    )
    base_change_set_id = _optional_string(arguments.get("base_change_set_id"))
    raw_blueprint = arguments.get("blueprint")
    if not isinstance(raw_blueprint, Mapping):
        raise TypeError("Builder Blueprint must be an object")
    blueprint = blueprint_from_payload(raw_blueprint)
    raw_patches = arguments.get("patches")
    if not isinstance(raw_patches, list) or not 1 <= len(raw_patches) <= 256:
        raise ValueError("Builder patches are invalid")
    patches: list[tuple[str, str | None, str, str | None]] = []
    normalized_patches: list[dict[str, object]] = []
    for raw_patch in raw_patches:
        if not isinstance(raw_patch, Mapping) or set(raw_patch) != {
            "path",
            "expected_source_digest",
            "operation",
            "content",
        }:
            raise ValueError("Builder patch fields are invalid")
        path = _required_string(raw_patch, "path")
        expected_digest = raw_patch.get("expected_source_digest")
        if expected_digest is not None:
            expected_digest = _digest_string(expected_digest, "Patch source digest")
        operation = _required_string(raw_patch, "operation")
        if operation not in {"create", "modify", "delete"}:
            raise ValueError("Builder patch operation is invalid")
        content = raw_patch.get("content")
        if operation == "delete":
            if content is not None:
                raise ValueError("Delete patch cannot contain content")
        elif not isinstance(content, str):
            raise TypeError("Builder patch content must be text")
        patches.append((path, expected_digest, operation, content))
        normalized_patches.append(
            {
                "path": path,
                "expected_source_digest": expected_digest,
                "operation": operation,
                "content": content,
            }
        )
    project_id = _required_string(arguments, "project_id")
    workspace_id = _required_string(arguments, "workspace_id")
    session_id = _required_string(arguments, "session_id")
    turn_id = _required_string(arguments, "turn_id")
    request_key = _required_string(arguments, "request_key")
    approval_summary_zh = _required_string(arguments, "approval_summary_zh")
    if len(approval_summary_zh) > 4_000:
        raise ValueError("Builder approval summary is too long")
    normalized = {
        "project_id": project_id,
        "workspace_id": workspace_id,
        "session_id": session_id,
        "turn_id": turn_id,
        "request_key": request_key,
        "approval_summary_zh": approval_summary_zh,
        "expected_project_version": expected_version,
        "expected_workspace_snapshot_digest": snapshot_digest,
        "base_change_set_id": base_change_set_id,
        "blueprint": blueprint_payload(blueprint),
        "patches": normalized_patches,
    }
    encoded = json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()
    return _BuildRequest(
        project_id=project_id,
        workspace_id=workspace_id,
        session_id=session_id,
        turn_id=turn_id,
        request_key=request_key,
        approval_summary_zh=approval_summary_zh,
        expected_project_version=expected_version,
        expected_workspace_snapshot_digest=snapshot_digest,
        base_change_set_id=base_change_set_id,
        blueprint=blueprint,
        patches=tuple(patches),
        input_digest=hashlib.sha256(encoded).hexdigest(),
    )


def _validations(
    blueprint: ProjectBlueprint,
) -> tuple[ProjectValidation, ProjectValidation]:
    sandbox = [item for item in blueprint.validations if item.execution == "sandbox"]
    slurm = [item for item in blueprint.validations if item.execution == "slurm"]
    if len(sandbox) != 1 or len(slurm) != 1 or len(blueprint.validations) != 2:
        raise _error(
            "Builder Blueprint must declare exactly one sandbox validation "
            "and one Slurm validation",
            "AGENT.BUILDER.VALIDATIONS_INVALID",
        )
    return sandbox[0], slurm[0]


def _matching_change_set(
    candidates: tuple[WorkspaceChangeSet, ...],
    patches: tuple[tuple[str, str | None, str, str | None], ...],
) -> WorkspaceChangeSet | None:
    expected = tuple(
        (
            path,
            operation,
            (
                None
                if operation == "delete"
                else hashlib.sha256((content or "").encode()).hexdigest()
            ),
        )
        for path, _, operation, content in patches
    )
    for candidate in candidates:
        actual = tuple((item.path, item.operation, item.after_sha256) for item in candidate.files)
        if actual == expected:
            return candidate
    return None


def _patches_make_progress(
    workspace_root: Path,
    patches: tuple[tuple[str, str | None, str, str | None], ...],
) -> bool:
    root = workspace_root.resolve(strict=True)
    progress = False
    for relative_path, _, operation, content in patches:
        relative = PurePosixPath(relative_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Builder patch path is invalid")
        target = root.joinpath(*relative.parts)
        if target.is_symlink():
            raise ValueError("Builder patch path is invalid")
        exists = target.is_file()
        if operation == "create":
            progress = progress or not exists
        elif operation == "delete":
            progress = progress or exists
        elif exists:
            expected = hashlib.sha256((content or "").encode()).hexdigest()
            progress = progress or hashlib.sha256(target.read_bytes()).hexdigest() != expected
    return progress


def _persisted_sandbox_result(
    record: BuilderSubmissionRecord,
    change_set: WorkspaceChangeSet,
    expected_argv: tuple[str, ...],
) -> SandboxExecutionResult | None:
    for result in reversed(change_set.sandbox_results):
        if result.argv != expected_argv:
            continue
        if record.sandbox_result_id is not None and result.result_id != record.sandbox_result_id:
            continue
        return SandboxExecutionResult(
            result_id=result.result_id,
            argv=result.argv,
            status=result.status,
            exit_code=result.exit_code,
            stdout="",
            stderr="",
            stdout_sha256=result.stdout_sha256,
            stderr_sha256=result.stderr_sha256,
            limit_reason=None,
        )
    return None


def _sandbox_diagnostics(result: SandboxExecutionResult) -> dict[str, object]:
    return {
        "status": result.status,
        "exit_code": result.exit_code,
        "stdout": _bounded_output(result.stdout),
        "stderr": _bounded_output(result.stderr),
        "stdout_sha256": result.stdout_sha256,
        "stderr_sha256": result.stderr_sha256,
        "limit_reason": result.limit_reason,
    }


def _bounded_output(value: str) -> str:
    encoded = value.encode()
    if len(encoded) <= _MAX_DIAGNOSTIC_BYTES:
        return value
    return encoded[:_MAX_DIAGNOSTIC_BYTES].decode("utf-8", errors="replace")


def _repair_sources(
    workspace_root: Path,
    change_set: WorkspaceChangeSet,
) -> dict[str, object]:
    root = workspace_root.resolve(strict=True)
    items: list[dict[str, object]] = []
    remaining = _MAX_REPAIR_SOURCE_BYTES
    truncated = False
    candidates = [item for item in change_set.files if item.after_sha256 is not None]
    for index, item in enumerate(candidates):
        if index >= _MAX_REPAIR_SOURCE_FILES or remaining <= 0:
            truncated = True
            break
        relative = PurePosixPath(item.path)
        target = root.joinpath(*relative.parts).resolve(strict=True)
        if not target.is_relative_to(root) or not target.is_file():
            raise OSError("Builder repair source is unavailable")
        data = target.read_bytes()
        bounded = data[:remaining]
        item_truncated = len(bounded) < len(data)
        items.append(
            {
                "path": item.path,
                "sha256": hashlib.sha256(data).hexdigest(),
                "content": bounded.decode("utf-8"),
                "truncated": item_truncated,
            }
        )
        remaining -= len(bounded)
        if item_truncated:
            truncated = True
            break
    return {"items": items, "truncated": truncated}


def _record_result(record: BuilderSubmissionRecord) -> AgentReadResult:
    if record.receipt is None:
        raise RuntimeError("Builder submission receipt is unavailable")
    refs = [f"builder-submission:{record.submission_id}"]
    if record.change_set_id is not None:
        refs.append(f"changeset:{record.change_set_id}")
    if record.sandbox_result_id is not None:
        refs.append(f"sandbox:{record.sandbox_result_id}")
    if record.task_id is not None:
        refs.append(f"agent-task:{record.task_id}")
    return AgentReadResult(result=dict(record.receipt), evidence_refs=tuple(refs))


def _live_manifest(root: Path) -> dict[str, object]:
    resolved_root = root.resolve(strict=True)
    if not resolved_root.is_dir():
        raise OSError("Workspace root is not a directory")
    items: list[dict[str, object]] = []
    truncated = False
    for directory, names, files in os.walk(resolved_root, followlinks=False):
        names[:] = sorted(name for name in names if not Path(directory, name).is_symlink())
        for name in sorted(files):
            candidate = Path(directory, name)
            if candidate.is_symlink() or not candidate.is_file():
                continue
            if len(items) >= _MAX_MANIFEST_FILES:
                truncated = True
                break
            data = candidate.read_bytes()
            items.append(
                {
                    "path": candidate.relative_to(resolved_root).as_posix(),
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "size_bytes": len(data),
                }
            )
        if truncated:
            break
    return {"items": items, "truncated": truncated}


def _envelope_payload(envelope: AgentResourceEnvelope) -> dict[str, object]:
    return {
        "partition": envelope.partition,
        "qos": envelope.qos,
        "cpus": envelope.cpus,
        "memory_mib": envelope.memory_mib,
        "gpu_type": envelope.gpu_type,
        "gpus": envelope.gpus,
        "walltime_seconds": envelope.walltime_seconds,
        "max_tasks": envelope.max_tasks,
        "max_submissions": envelope.max_submissions,
        "workspace_snapshot_digest": envelope.workspace_snapshot_digest,
        "expires_at": envelope.expires_at,
    }


def _submission_context(record: BuilderSubmissionRecord | None) -> dict[str, Any] | None:
    if record is None:
        return None
    return {
        "submission_id": record.submission_id,
        "request_key": record.request_key,
        "phase": record.phase.value,
        "state": record.state.value,
        "base_change_set_id": record.base_change_set_id,
        "change_set_id": record.change_set_id,
        "sandbox_result_id": record.sandbox_result_id,
        "task_id": record.task_id,
        "receipt": None if record.receipt is None else dict(record.receipt),
    }


def _required_string(arguments: Mapping[str, object], key: str) -> str:
    value = arguments.get(key)
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 256
        or any(character in value for character in "\r\n\0")
    ):
        raise _error("Builder context fields are invalid", "AGENT.TOOL.INVALID")
    return value


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > 256 or "\0" in value:
        raise ValueError("Builder optional identifier is invalid")
    return value


def _digest_string(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{label} is invalid")
    return value


def _error(message: str, code: str) -> AgentToolGatewayError:
    return AgentToolGatewayError(message, code=code)
