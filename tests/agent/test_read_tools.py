from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from pilot107.api.evidence_query import EvidenceQueryService
from pilot107.core.code_context import LocalWorkspaceReader
from pilot107.core.run_store import RunStore
from pilot107.worker.evidence import EvidenceStore


def _api():
    from pilot107.agent.read_tools import AgentReadContext, build_a1_read_handlers
    from pilot107.agent.tool_gateway import AgentToolGatewayError

    return AgentReadContext, build_a1_read_handlers, AgentToolGatewayError


def _workspace(root: Path) -> Path:
    workspace = root / "alice" / "project"
    workspace.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    (workspace / "README.md").write_text("alpha\nneedle here\nomega\n", encoding="utf-8")
    (workspace / "large.txt").write_text("x" * (70 * 1024), encoding="utf-8")
    (workspace / ".git" / "private-token").write_text("never", encoding="utf-8")
    (workspace / "linked.txt").symlink_to(workspace / ".git" / "private-token")
    subprocess.run(
        ["git", "-C", str(workspace), "add", "README.md", "large.txt", "linked.txt"],
        check=True,
    )
    return workspace


def _context(root: Path):
    AgentReadContext, build_handlers, error_type = _api()
    database = root / "runtime.db"
    runs = RunStore(database)
    evidence = EvidenceStore(root / "evidence")
    return (
        AgentReadContext(
            platform_snapshot_store=None,
            run_store=runs,
            evidence_query=EvidenceQueryService(store=runs, evidence_store=evidence),
            workspace_reader=LocalWorkspaceReader(allowed_roots=(root / "alice",)),
            workspace_root_templates=(str(root / "{user}"),),
        ),
        build_handlers,
        error_type,
        runs,
        evidence,
    )


def test_workspace_handlers_enforce_roots_traversal_symlinks_and_bounds(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    context, build_handlers, error_type, _, _ = _context(tmp_path)
    handlers = build_handlers(context)

    listed = handlers["workspace_list"]("alice", {"workspace": str(workspace)})
    assert {item["path"] for item in listed.result["items"]} == {
        "README.md",
        "large.txt",
        "linked.txt",
    }
    assert all("target" not in item for item in listed.result["items"])
    read = handlers["workspace_read"](
        "alice", {"workspace": str(workspace), "path": "large.txt"}
    )
    assert len(read.result["content"].encode()) == 64 * 1024
    assert read.result["truncated"] is True

    for path in ("../outside", ".git/private-token", "linked.txt"):
        with pytest.raises(error_type):
            handlers["workspace_read"](
                "alice", {"workspace": str(workspace), "path": path}
            )
    with pytest.raises(error_type):
        handlers["workspace_read"](
            "bob", {"workspace": str(workspace), "path": "README.md"}
        )


def test_workspace_search_returns_bounded_snippets(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    context, build_handlers, _, _, _ = _context(tmp_path)

    result = build_handlers(context)["workspace_search"](
        "alice", {"workspace": str(workspace), "query": "needle"}
    )

    assert result.result["matches"] == [
        {"path": "README.md", "line": 2, "snippet": "needle here"}
    ]
    assert result.evidence_refs == (f"workspace:{workspace}:README.md:2",)


@pytest.mark.parametrize(("count", "truncated"), [(500, False), (501, True)])
def test_workspace_list_reports_truncation_only_when_more_paths_exist(
    tmp_path: Path, count: int, truncated: bool
) -> None:
    context, build_handlers, _, _, _ = _context(tmp_path)
    workspace = tmp_path / "alice" / "project"
    context = type(context)(
        platform_snapshot_store=context.platform_snapshot_store,
        run_store=context.run_store,
        evidence_query=context.evidence_query,
        workspace_reader=_ListingReader(workspace, count),
        workspace_root_templates=context.workspace_root_templates,
    )

    result = build_handlers(context)["workspace_list"](
        "alice", {"workspace": str(workspace)}
    )

    assert len(result.result["items"]) == min(count, 500)
    assert result.result["truncated"] is truncated


def test_run_and_evidence_reads_are_owner_bound_and_redacted(tmp_path: Path) -> None:
    context, build_handlers, error_type, runs, evidence = _context(tmp_path)
    run = runs.create_run(
        run_id="run-1",
        owner="alice",
        workdir="/public/home/alice/project",
        script="echo super-secret-script",
        resource_plan={"cpus_per_task": 2},
    )
    with runs.connect() as connection:
        connection.execute(
            "UPDATE runs SET submit_response_json = ? WHERE run_id = ?",
            ('{"provider_key":"never-return"}', run.run_id),
        )
    artifact = evidence.write_text(
        run_id=run.run_id,
        logical_path="logs/stdout.txt",
        content="visible evidence\n",
        content_type="text/plain",
    )
    runs.upsert_evidence_objects(
        run.run_id,
        [
            {
                "object_id": "evidence-1",
                "category": "logs",
                "logical_path": artifact.logical_path,
                "store_path": str(artifact.path),
                "sha256": artifact.sha256,
                "size_bytes": artifact.size_bytes,
                "mime_type": artifact.content_type,
                "collection_status": "collected",
            }
        ],
    )
    handlers = build_handlers(context)

    run_result = handlers["run_get"]("alice", {"run_id": "run-1"})
    encoded = str(run_result.result)
    assert run_result.result["run_id"] == "run-1"
    assert "super-secret-script" not in encoded
    assert "provider_key" not in encoded
    evidence_result = handlers["evidence_read"](
        "alice", {"run_id": "run-1", "object_id": "evidence-1"}
    )
    assert evidence_result.result["preview"]["content"] == "visible evidence\n"
    assert evidence_result.evidence_refs == ("evidence:run-1:evidence-1",)

    for tool, arguments in (
        ("run_get", {"run_id": "run-1"}),
        ("evidence_read", {"run_id": "run-1", "object_id": "evidence-1"}),
    ):
        with pytest.raises(error_type):
            handlers[tool]("mallory", arguments)


class _ListingReader:
    def __init__(self, workspace: Path, count: int) -> None:
        self.workspace = workspace
        self.count = count

    def resolve_workspace(self, workspace: str) -> str:
        assert workspace == str(self.workspace)
        return workspace

    def git(self, workspace: str, arguments: tuple[str, ...]) -> str:
        assert workspace == str(self.workspace)
        assert arguments == ("ls-files", "-z")
        return "\0".join(f"file-{index:04d}.txt" for index in range(self.count))

    def read_text(self, workspace: str, relative_path: str, *, max_bytes: int) -> str:
        raise AssertionError("workspace_list must not read file contents")
