"""Manifest-first import into owner-isolated Agent Workspaces."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Protocol

from pilot107.adapters.slurm import FileEntry, FileStat
from pilot107.agent.project import ExperimentProjectSessionRecord
from pilot107.agent.project_store import ProjectStore
from pilot107.core.file_uploads import OwnerPathAuthorizationError, authorize_owner_path
from pilot107.core.identity import is_safe_username

WorkspaceClassification = Literal["editable", "read_only", "metadata_only", "excluded"]

_EDITABLE_SUFFIXES = frozenset(
    {
        ".c",
        ".cc",
        ".cfg",
        ".cpp",
        ".cu",
        ".f",
        ".f90",
        ".go",
        ".h",
        ".hpp",
        ".ini",
        ".jl",
        ".json",
        ".md",
        ".py",
        ".r",
        ".rs",
        ".sh",
        ".toml",
        ".txt",
        ".yaml",
        ".yml",
    }
)
_EDITABLE_NAMES = frozenset({"Dockerfile", "Makefile", "requirements.txt"})
_METADATA_SUFFIXES = frozenset(
    {
        ".bin",
        ".ckpt",
        ".gz",
        ".h5",
        ".hdf5",
        ".npy",
        ".npz",
        ".onnx",
        ".parquet",
        ".pt",
        ".pth",
        ".tar",
        ".zip",
    }
)
_EXCLUDED_DIRECTORIES = frozenset(
    {".git", ".hg", ".svn", ".venv", "__pycache__", "node_modules"}
)


class WorkspacePolicyError(ValueError):
    """The source tree or requested import exceeds a closed Workspace policy."""


class WorkspaceConflict(RuntimeError):
    """The source changed while its manifest was being copied."""


class WorkspaceSourceReader(Protocol):
    def stat_path(
        self, *, path: str, owner: str, timeout_seconds: float = 30.0
    ) -> FileStat: ...

    def list_dir(
        self, *, path: str, owner: str, timeout_seconds: float = 30.0
    ) -> list[FileEntry]: ...

    def read_bytes_chunk(
        self,
        *,
        path: str,
        offset: int,
        length: int,
        owner: str,
        timeout_seconds: float = 30.0,
    ) -> tuple[str, int]: ...

    def file_sha256(
        self, *, path: str, owner: str, timeout_seconds: float = 30.0
    ) -> str: ...


@dataclass(frozen=True)
class WorkspaceEntry:
    path: str
    kind: Literal["file", "directory"]
    classification: WorkspaceClassification
    size_bytes: int
    mtime_epoch: int
    source_sha256: str | None
    content_ref: str | None

    def __post_init__(self) -> None:
        _relative_path(self.path)
        if self.kind not in {"file", "directory"}:
            raise ValueError("Workspace entry kind is invalid")
        if self.classification not in {
            "editable",
            "read_only",
            "metadata_only",
            "excluded",
        }:
            raise ValueError("Workspace classification is invalid")
        if self.size_bytes < 0 or self.mtime_epoch < 0:
            raise ValueError("Workspace entry metadata is invalid")
        if self.source_sha256 is not None:
            _digest(self.source_sha256, "Workspace source digest")


@dataclass(frozen=True)
class WorkspaceSnapshot:
    source_ref: str
    digest: str
    entries: tuple[WorkspaceEntry, ...]
    captured_at: str

    def __post_init__(self) -> None:
        if not self.source_ref.startswith("/"):
            raise ValueError("Workspace source_ref must be absolute")
        _digest(self.digest, "Workspace snapshot digest")
        object.__setattr__(self, "entries", tuple(self.entries))
        if any(not isinstance(item, WorkspaceEntry) for item in self.entries):
            raise TypeError("Workspace snapshot contains an invalid entry")


@dataclass(frozen=True)
class AgentWorkspaceRecord:
    workspace_id: str
    project_id: str
    owner: str
    local_root: str
    snapshot: WorkspaceSnapshot
    created_at: str
    updated_at: str

    def __post_init__(self) -> None:
        if not self.workspace_id.startswith("workspace-"):
            raise ValueError("workspace_id is invalid")
        if not self.project_id.startswith("project-"):
            raise ValueError("project_id is invalid")
        if not is_safe_username(self.owner):
            raise ValueError("Workspace owner is invalid")
        if not Path(self.local_root).is_absolute():
            raise ValueError("Workspace local_root must be absolute")
        if not isinstance(self.snapshot, WorkspaceSnapshot):
            raise TypeError("snapshot must be WorkspaceSnapshot")


class WorkspaceImporter:
    def __init__(
        self,
        *,
        store: ProjectStore,
        reader: WorkspaceSourceReader,
        owner_roots: tuple[str, ...] | list[str],
        workspace_root: Path,
        max_copy_file_bytes: int = 8 * 1024 * 1024,
        max_total_copy_bytes: int = 64 * 1024 * 1024,
        max_entries: int = 10_000,
        max_depth: int = 32,
        read_chunk_bytes: int = 1024 * 1024,
    ) -> None:
        if not owner_roots:
            raise ValueError("Workspace owner_roots must be explicit")
        for value, label in (
            (max_copy_file_bytes, "max_copy_file_bytes"),
            (max_total_copy_bytes, "max_total_copy_bytes"),
            (max_entries, "max_entries"),
            (max_depth, "max_depth"),
            (read_chunk_bytes, "read_chunk_bytes"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{label} must be a positive integer")
        self.store = store
        self.reader = reader
        self.owner_roots = tuple(owner_roots)
        self.workspace_root = workspace_root.resolve()
        self.max_copy_file_bytes = max_copy_file_bytes
        self.max_total_copy_bytes = max_total_copy_bytes
        self.max_entries = max_entries
        self.max_depth = max_depth
        self.read_chunk_bytes = read_chunk_bytes

    def create(
        self,
        project: ExperimentProjectSessionRecord,
        *,
        source_ref: str,
    ) -> AgentWorkspaceRecord:
        if not isinstance(project, ExperimentProjectSessionRecord):
            raise TypeError("project must be an ExperimentProjectSessionRecord")
        if not is_safe_username(project.owner):
            raise WorkspacePolicyError("Workspace owner is invalid")
        try:
            source = authorize_owner_path(
                self.owner_roots,
                owner=project.owner,
                target_path=source_ref,
            )
        except (OwnerPathAuthorizationError, ValueError) as exc:
            raise WorkspacePolicyError(str(exc)) from exc
        source_stat = self.reader.stat_path(path=source, owner=project.owner)
        if source_stat.type != "dir":
            raise WorkspacePolicyError("Workspace source must be a directory")

        manifest = self._scan(source, owner=project.owner)
        digest = _snapshot_digest(source, manifest)
        workspace_digest = hashlib.sha256(
            f"{project.project_id}\0{digest}".encode()
        ).hexdigest()[:24]
        workspace_id = f"workspace-{workspace_digest}"
        final_entries = tuple(
            replace(
                entry,
                content_ref=(
                    f"workspace:{workspace_id}:{entry.path}"
                    if entry.kind == "file" and entry.classification == "editable"
                    else None
                ),
            )
            for entry in manifest
        )
        local_root = self._workspace_path(project.owner, workspace_id)
        local_root.mkdir(parents=True, exist_ok=True)
        self._copy_editable(
            source,
            owner=project.owner,
            entries=final_entries,
            local_root=local_root,
        )
        now = _timestamp()
        record = AgentWorkspaceRecord(
            workspace_id=workspace_id,
            project_id=project.project_id,
            owner=project.owner,
            local_root=str(local_root),
            snapshot=WorkspaceSnapshot(
                source_ref=source,
                digest=digest,
                entries=final_entries,
                captured_at=now,
            ),
            created_at=now,
            updated_at=now,
        )
        return self.store.save_workspace(record)

    def _scan(self, source: str, *, owner: str) -> tuple[WorkspaceEntry, ...]:
        entries: list[WorkspaceEntry] = []
        stack: list[tuple[str, PurePosixPath, int]] = [
            (source, PurePosixPath("."), 0)
        ]
        while stack:
            remote_directory, relative_directory, depth = stack.pop()
            if depth > self.max_depth:
                raise WorkspacePolicyError("Workspace source exceeds maximum depth")
            children = self.reader.list_dir(path=remote_directory, owner=owner)
            for child in sorted(children, key=lambda item: item.name, reverse=True):
                _entry_name(child.name)
                relative = (
                    PurePosixPath(child.name)
                    if str(relative_directory) == "."
                    else relative_directory / child.name
                )
                relative_text = relative.as_posix()
                remote_path = f"{source}/{relative_text}"
                if len(entries) >= self.max_entries:
                    raise WorkspacePolicyError("Workspace source exceeds maximum entries")
                if child.type == "symlink":
                    raise WorkspacePolicyError(
                        f"symlink cannot be imported safely: {relative_text}"
                    )
                if child.type == "dir":
                    excluded = child.name in _EXCLUDED_DIRECTORIES
                    entries.append(
                        WorkspaceEntry(
                            path=relative_text,
                            kind="directory",
                            classification="excluded" if excluded else "read_only",
                            size_bytes=child.size,
                            mtime_epoch=child.mtime,
                            source_sha256=None,
                            content_ref=None,
                        )
                    )
                    if not excluded:
                        stack.append((remote_path, relative, depth + 1))
                    continue
                if child.type != "file":
                    raise WorkspacePolicyError(
                        f"unsupported file type {child.type}: {relative_text}"
                    )
                classification = self._classification(relative, child.size)
                source_sha256 = None
                if classification in {"editable", "read_only"}:
                    source_sha256 = self.reader.file_sha256(path=remote_path, owner=owner)
                    _digest(source_sha256, f"source digest for {relative_text}")
                entries.append(
                    WorkspaceEntry(
                        path=relative_text,
                        kind="file",
                        classification=classification,
                        size_bytes=child.size,
                        mtime_epoch=child.mtime,
                        source_sha256=source_sha256,
                        content_ref=None,
                    )
                )
        return tuple(sorted(entries, key=lambda item: item.path))

    def _classification(self, relative: PurePosixPath, size: int) -> WorkspaceClassification:
        if size < 0:
            raise WorkspacePolicyError(f"negative source size: {relative.as_posix()}")
        suffix = relative.suffix.lower()
        if suffix in _METADATA_SUFFIXES or size > self.max_copy_file_bytes:
            return "metadata_only"
        if relative.name in _EDITABLE_NAMES or suffix in _EDITABLE_SUFFIXES:
            return "editable"
        return "read_only"

    def _workspace_path(self, owner: str, workspace_id: str) -> Path:
        owner_root = (self.workspace_root / owner).resolve()
        candidate = (owner_root / workspace_id).resolve()
        if candidate.parent != owner_root:
            raise WorkspacePolicyError("Workspace destination escaped the owner root")
        return candidate

    def _copy_editable(
        self,
        source: str,
        *,
        owner: str,
        entries: tuple[WorkspaceEntry, ...],
        local_root: Path,
    ) -> None:
        editable = [
            entry
            for entry in entries
            if entry.kind == "file" and entry.classification == "editable"
        ]
        total = sum(entry.size_bytes for entry in editable)
        if total > self.max_total_copy_bytes:
            raise WorkspacePolicyError("Workspace editable files exceed the copy budget")
        for entry in editable:
            remote_path = f"{source}/{entry.path}"
            content = self._read_file(
                remote_path,
                owner=owner,
                expected_size=entry.size_bytes,
            )
            actual = hashlib.sha256(content).hexdigest()
            if actual != entry.source_sha256:
                raise WorkspaceConflict(f"source changed while copying {entry.path}")
            destination = local_root.joinpath(*PurePosixPath(entry.path).parts)
            if local_root not in destination.resolve().parents:
                raise WorkspacePolicyError("Workspace file escaped the local root")
            _atomic_write(destination, content)

    def _read_file(self, remote_path: str, *, owner: str, expected_size: int) -> bytes:
        blocks: list[bytes] = []
        offset = 0
        while offset < expected_size:
            length = min(self.read_chunk_bytes, expected_size - offset)
            encoded, total_size = self.reader.read_bytes_chunk(
                path=remote_path,
                offset=offset,
                length=length,
                owner=owner,
            )
            if total_size != expected_size:
                raise WorkspaceConflict(f"source size changed while copying {remote_path}")
            try:
                block = base64.b64decode(encoded, validate=True)
            except ValueError as exc:
                raise WorkspaceConflict("source reader returned invalid base64") from exc
            if not block or len(block) > length:
                raise WorkspaceConflict("source reader returned an invalid byte range")
            blocks.append(block)
            offset += len(block)
        return b"".join(blocks)


def workspace_payload(record: AgentWorkspaceRecord) -> dict[str, Any]:
    return {
        "workspace_id": record.workspace_id,
        "project_id": record.project_id,
        "owner": record.owner,
        "local_root": record.local_root,
        "snapshot": {
            "source_ref": record.snapshot.source_ref,
            "digest": record.snapshot.digest,
            "captured_at": record.snapshot.captured_at,
            "entries": [
                {
                    "path": item.path,
                    "kind": item.kind,
                    "classification": item.classification,
                    "size_bytes": item.size_bytes,
                    "mtime_epoch": item.mtime_epoch,
                    "source_sha256": item.source_sha256,
                    "content_ref": item.content_ref,
                }
                for item in record.snapshot.entries
            ],
        },
        "created_at": record.created_at,
        "updated_at": record.updated_at,
    }


def workspace_from_payload(value: Mapping[str, Any]) -> AgentWorkspaceRecord:
    snapshot = _object(value.get("snapshot"), "snapshot")
    raw_entries = snapshot.get("entries")
    if not isinstance(raw_entries, list):
        raise TypeError("Workspace entries must be an array")
    entries = tuple(
        WorkspaceEntry(
            path=_text(item.get("path"), "path"),
            kind=_text(item.get("kind"), "kind"),  # type: ignore[arg-type]
            classification=_text(item.get("classification"), "classification"),  # type: ignore[arg-type]
            size_bytes=_integer(item.get("size_bytes"), "size_bytes"),
            mtime_epoch=_integer(item.get("mtime_epoch"), "mtime_epoch"),
            source_sha256=_optional_text(item.get("source_sha256")),
            content_ref=_optional_text(item.get("content_ref")),
        )
        for item in (_object(entry, "entry") for entry in raw_entries)
    )
    return AgentWorkspaceRecord(
        workspace_id=_text(value.get("workspace_id"), "workspace_id"),
        project_id=_text(value.get("project_id"), "project_id"),
        owner=_text(value.get("owner"), "owner"),
        local_root=_text(value.get("local_root"), "local_root"),
        snapshot=WorkspaceSnapshot(
            source_ref=_text(snapshot.get("source_ref"), "source_ref"),
            digest=_text(snapshot.get("digest"), "digest"),
            entries=entries,
            captured_at=_text(snapshot.get("captured_at"), "captured_at"),
        ),
        created_at=_text(value.get("created_at"), "created_at"),
        updated_at=_text(value.get("updated_at"), "updated_at"),
    )


def _snapshot_digest(source: str, entries: tuple[WorkspaceEntry, ...]) -> str:
    payload = {
        "source_ref": source,
        "entries": [
            {
                "path": item.path,
                "kind": item.kind,
                "classification": item.classification,
                "size_bytes": item.size_bytes,
                "mtime_epoch": item.mtime_epoch,
                "source_sha256": item.source_sha256,
            }
            for item in entries
        ],
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _entry_name(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or "\0" in value
    ):
        raise WorkspacePolicyError(f"unsafe directory entry name: {value!r}")
    return value


def _relative_path(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or value.startswith("/")
        or any(part in {"", ".", ".."} for part in path.parts)
        or "\\" in value
        or "\0" in value
    ):
        raise ValueError("Workspace path must be a contained relative path")
    return value


def _digest(value: str, label: str) -> str:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise WorkspaceConflict(f"{label} is invalid")
    return value


def _atomic_write(destination: Path, content: bytes) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            delete=False,
        ) as handle:
            temporary_name = handle.name
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_name, 0o600)
        os.replace(temporary_name, destination)
        temporary_name = None
    finally:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)


def _timestamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _object(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} must be an object")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be a string")
    return value


def _optional_text(value: object) -> str | None:
    return None if value is None else _text(value, "optional value")


def _integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{label} must be an integer")
    return value
