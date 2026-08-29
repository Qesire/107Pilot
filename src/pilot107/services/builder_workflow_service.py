"""Phase-aware facade for bounded Experiment Builder orchestration."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pilot107.agent.builder_workflow import BuilderSubmissionRecord
from pilot107.agent.project import blueprint_payload
from pilot107.agent.project_store import ProjectStore
from pilot107.agent.tasks import AgentResourceEnvelope, parse_timestamp
from pilot107.agent.tool_gateway import (
    AgentReadHandler,
    AgentReadResult,
    AgentToolGatewayError,
)
from pilot107.services.project_agent_service import ProjectAgentService

type EnvelopeResolver = Callable[[str, str], AgentResourceEnvelope]

_MAX_MANIFEST_FILES = 500


class BuilderWorkflowService:
    def __init__(
        self,
        *,
        project_service: ProjectAgentService,
        store: ProjectStore,
        envelope_resolver: EnvelopeResolver,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.project_service = project_service
        self.store = store
        self.envelope_resolver = envelope_resolver
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
            },
            evidence_refs=(
                f"project:{project_id}",
                f"workspace:{workspace_id}:manifest",
            ),
        )

    def build_tool_handlers(self) -> dict[str, AgentReadHandler]:
        return {"builder_context_get": self._tool_context_get}

    def _tool_context_get(
        self, owner: str, arguments: Mapping[str, object]
    ) -> AgentReadResult:
        required = {"project_id", "workspace_id", "session_id"}
        if set(arguments) != required:
            raise _error("Builder context fields are invalid", "AGENT.TOOL.INVALID")
        return self.context(
            owner=owner,
            project_id=_required_string(arguments, "project_id"),
            workspace_id=_required_string(arguments, "workspace_id"),
            session_id=_required_string(arguments, "session_id"),
        )


def _live_manifest(root: Path) -> dict[str, object]:
    resolved_root = root.resolve(strict=True)
    if not resolved_root.is_dir():
        raise OSError("Workspace root is not a directory")
    items: list[dict[str, object]] = []
    truncated = False
    for directory, names, files in os.walk(resolved_root, followlinks=False):
        names[:] = sorted(
            name for name in names if not Path(directory, name).is_symlink()
        )
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


def _error(message: str, code: str) -> AgentToolGatewayError:
    return AgentToolGatewayError(message, code=code)
