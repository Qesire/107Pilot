"""Backend-neutral filesystem mechanics for durable Agent Workspace mutation.

This module owns no database authority. PostgreSQL live-head and journal state
live in ``postgres_workspace_durability``. Historical SQLite editor construction
is retired; ``DurableWorkspaceEditor`` is only a fail-closed compatibility shell.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from pilot107.agent.workspace import (
    AgentWorkspaceRecord,
    WorkspaceFileChange,
    _validate_patch_target,
)
from pilot107.agent.workspace_journal import WorkspaceMutationJournal
from pilot107.agent.workspace_live import WorkspaceLiveConflict

_SQLITE_RETIRED = "SQLite durable Workspace editor has been retired"


@dataclass(frozen=True)
class _ManifestEntry:
    path: str
    kind: str
    mode: int
    size_bytes: int | None = None
    sha256: str | None = None


@dataclass(frozen=True)
class _PreparedPatch:
    path: str
    target: Path
    operation: str
    before: bytes
    after: bytes
    existed: bool
    mode: int
    change: WorkspaceFileChange
    unified_diff: str


@dataclass(frozen=True)
class WorkspaceRecoveryReport:
    workspace_id: str
    committed: tuple[str, ...]
    rolled_back: tuple[str, ...]
    conflicted: tuple[str, ...]


class DurableWorkspaceEditor:
    """Rejected legacy SQLite editor name; never a usable mutation authority."""

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise RuntimeError(_SQLITE_RETIRED)


def _capture_manifest(
    workspace: AgentWorkspaceRecord,
) -> tuple[str, dict[str, _ManifestEntry]]:
    root = Path(workspace.local_root)
    info = root.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise WorkspaceLiveConflict("Workspace local root is not a real directory")
    root = root.resolve(strict=True)
    entries: dict[str, _ManifestEntry] = {}
    stack = [root]
    while stack:
        directory = stack.pop()
        for child in sorted(
            os.scandir(directory), key=lambda item: item.name, reverse=True
        ):
            item = child.stat(follow_symlinks=False)
            path = Path(child.path)
            relative = path.relative_to(root).as_posix()
            if stat.S_ISLNK(item.st_mode):
                raise WorkspaceLiveConflict("Workspace live manifest contains a symlink")
            mode = stat.S_IMODE(item.st_mode)
            if stat.S_ISDIR(item.st_mode):
                entries[relative] = _ManifestEntry(relative, "directory", mode)
                stack.append(path)
            elif stat.S_ISREG(item.st_mode):
                entries[relative] = _ManifestEntry(
                    relative,
                    "file",
                    mode,
                    item.st_size,
                    _file_sha256(path),
                )
            else:
                raise WorkspaceLiveConflict(
                    "Workspace live manifest contains unsupported file type"
                )
            if len(entries) > 10_000:
                raise WorkspaceLiveConflict("Workspace live manifest exceeds entry limit")
    return _manifest_digest(entries), entries


def _manifest_digest(entries: Mapping[str, _ManifestEntry]) -> str:
    values: list[dict[str, object]] = []
    for path in sorted(entries):
        item = entries[path]
        value: dict[str, object] = {
            "path": item.path,
            "kind": item.kind,
            "mode": item.mode,
        }
        if item.kind == "file":
            value.update(size_bytes=item.size_bytes, sha256=item.sha256)
        values.append(value)
    encoded = json.dumps(
        {
            "schema_version": "pilot107.workspace-live-manifest/v1",
            "entries": values,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _expected_manifest(
    before: Mapping[str, _ManifestEntry],
    patches: tuple[_PreparedPatch, ...],
) -> dict[str, _ManifestEntry]:
    result = dict(before)
    for item in patches:
        if item.operation == "delete":
            result.pop(item.path, None)
            continue
        parent = PurePosixPath()
        for part in PurePosixPath(item.path).parts[:-1]:
            parent /= part
            relative = parent.as_posix()
            result.setdefault(
                relative,
                _ManifestEntry(relative, "directory", 0o700),
            )
        result[item.path] = _ManifestEntry(
            item.path,
            "file",
            item.mode if item.existed else 0o600,
            len(item.after),
            hashlib.sha256(item.after).hexdigest(),
        )
    return result


def _apply_prepared(item: _PreparedPatch, root: Path) -> None:
    _validate_patch_target(root, item.target)
    if item.operation == "delete":
        current = item.target.resolve(strict=True)
        if not current.is_file() or not current.is_relative_to(root):
            raise WorkspaceLiveConflict("Workspace target changed before delete")
        if _file_sha256(current) != item.change.before_sha256:
            raise WorkspaceLiveConflict("Workspace target changed before delete")
        current.unlink()
        _fsync_dir(current.parent)
        return
    _ensure_parents(root, item.target.parent)
    if item.existed:
        current = item.target.resolve(strict=True)
        if _file_sha256(current) != item.change.before_sha256:
            raise WorkspaceLiveConflict("Workspace target changed before replace")
    elif item.target.exists():
        raise WorkspaceLiveConflict("Workspace create target appeared concurrently")
    _atomic_write(item.target, item.after, item.mode if item.existed else 0o600)


def _write_backup(
    root: Path,
    workspace_root: Path,
    prepared: tuple[_PreparedPatch, ...],
) -> None:
    if root.exists():
        if (root / "manifest.json").is_file():
            return
        raise WorkspaceLiveConflict("Workspace backup exists without durable manifest")
    root.mkdir(parents=True, mode=0o700)
    created_parents: set[str] = set()
    files: list[dict[str, object]] = []
    for index, item in enumerate(prepared):
        parent = item.target.parent
        while parent != workspace_root:
            if not parent.exists():
                created_parents.add(parent.relative_to(workspace_root).as_posix())
            parent = parent.parent
        blob: str | None = None
        if item.existed:
            blob = f"{index:04d}.bin"
            _durable_write(root / blob, item.before, 0o600)
        files.append(
            {
                "path": item.path,
                "existed": item.existed,
                "mode": item.mode,
                "before_sha256": item.change.before_sha256,
                "after_sha256": item.change.after_sha256,
                "blob": blob,
            }
        )
    manifest = {
        "schema_version": "pilot107.workspace-backup/v1",
        "files": files,
        "created_parents": sorted(
            created_parents, key=lambda value: value.count("/")
        ),
    }
    _durable_write(
        root / "manifest.json",
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode(),
        0o600,
    )
    _fsync_dir(root)


def _restore_backup(workspace: AgentWorkspaceRecord, backup: Path) -> None:
    manifest = _backup_manifest(backup)
    root = Path(workspace.local_root).resolve(strict=True)
    files = manifest["files"]
    assert isinstance(files, list)
    for item in files:
        assert isinstance(item, dict)
        target = root.joinpath(*PurePosixPath(str(item["path"])).parts)
        _validate_patch_target(root, target)
        if bool(item["existed"]):
            content = (backup / str(item["blob"])).read_bytes()
            expected = item.get("before_sha256")
            if (
                not isinstance(expected, str)
                or hashlib.sha256(content).hexdigest() != expected
            ):
                raise WorkspaceLiveConflict(
                    "Workspace backup content digest is invalid"
                )
            mode = item.get("mode")
            if isinstance(mode, bool) or not isinstance(mode, int):
                raise WorkspaceLiveConflict("Workspace backup file mode is invalid")
            _ensure_parents(root, target.parent)
            _atomic_write(target, content, mode)
        elif target.exists():
            target_info = target.lstat()
            if stat.S_ISLNK(target_info.st_mode) or not stat.S_ISREG(
                target_info.st_mode
            ):
                raise WorkspaceLiveConflict(
                    "Workspace rollback target is not a regular file"
                )
            target.unlink()
            _fsync_dir(target.parent)
    parents = manifest.get("created_parents", [])
    if not isinstance(parents, list):
        raise WorkspaceLiveConflict("Workspace backup parent provenance is invalid")
    for relative in sorted(
        (str(value) for value in parents),
        key=lambda value: -value.count("/"),
    ):
        candidate = root.joinpath(*PurePosixPath(relative).parts)
        if candidate == root or not candidate.is_relative_to(root):
            raise WorkspaceLiveConflict("Workspace backup parent escaped root")
        try:
            candidate.rmdir()
            _fsync_dir(candidate.parent)
        except OSError:
            pass


def _touched_paths_known(
    workspace: AgentWorkspaceRecord,
    journal: WorkspaceMutationJournal,
) -> bool:
    root = Path(workspace.local_root).resolve(strict=True)
    for item in journal.files:
        target = root.joinpath(*PurePosixPath(item.path).parts)
        if target.is_symlink():
            return False
        if not target.exists():
            if item.before_sha256 is None or item.after_sha256 is None:
                continue
            return False
        if not target.is_file():
            return False
        digest = _file_sha256(target)
        known = {
            value for value in (item.before_sha256, item.after_sha256) if value
        }
        if digest not in known:
            return False
    return True


def _backup_manifest(path: Path) -> dict[str, Any]:
    try:
        value = json.loads((path / "manifest.json").read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkspaceLiveConflict(
            "Workspace durable backup cannot be read"
        ) from exc
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != "pilot107.workspace-backup/v1"
    ):
        raise WorkspaceLiveConflict("Workspace durable backup manifest is invalid")
    files = value.get("files")
    if not isinstance(files, list) or not all(
        isinstance(item, dict) for item in files
    ):
        raise WorkspaceLiveConflict("Workspace durable backup file list is invalid")
    return value


def _ensure_parents(root: Path, parent: Path) -> None:
    if parent == root:
        return
    current = root
    for part in parent.relative_to(root).parts:
        current /= part
        if current.exists():
            info = current.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise WorkspaceLiveConflict(
                    "Workspace parent is not a real directory"
                )
        else:
            current.mkdir(mode=0o700)
            _fsync_dir(current.parent)


def _atomic_write(path: Path, content: bytes, mode: int) -> None:
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as handle:
            temporary = handle.name
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        temporary = None
        _fsync_dir(path.parent)
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)


def _durable_write(path: Path, content: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("xb") as handle:
        os.fchmod(handle.fileno(), mode)
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    _fsync_dir(path.parent)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _fsync_dir(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _remove_tree(path: Path) -> None:
    if not path.exists():
        return
    for child in path.iterdir():
        if child.is_dir() and not child.is_symlink():
            _remove_tree(child)
        else:
            child.unlink(missing_ok=True)
    path.rmdir()


__all__ = ["DurableWorkspaceEditor", "WorkspaceRecoveryReport"]
