from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from pilot107.agent.project_store import SQLiteProjectStore
from pilot107.agent.sandbox import SandboxExecutor, SandboxPolicyError
from pilot107.agent.workspace import (
    AgentWorkspaceRecord,
    WorkspaceEditor,
    WorkspacePatch,
    WorkspaceSnapshot,
)


@pytest.fixture
def sandbox_workspace(tmp_path: Path):
    store = SQLiteProjectStore(tmp_path / "projects.db")
    project = store.create_project(
        owner="alice",
        origin="blank",
        goal="validate code",
        request_key="sandbox-project",
    )
    root = tmp_path / "workspaces" / "alice" / "workspace-sandbox"
    root.mkdir(parents=True)
    (root / "main.py").write_text("print('ok')\n")
    workspace = AgentWorkspaceRecord(
        workspace_id="workspace-sandbox",
        project_id=project.project_id,
        owner="alice",
        local_root=str(root),
        snapshot=WorkspaceSnapshot(
            source_ref="/public/home/alice/project",
            digest="b" * 64,
            entries=(),
            captured_at="2026-08-19T00:00:00Z",
        ),
        created_at="2026-08-19T00:00:00Z",
        updated_at="2026-08-19T00:00:00Z",
    )
    store.save_workspace(workspace)
    return store, workspace


def test_sandbox_runs_argv_only_syntax_check(sandbox_workspace) -> None:
    _, workspace = sandbox_workspace

    result = SandboxExecutor().execute(
        workspace,
        argv=("python", "-m", "py_compile", "main.py"),
        timeout=3,
    )

    assert result.status == "succeeded"
    assert result.exit_code == 0
    assert result.stdout_sha256 == hashlib.sha256(b"").hexdigest()


def test_sandbox_kills_command_at_deadline(sandbox_workspace) -> None:
    _, workspace = sandbox_workspace

    result = SandboxExecutor().execute(
        workspace,
        argv=("python", "-c", "while True: pass"),
        timeout=1,
    )

    assert result.status == "timed_out"
    assert result.exit_code is None


def test_sandbox_rejects_shell_strings_and_unapproved_executables(sandbox_workspace) -> None:
    _, workspace = sandbox_workspace
    sandbox = SandboxExecutor()

    with pytest.raises(SandboxPolicyError, match="tuple"):
        sandbox.execute(workspace, argv="python main.py", timeout=1)  # type: ignore[arg-type]
    with pytest.raises(SandboxPolicyError, match="executable"):
        sandbox.execute(workspace, argv=("/bin/sh", "-c", "id"), timeout=1)


def test_sandbox_stops_output_overflow(sandbox_workspace) -> None:
    _, workspace = sandbox_workspace

    result = SandboxExecutor(max_output_bytes=128).execute(
        workspace,
        argv=("python", "-c", "print('x' * 10000)"),
        timeout=3,
    )

    assert result.status == "failed"
    assert result.limit_reason == "output_limit"
    assert len(result.stdout) <= 128


def test_sandbox_clears_cluster_credentials_and_disables_network(
    sandbox_workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, workspace = sandbox_workspace
    monkeypatch.setenv("PILOT107_AGENTD_TOKEN", "must-not-enter-sandbox")
    program = (
        "import os,socket;"
        "print(os.getenv('PILOT107_AGENTD_TOKEN','cleared'));"
        "s=socket.socket();s.settimeout(.1);"
        "\ntry:s.connect(('127.0.0.1',9));print('network-open')"
        "\nexcept OSError:print('network-blocked')"
    )

    result = SandboxExecutor().execute(
        workspace,
        argv=("python", "-c", program),
        timeout=3,
    )

    assert result.status == "succeeded"
    assert "cleared" in result.stdout
    assert "network-blocked" in result.stdout
    assert "must-not-enter-sandbox" not in result.stdout


def test_sandbox_result_is_persisted_on_change_set(sandbox_workspace) -> None:
    store, workspace = sandbox_workspace
    before = hashlib.sha256(b"print('ok')\n").hexdigest()
    change_set = WorkspaceEditor(store=store).apply_patch(
        workspace.workspace_id,
        "alice",
        "main.py",
        before,
        WorkspacePatch(operation="modify", content="print('changed')\n"),
    )

    SandboxExecutor(store=store).execute(
        workspace,
        argv=("python", "-m", "py_compile", "main.py"),
        timeout=3,
        change_set_id=change_set.change_set_id,
    )

    persisted = store.get_change_set(change_set.change_set_id, owner="alice")
    assert len(persisted.sandbox_results) == 1
    assert persisted.sandbox_results[0].status == "succeeded"
