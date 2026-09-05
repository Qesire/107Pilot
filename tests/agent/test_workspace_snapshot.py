from __future__ import annotations

import base64
import hashlib
from pathlib import Path

import pytest

from pilot107.adapters.slurm import FileEntry, FileStat
from pilot107.agent.project_store import SQLiteProjectStore
from pilot107.agent.workspace import WorkspaceImporter, WorkspacePolicyError


class ManifestExecutor:
    def __init__(self, *, special_kind: str | None = None, symlink: bool = False) -> None:
        self.files = {
            "/public/home/alice/exp/main.py": b"print(sum([1, 2, 3]))\n",
            "/public/home/alice/exp/data/input.csv": b"value\n1\n2\n3\n",
        }
        self.special_kind = special_kind
        self.symlink = symlink
        self.read_paths: list[str] = []

    def list_dir(self, *, path: str, owner: str, timeout_seconds: float = 30.0):
        assert owner == "alice"
        if path == "/public/home/alice/exp":
            entries = [
                FileEntry(name="data", type="dir", size=0, mtime=10),
                FileEntry(
                    name="main.py",
                    type="file",
                    size=len(self.files[f"{path}/main.py"]),
                    mtime=11,
                ),
                FileEntry(name="model.ckpt", type="file", size=5 * 1024**3, mtime=12),
            ]
            if self.symlink:
                entries.append(FileEntry(name="outside", type="symlink", size=11, mtime=13))
            if self.special_kind:
                entries.append(FileEntry(name="control", type=self.special_kind, size=0, mtime=14))
            return entries
        if path == "/public/home/alice/exp/data":
            return [
                FileEntry(
                    name="input.csv",
                    type="file",
                    size=len(self.files[f"{path}/input.csv"]),
                    mtime=15,
                )
            ]
        raise AssertionError(path)

    def stat_path(self, *, path: str, owner: str, timeout_seconds: float = 30.0):
        assert path == "/public/home/alice/exp"
        assert owner == "alice"
        return FileStat(path=path, type="dir", size=0, mtime=9)

    def read_bytes_chunk(
        self,
        *,
        path: str,
        offset: int,
        length: int,
        owner: str,
        timeout_seconds: float = 30.0,
    ) -> tuple[str, int]:
        self.read_paths.append(path)
        content = self.files[path]
        return base64.b64encode(content[offset : offset + length]).decode(), len(content)

    def file_sha256(self, *, path: str, owner: str, timeout_seconds: float = 30.0):
        if path.endswith("model.ckpt"):
            raise AssertionError("large weights must not be hashed during import")
        return hashlib.sha256(self.files[path]).hexdigest()


@pytest.fixture
def project_store(tmp_path: Path) -> SQLiteProjectStore:
    return SQLiteProjectStore(tmp_path / "projects.db")


def project(store: SQLiteProjectStore):
    return store.create_project(
        owner="alice",
        origin="existing",
        goal="import an existing experiment",
        request_key="workspace-project",
    )


def test_snapshot_keeps_large_weights_metadata_only(
    tmp_path: Path,
    project_store: SQLiteProjectStore,
) -> None:
    reader = ManifestExecutor()
    importer = WorkspaceImporter(
        store=project_store,
        reader=reader,
        owner_roots=("/public/home/{user}",),
        workspace_root=tmp_path / "workspaces",
    )

    workspace = importer.create(
        project(project_store),
        source_ref="/public/home/alice/exp",
    )

    weight = next(item for item in workspace.snapshot.entries if item.path == "model.ckpt")
    assert weight.classification == "metadata_only"
    assert weight.content_ref is None
    assert weight.size_bytes == 5 * 1024**3
    assert "/public/home/alice/exp/model.ckpt" not in reader.read_paths
    assert (Path(workspace.local_root) / "main.py").read_text() == "print(sum([1, 2, 3]))\n"
    assert not (Path(workspace.local_root) / "data/input.csv").exists()
    assert project_store.get_workspace(workspace.workspace_id, owner="alice") == workspace


@pytest.mark.parametrize(
    "source_ref",
    [
        "/public/home/bob/exp",
        "/public/home/alice/../bob/exp",
        "../relative",
    ],
)
def test_import_rejects_cross_owner_and_traversal_paths(
    tmp_path: Path,
    project_store: SQLiteProjectStore,
    source_ref: str,
) -> None:
    importer = WorkspaceImporter(
        store=project_store,
        reader=ManifestExecutor(),
        owner_roots=("/public/home/{user}",),
        workspace_root=tmp_path / "workspaces",
    )

    with pytest.raises(WorkspacePolicyError):
        importer.create(project(project_store), source_ref=source_ref)


def test_import_rejects_symlinks_even_when_the_relay_cannot_prove_the_target(
    tmp_path: Path,
    project_store: SQLiteProjectStore,
) -> None:
    importer = WorkspaceImporter(
        store=project_store,
        reader=ManifestExecutor(symlink=True),
        owner_roots=("/public/home/{user}",),
        workspace_root=tmp_path / "workspaces",
    )

    with pytest.raises(WorkspacePolicyError, match="symlink"):
        importer.create(project(project_store), source_ref="/public/home/alice/exp")


@pytest.mark.parametrize("kind", ["socket", "fifo", "device", "other"])
def test_import_rejects_special_files(
    tmp_path: Path,
    project_store: SQLiteProjectStore,
    kind: str,
) -> None:
    importer = WorkspaceImporter(
        store=project_store,
        reader=ManifestExecutor(special_kind=kind),
        owner_roots=("/public/home/{user}",),
        workspace_root=tmp_path / "workspaces",
    )

    with pytest.raises(WorkspacePolicyError, match="unsupported file type"):
        importer.create(project(project_store), source_ref="/public/home/alice/exp")


def test_import_rejects_untrusted_directory_entry_names(
    tmp_path: Path,
    project_store: SQLiteProjectStore,
) -> None:
    reader = ManifestExecutor()
    original = reader.list_dir

    def malicious_list(**kwargs):
        if kwargs["path"] == "/public/home/alice/exp":
            return [FileEntry(name="../escape.py", type="file", size=1, mtime=1)]
        return original(**kwargs)

    reader.list_dir = malicious_list  # type: ignore[method-assign]
    importer = WorkspaceImporter(
        store=project_store,
        reader=reader,
        owner_roots=("/public/home/{user}",),
        workspace_root=tmp_path / "workspaces",
    )

    with pytest.raises(WorkspacePolicyError, match="entry name"):
        importer.create(project(project_store), source_ref="/public/home/alice/exp")
