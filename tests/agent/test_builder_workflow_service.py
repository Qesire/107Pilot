from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from pilot107.agent.builder_workflow import (
    BuilderPhase,
    BuilderSubmissionRecord,
    BuilderSubmissionState,
)
from pilot107.agent.project_store import SQLiteProjectStore
from pilot107.agent.sandbox import SandboxExecutor
from pilot107.agent.tasks import AgentResourceEnvelope
from pilot107.agent.tool_gateway import AgentToolGatewayError
from pilot107.services.builder_workflow_service import BuilderWorkflowService
from pilot107.services.project_agent_service import ProjectAgentService


class BuilderHarness:
    def __init__(self, tmp_path: Path, *, envelope_digest: str | None = None) -> None:
        self.store = SQLiteProjectStore(tmp_path / "builder-context.db")
        self.projects = ProjectAgentService(
            store=self.store,
            workspace_root=tmp_path / "workspaces",
            sandbox=SandboxExecutor(store=self.store),
        )
        created = self.projects.create_project(
            owner="alice",
            origin="blank",
            goal="solve a bounded heat-diffusion experiment",
            request_key="context-project",
        )
        self.project = created.project
        self.workspace = created.workspace
        self.session_id = "session-builder"
        self.envelope = AgentResourceEnvelope(
            partition="CPU-RC",
            qos="normal",
            cpus=4,
            memory_mib=4096,
            gpu_type=None,
            gpus=0,
            walltime_seconds=600,
            max_tasks=1,
            max_submissions=1,
            workspace_snapshot_digest=(
                envelope_digest or self.workspace.snapshot.digest
            ),
            expires_at="2026-08-30T00:00:00Z",
            approved_by="alice",
        )
        self.workflow = BuilderWorkflowService(
            project_service=self.projects,
            store=self.store,
            envelope_resolver=self.resolve_envelope,
        )

    def resolve_envelope(self, owner: str, session_id: str) -> AgentResourceEnvelope:
        assert owner == "alice"
        if session_id != self.session_id:
            raise KeyError(session_id)
        return self.envelope


def test_context_returns_live_manifest_envelope_phase_and_next_action(
    tmp_path: Path,
) -> None:
    harness = BuilderHarness(tmp_path)
    source = Path(harness.workspace.local_root, "src", "heat2d.c")
    source.parent.mkdir()
    source.write_text("int main(void) { return 0; }\n")

    result = harness.workflow.context(
        owner="alice",
        project_id=harness.project.project_id,
        workspace_id=harness.workspace.workspace_id,
        session_id=harness.session_id,
    )

    assert result.result["phase"] == "drafting"
    assert result.result["next_action"] == "builder_build_submit"
    assert result.result["project"] == {
        "version": 1,
        "goal": harness.project.goal,
        "blueprint": None,
    }
    assert result.result["resource_envelope"] == {
        "partition": "CPU-RC",
        "qos": "normal",
        "cpus": 4,
        "memory_mib": 4096,
        "gpu_type": None,
        "gpus": 0,
        "walltime_seconds": 600,
        "max_tasks": 1,
        "max_submissions": 1,
        "workspace_snapshot_digest": harness.workspace.snapshot.digest,
        "expires_at": "2026-08-30T00:00:00Z",
    }
    assert result.result["manifest"] == {
        "items": [
            {
                "path": "src/heat2d.c",
                "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "size_bytes": source.stat().st_size,
            }
        ],
        "truncated": False,
    }
    serialized = repr(result.result)
    assert "local_root" not in serialized
    assert "approved_by" not in serialized
    assert "owner" not in result.result


def test_context_derives_failed_phase_from_latest_durable_submission(
    tmp_path: Path,
) -> None:
    harness = BuilderHarness(tmp_path)
    harness.store.create_builder_submission(
        BuilderSubmissionRecord(
            submission_id="builder-submission-failed",
            owner="alice",
            session_id=harness.session_id,
            turn_id="turn-builder",
            project_id=harness.project.project_id,
            workspace_id=harness.workspace.workspace_id,
            request_key="build-failed",
            input_digest="a" * 64,
            phase=BuilderPhase.SANDBOX_FAILED,
            state=BuilderSubmissionState.SANDBOX_FAILED,
            version=1,
            base_change_set_id=None,
            change_set_id="changeset-failed",
            sandbox_result_id="sandbox-failed",
            task_id=None,
            receipt={"status": "repair_required", "exit_code": 1},
            created_at="2026-08-29T00:00:00Z",
            updated_at="2026-08-29T00:01:00Z",
        )
    )

    result = harness.workflow.context(
        owner="alice",
        project_id=harness.project.project_id,
        workspace_id=harness.workspace.workspace_id,
        session_id=harness.session_id,
    )

    assert result.result["phase"] == "sandbox_failed"
    assert result.result["next_action"] == "builder_build_submit"
    assert result.result["last_submission"]["change_set_id"] == "changeset-failed"
    assert result.result["last_submission"]["receipt"]["status"] == "repair_required"


def test_context_fails_when_binding_or_snapshot_is_not_approved(tmp_path: Path) -> None:
    harness = BuilderHarness(tmp_path)
    other = harness.projects.create_project(
        owner="alice",
        origin="blank",
        goal="other",
        request_key="other-project",
    )

    with pytest.raises(AgentToolGatewayError) as binding_error:
        harness.workflow.context(
            owner="alice",
            project_id=other.project.project_id,
            workspace_id=harness.workspace.workspace_id,
            session_id=harness.session_id,
        )
    assert binding_error.value.code == "AGENT.BUILDER.BINDING_INVALID"

    mismatched = BuilderHarness(tmp_path / "mismatch", envelope_digest="f" * 64)
    with pytest.raises(AgentToolGatewayError) as snapshot_error:
        mismatched.workflow.context(
            owner="alice",
            project_id=mismatched.project.project_id,
            workspace_id=mismatched.workspace.workspace_id,
            session_id=mismatched.session_id,
        )
    assert snapshot_error.value.code == "AGENT.BUILDER.SNAPSHOT_INVALID"


def test_context_handler_accepts_only_injected_scope(tmp_path: Path) -> None:
    harness = BuilderHarness(tmp_path)
    handler = harness.workflow.build_tool_handlers()["builder_context_get"]
    arguments = {
        "project_id": harness.project.project_id,
        "workspace_id": harness.workspace.workspace_id,
        "session_id": harness.session_id,
    }

    assert handler("alice", arguments).result["phase"] == "drafting"
    with pytest.raises(AgentToolGatewayError) as error:
        handler("alice", {**arguments, "owner": "alice"})
    assert error.value.code == "AGENT.TOOL.INVALID"
