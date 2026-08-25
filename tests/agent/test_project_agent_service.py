from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from pilot107.adapters.slurm import FileEntry, FileStat
from pilot107.agent.project import (
    ProjectBlueprint,
    ProjectContractIntent,
    ProjectFile,
    ProjectValidation,
    blueprint_payload,
)
from pilot107.agent.project_store import SQLiteProjectStore
from pilot107.agent.protocol import TOOL_INVOCATION_PROTOCOL_VERSION, ToolInvocation
from pilot107.agent.sandbox import SandboxExecutor
from pilot107.agent.store import SQLiteAgentSessionStore
from pilot107.agent.tool_gateway import AgentReadResult, AgentToolGateway, AgentToolGatewayError
from pilot107.agent.workspace import WorkspaceConflict, WorkspaceImporter
from pilot107.services.project_agent_service import ProjectAgentService


def _service(tmp_path: Path) -> ProjectAgentService:
    store = SQLiteProjectStore(tmp_path / "projects.db")
    return ProjectAgentService(
        store=store,
        workspace_root=tmp_path / "workspaces",
        sandbox=SandboxExecutor(store=store),
    )


class _ExistingSource:
    def __init__(self) -> None:
        self.files = {
            "/public/home/alice/exp/main.py": b"print('source')\n",
            "/public/home/alice/exp/config.yaml": b"seed: 1\n",
        }

    def stat_path(self, *, path: str, owner: str, timeout_seconds: float = 30.0):
        assert (path, owner) == ("/public/home/alice/exp", "alice")
        return FileStat(path=path, type="dir", size=0, mtime=1)

    def list_dir(self, *, path: str, owner: str, timeout_seconds: float = 30.0):
        assert (path, owner) == ("/public/home/alice/exp", "alice")
        return [
            FileEntry(
                name=name,
                type="file",
                size=len(content),
                mtime=index,
            )
            for index, (name, content) in enumerate(
                ((Path(path).name, content) for path, content in self.files.items()),
                start=2,
            )
        ]

    def read_bytes_chunk(
        self,
        *,
        path: str,
        offset: int,
        length: int,
        owner: str,
        timeout_seconds: float = 30.0,
    ) -> tuple[str, int]:
        content = self.files[path]
        return base64.b64encode(content[offset : offset + length]).decode(), len(content)

    def file_sha256(self, *, path: str, owner: str, timeout_seconds: float = 30.0):
        return hashlib.sha256(self.files[path]).hexdigest()


def _existing_service(tmp_path: Path) -> tuple[ProjectAgentService, _ExistingSource]:
    store = SQLiteProjectStore(tmp_path / "projects.db")
    source = _ExistingSource()
    importer = WorkspaceImporter(
        store=store,
        reader=source,
        owner_roots=("/public/home/{user}",),
        workspace_root=tmp_path / "workspaces",
    )
    return (
        ProjectAgentService(
            store=store,
            workspace_root=tmp_path / "workspaces",
            sandbox=SandboxExecutor(store=store),
            importer=importer,
        ),
        source,
    )


def test_blank_project_is_idempotent_and_owner_scoped(tmp_path: Path) -> None:
    service = _service(tmp_path)

    first = service.create_project(
        owner="alice",
        origin="blank",
        goal="build an experiment",
        request_key="blank-1",
    )
    replay = service.create_project(
        owner="alice",
        origin="blank",
        goal="build an experiment",
        request_key="blank-1",
    )

    assert replay.project.project_id == first.project.project_id
    assert replay.workspace.workspace_id == first.workspace.workspace_id
    assert Path(first.workspace.local_root).is_dir()
    with pytest.raises(KeyError):
        service.get_project(first.project.project_id, owner="bob")


def test_blueprint_patch_diff_and_sandbox_form_reviewable_view(tmp_path: Path) -> None:
    service = _service(tmp_path)
    created = service.create_project(
        owner="alice",
        origin="blank",
        goal="build an experiment",
        request_key="blank-2",
    )
    blueprint = ProjectBlueprint(
        goal="build an experiment",
        entrypoints=("main.py",),
        files=(
            ProjectFile(
                path="main.py", purpose="entrypoint", classification="editable"
            ),
        ),
        validations=(
            ProjectValidation(
                validation_id="syntax",
                execution="sandbox",
                argv=("python", "-m", "py_compile", "main.py"),
                expected_outputs=(),
            ),
        ),
        contract_intent=ProjectContractIntent(
            recipe_version_id=None, resource_hints={}
        ),
        expected_outputs=(),
        dependencies=(),
        open_questions=(),
    )
    service.save_blueprint(
        created.project.project_id,
        owner="alice",
        expected_version=created.project.version,
        blueprint=blueprint,
    )

    change_set = service.apply_patches(
        project_id=created.project.project_id,
        workspace_id=created.workspace.workspace_id,
        owner="alice",
        patches=(
            ("main.py", None, "create", "print('ok')\n"),
            ("config.yaml", None, "create", "seed: 107\n"),
        ),
    )
    result = service.execute_sandbox(
        project_id=created.project.project_id,
        workspace_id=created.workspace.workspace_id,
        owner="alice",
        change_set_id=change_set.change_set_id,
        argv=("python", "-m", "py_compile", "main.py"),
        timeout=3,
    )
    view = service.get_project(created.project.project_id, owner="alice")

    assert result.status == "succeeded"
    assert view.change_sets[0].state.value == "reviewable"
    assert [item.path for item in view.change_sets[0].files] == [
        "main.py",
        "config.yaml",
    ]
    assert service.get_diff(
        change_set.change_set_id,
        owner="alice",
        project_id=created.project.project_id,
        workspace_id=created.workspace.workspace_id,
    ).startswith("--- a/main.py")
    persisted_digest = hashlib.sha256(
        Path(created.workspace.local_root, "main.py").read_bytes()
    ).hexdigest()
    assert persisted_digest == view.change_sets[0].files[0].after_sha256


def test_patch_batch_is_prevalidated_before_any_file_is_mutated(tmp_path: Path) -> None:
    service = _service(tmp_path)
    created = service.create_project(
        owner="alice", origin="blank", goal="atomic edits", request_key="atomic"
    )

    with pytest.raises(WorkspaceConflict, match="no longer exists"):
        service.apply_patches(
            project_id=created.project.project_id,
            workspace_id=created.workspace.workspace_id,
            owner="alice",
            patches=(
                ("would-have-been-created.py", None, "create", "print(1)\n"),
                ("missing.py", "0" * 64, "modify", "print(2)\n"),
            ),
        )

    assert not Path(
        created.workspace.local_root, "would-have-been-created.py"
    ).exists()
    assert service.get_project(created.project.project_id, owner="alice").change_sets == ()


def test_existing_origin_produces_reviewable_multifile_copy_without_source_mutation(
    tmp_path: Path,
) -> None:
    service, source = _existing_service(tmp_path)
    original = dict(source.files)
    created = service.create_project(
        owner="alice",
        origin="existing",
        goal="edit an existing experiment safely",
        request_key="existing",
        source_ref="/public/home/alice/exp",
    )
    digests = {
        item.path: item.source_sha256 for item in created.workspace.snapshot.entries
    }

    change_set = service.apply_patches(
        project_id=created.project.project_id,
        workspace_id=created.workspace.workspace_id,
        owner="alice",
        patches=(
            ("main.py", digests["main.py"], "modify", "print('workspace')\n"),
            ("config.yaml", digests["config.yaml"], "modify", "seed: 107\n"),
        ),
    )
    service.execute_sandbox(
        project_id=created.project.project_id,
        workspace_id=created.workspace.workspace_id,
        owner="alice",
        change_set_id=change_set.change_set_id,
        argv=("python", "-m", "py_compile", "main.py"),
        timeout=3,
    )
    persisted = service.get_project(created.project.project_id, owner="alice")

    assert source.files == original
    assert len(persisted.change_sets[0].files) == 2
    assert persisted.change_sets[0].state.value == "reviewable"


def test_project_tool_handlers_reject_cross_project_workspace(tmp_path: Path) -> None:
    service = _service(tmp_path)
    first = service.create_project(
        owner="alice", origin="blank", goal="one", request_key="one"
    )
    second = service.create_project(
        owner="alice", origin="blank", goal="two", request_key="two"
    )
    handler = service.build_tool_handlers()["workspace_read"]

    with pytest.raises(Exception, match="Workspace"):
        handler(
            "alice",
            {
                "project_id": first.project.project_id,
                "workspace_id": second.workspace.workspace_id,
                "path": "main.py",
            },
        )


def test_project_blueprint_tool_saves_typed_blueprint_and_enforces_binding(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    first = service.create_project(
        owner="alice", origin="blank", goal="heat", request_key="heat-one"
    )
    second = service.create_project(
        owner="alice", origin="blank", goal="other", request_key="heat-two"
    )
    blueprint = ProjectBlueprint(
        goal="Verify second-order heat diffusion convergence.",
        entrypoints=("scripts/run_experiment.sh",),
        files=(
            ProjectFile(
                path="src/heat2d.c", purpose="OpenMP solver", classification="editable"
            ),
        ),
        validations=(
            ProjectValidation(
                validation_id="static-project-check",
                execution="sandbox",
                argv=("python3", "scripts/validate_project.py"),
                expected_outputs=(),
            ),
            ProjectValidation(
                validation_id="heat-slurm-validation",
                execution="slurm",
                argv=("bash", "scripts/run_experiment.sh"),
                expected_outputs=("convergence.json", "scaling.json", "report.md"),
            ),
        ),
        contract_intent=ProjectContractIntent(
            recipe_version_id="recipe_python_cpu@1.0.0",
            resource_hints={"cpus_per_task": 4, "gpus": 0},
        ),
        expected_outputs=(),
        dependencies=(),
        open_questions=(),
    )
    handler = service.build_tool_handlers()["project_blueprint_save"]

    result = handler(
        "alice",
        {
            "project_id": first.project.project_id,
            "workspace_id": first.workspace.workspace_id,
            "expected_version": first.project.version,
            "blueprint": blueprint_payload(blueprint),
        },
    )

    assert result.result["project"]["blueprint"]["entrypoints"] == [
        "scripts/run_experiment.sh"
    ]
    with pytest.raises(Exception, match="Workspace"):
        handler(
            "alice",
            {
                "project_id": first.project.project_id,
                "workspace_id": second.workspace.workspace_id,
                "expected_version": first.project.version + 1,
                "blueprint": blueprint_payload(blueprint),
            },
        )


@pytest.mark.parametrize(
    "profile_id", ["experiment_builder", "run_diagnosis_repair"]
)
def test_workspace_patch_requires_turn_bound_project_capability(
    tmp_path: Path, profile_id: str
) -> None:
    from pilot107.agent.capabilities import AgentCapabilityClaims, AgentCapabilitySigner

    now = datetime(2026, 8, 19, tzinfo=UTC)
    sessions = SQLiteAgentSessionStore(tmp_path / "sessions.db", clock=lambda: now)
    session, _ = sessions.create_session(
        owner="alice",
        request_key="builder-session",
        profile_id=profile_id,
        model_profile_id="faux-default",
        source={"project_id": "project-one", "workspace_id": "workspace-one"},
    )
    turn, _ = sessions.create_turn(
        session_id=session.session_id,
        owner="alice",
        request_key="builder-turn",
        message="edit",
        expected_state_version=session.state_version,
    )
    lease = sessions.claim_turn(turn.turn_id, worker_id="worker", lease_seconds=30)
    assert lease is not None
    signer = AgentCapabilitySigner(b"s" * 32, clock=lambda: int(now.timestamp()))
    called: list[str] = []
    gateway = AgentToolGateway(
        store=sessions,
        signer=signer,
        handlers={},
        profile_handlers={
            "experiment_builder": {
                "workspace_patch": lambda owner, arguments: (
                    called.append(owner)
                    or AgentReadResult(result={"ok": True}, evidence_refs=())
                )
            },
            "run_diagnosis_repair": {
                "workspace_patch": lambda owner, arguments: (
                    called.append(owner)
                    or AgentReadResult(result={"ok": True}, evidence_refs=())
                )
            },
        },
        clock=lambda: now,
    )
    token = signer.sign(
        AgentCapabilityClaims(
            owner="alice",
            session_id=session.session_id,
            turn_id=turn.turn_id,
            state_version=lease.state_version,
            fencing_token=lease.fencing_token,
            profile_id=profile_id,
            tools=frozenset({"workspace_patch"}),
            max_invocations=4,
            max_bytes=64 * 1024,
            expires_at=int(now.timestamp()) + 30,
            project_id="project-one",
            workspace_id="workspace-one",
            operations=frozenset({"write"}),
            max_commands=0,
        )
    )
    invocation = ToolInvocation(
        schema_version=TOOL_INVOCATION_PROTOCOL_VERSION,
        invocation_id="invocation-builder",
        idempotency_key="idem-builder",
        owner="alice",
        session_id=session.session_id,
        turn_id=turn.turn_id,
        state_version=lease.state_version,
        profile_id=profile_id,
        tool_name="workspace_patch",
        arguments={
            "project_id": "project-one",
            "workspace_id": "workspace-other",
            "patches": [
                {
                    "path": "main.py",
                    "expected_source_digest": None,
                    "operation": "create",
                    "content": "print(1)\n",
                }
            ],
        },
        deadline="2026-08-19T00:00:20Z",
    )

    with pytest.raises(AgentToolGatewayError) as denied:
        gateway.invoke(token, invocation)

    assert denied.value.code == "AGENT.TOOL.CAPABILITY_DENIED"
    assert called == []
