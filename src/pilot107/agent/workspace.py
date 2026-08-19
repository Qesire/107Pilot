"""Manifest-first import into owner-isolated Agent Workspaces."""

from __future__ import annotations

import base64
import difflib
import hashlib
import json
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
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


class WorkspaceChangeSetState(StrEnum):
    DRAFT = "draft"
    REVIEWABLE = "reviewable"
    APPROVED = "approved"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    CONFLICTED = "conflicted"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class WorkspacePatch:
    operation: Literal["create", "modify", "delete"]
    content: str | None

    def __post_init__(self) -> None:
        if self.operation not in {"create", "modify", "delete"}:
            raise ValueError("Workspace patch operation is invalid")
        if self.operation == "delete" and self.content is not None:
            raise ValueError("delete patches cannot contain content")
        if self.operation != "delete" and not isinstance(self.content, str):
            raise TypeError("create and modify patches require text content")


@dataclass(frozen=True)
class WorkspaceFileChange:
    path: str
    operation: Literal["create", "modify", "delete"]
    before_sha256: str | None
    after_sha256: str | None
    diff_sha256: str
    size_bytes: int


@dataclass(frozen=True)
class SandboxResultRecord:
    result_id: str
    argv: tuple[str, ...]
    status: Literal["succeeded", "failed", "timed_out", "cancelled"]
    exit_code: int | None
    stdout_sha256: str
    stderr_sha256: str


@dataclass(frozen=True)
class WorkspaceApproval:
    actor: str
    approved_digest: str
    approved_at: str


@dataclass(frozen=True)
class WorkspaceChangeSet:
    change_set_id: str
    project_id: str
    workspace_id: str
    owner: str
    base_snapshot_digest: str
    digest: str
    state: WorkspaceChangeSetState
    version: int
    files: tuple[WorkspaceFileChange, ...]
    sandbox_results: tuple[SandboxResultRecord, ...]
    approval: WorkspaceApproval | None
    created_at: str
    updated_at: str
    schema_version: str = "pilot107.workspace-changeset/v1"


@dataclass(frozen=True)
class _PreparedWorkspacePatch:
    target: Path
    before: bytes
    after: bytes
    existed: bool
    change: WorkspaceFileChange
    unified_diff: str


class WorkspaceEditor:
    def __init__(
        self,
        *,
        store: ProjectStore,
        max_file_bytes: int = 8 * 1024 * 1024,
        max_diff_bytes: int = 1024 * 1024,
    ) -> None:
        if max_file_bytes < 1 or max_diff_bytes < 1:
            raise ValueError("Workspace edit limits must be positive")
        self.store = store
        self.max_file_bytes = max_file_bytes
        self.max_diff_bytes = max_diff_bytes

    def apply_patch(
        self,
        workspace_id: str,
        owner: str,
        relative_path: str,
        expected_source_digest: str | None,
        patch: WorkspacePatch,
    ) -> WorkspaceChangeSet:
        return self.apply_patches(
            workspace_id,
            owner,
            ((relative_path, expected_source_digest, patch),),
        )

    def apply_patches(
        self,
        workspace_id: str,
        owner: str,
        patches: tuple[tuple[str, str | None, WorkspacePatch], ...],
    ) -> WorkspaceChangeSet:
        if not isinstance(patches, tuple) or not 1 <= len(patches) <= 256:
            raise WorkspacePolicyError("Workspace patch batch must contain 1 to 256 items")
        workspace = self.store.get_workspace(workspace_id, owner=owner)
        root = Path(workspace.local_root).resolve(strict=True)
        prepared: list[_PreparedWorkspacePatch] = []
        seen: set[str] = set()
        total_diff_bytes = 0
        for relative_path, expected_source_digest, patch in patches:
            try:
                relative = _relative_path(relative_path)
            except (TypeError, ValueError) as exc:
                raise WorkspacePolicyError(str(exc)) from exc
            if relative in seen:
                raise WorkspacePolicyError("Workspace patch batch contains duplicate paths")
            seen.add(relative)
            if not isinstance(patch, WorkspacePatch):
                raise TypeError("patch must be WorkspacePatch")
            target = root.joinpath(*PurePosixPath(relative).parts)
            _validate_patch_target(root, target)
            existing = target.exists()
            if existing:
                resolved = target.resolve(strict=True)
                if not resolved.is_relative_to(root) or not resolved.is_file():
                    raise WorkspacePolicyError(
                        "Workspace patch target is not a contained file"
                    )
                before = resolved.read_bytes()
            else:
                before = b""
            before_digest = hashlib.sha256(before).hexdigest() if existing else None
            if patch.operation == "create":
                if existing or expected_source_digest is not None:
                    raise WorkspaceConflict(
                        "create patch no longer matches an absent source"
                    )
            else:
                if not existing:
                    raise WorkspaceConflict("patch source file no longer exists")
                if expected_source_digest != before_digest:
                    raise WorkspaceConflict("patch source digest no longer matches")
            if not _editable_path(relative):
                raise WorkspacePolicyError(
                    "Workspace patch target is not an editable file type"
                )
            after = (
                b"" if patch.operation == "delete" else (patch.content or "").encode()
            )
            if len(after) > self.max_file_bytes:
                raise WorkspacePolicyError("Workspace patch exceeds the file size limit")
            try:
                before_text = before.decode("utf-8")
                after_text = after.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise WorkspacePolicyError(
                    "Workspace patches require UTF-8 text files"
                ) from exc
            unified = "".join(
                difflib.unified_diff(
                    before_text.splitlines(keepends=True),
                    after_text.splitlines(keepends=True),
                    fromfile=f"a/{relative}",
                    tofile=f"b/{relative}",
                )
            )
            encoded_diff = unified.encode()
            total_diff_bytes += len(encoded_diff)
            if total_diff_bytes > self.max_diff_bytes:
                raise WorkspacePolicyError("Workspace diff exceeds the output limit")
            after_digest = (
                None
                if patch.operation == "delete"
                else hashlib.sha256(after).hexdigest()
            )
            prepared.append(
                _PreparedWorkspacePatch(
                    target=target,
                    before=before,
                    after=after,
                    existed=existing,
                    change=WorkspaceFileChange(
                        path=relative,
                        operation=patch.operation,
                        before_sha256=before_digest,
                        after_sha256=after_digest,
                        diff_sha256=hashlib.sha256(encoded_diff).hexdigest(),
                        size_bytes=len(after),
                    ),
                    unified_diff=unified,
                )
            )
        files = tuple(item.change for item in prepared)
        unified_diff = "".join(item.unified_diff for item in prepared)
        digest = _change_set_digest(workspace, files)
        change_set_id = f"changeset-{digest[:24]}"
        now = _timestamp()
        change_set = WorkspaceChangeSet(
            change_set_id=change_set_id,
            project_id=workspace.project_id,
            workspace_id=workspace.workspace_id,
            owner=owner,
            base_snapshot_digest=workspace.snapshot.digest,
            digest=digest,
            state=WorkspaceChangeSetState.DRAFT,
            version=1,
            files=files,
            sandbox_results=(),
            approval=None,
            created_at=now,
            updated_at=now,
        )
        changed: list[_PreparedWorkspacePatch] = []
        try:
            for item in prepared:
                if item.change.operation == "delete":
                    item.target.unlink()
                else:
                    _atomic_write(item.target, item.after)
                changed.append(item)
            return self.store.save_change_set(change_set, diff_text=unified_diff)
        except Exception:
            for item in reversed(changed):
                if item.existed:
                    _atomic_write(item.target, item.before)
                else:
                    item.target.unlink(missing_ok=True)
            raise

    def diff(self, change_set_id: str, owner: str) -> str:
        return self.store.get_change_set_diff(change_set_id, owner=owner)


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


def change_set_payload(change_set: WorkspaceChangeSet) -> dict[str, Any]:
    approval = None
    if change_set.approval is not None:
        approval = {
            "actor": change_set.approval.actor,
            "approved_digest": change_set.approval.approved_digest,
            "approved_at": change_set.approval.approved_at,
        }
    return {
        "schema_version": change_set.schema_version,
        "change_set_id": change_set.change_set_id,
        "project_id": change_set.project_id,
        "workspace_id": change_set.workspace_id,
        "owner": change_set.owner,
        "base_snapshot_digest": change_set.base_snapshot_digest,
        "digest": change_set.digest,
        "state": change_set.state.value,
        "version": change_set.version,
        "files": [
            {
                "path": item.path,
                "operation": item.operation,
                "before_sha256": item.before_sha256,
                "after_sha256": item.after_sha256,
                "diff_sha256": item.diff_sha256,
                "size_bytes": item.size_bytes,
            }
            for item in change_set.files
        ],
        "sandbox_results": [
            {
                "result_id": item.result_id,
                "argv": list(item.argv),
                "status": item.status,
                "exit_code": item.exit_code,
                "stdout_sha256": item.stdout_sha256,
                "stderr_sha256": item.stderr_sha256,
            }
            for item in change_set.sandbox_results
        ],
        "approval": approval,
        "created_at": change_set.created_at,
        "updated_at": change_set.updated_at,
    }


def change_set_from_payload(value: Mapping[str, Any]) -> WorkspaceChangeSet:
    raw_files = value.get("files")
    raw_results = value.get("sandbox_results")
    if not isinstance(raw_files, list) or not isinstance(raw_results, list):
        raise TypeError("ChangeSet files and sandbox_results must be arrays")
    raw_approval = value.get("approval")
    approval = None
    if raw_approval is not None:
        approval_value = _object(raw_approval, "approval")
        approval = WorkspaceApproval(
            actor=_text(approval_value.get("actor"), "approval actor"),
            approved_digest=_text(
                approval_value.get("approved_digest"), "approved_digest"
            ),
            approved_at=_text(approval_value.get("approved_at"), "approved_at"),
        )
    return WorkspaceChangeSet(
        change_set_id=_text(value.get("change_set_id"), "change_set_id"),
        project_id=_text(value.get("project_id"), "project_id"),
        workspace_id=_text(value.get("workspace_id"), "workspace_id"),
        owner=_text(value.get("owner"), "owner"),
        base_snapshot_digest=_text(
            value.get("base_snapshot_digest"), "base_snapshot_digest"
        ),
        digest=_text(value.get("digest"), "digest"),
        state=WorkspaceChangeSetState(_text(value.get("state"), "state")),
        version=_integer(value.get("version"), "version"),
        files=tuple(
            WorkspaceFileChange(
                path=_text(item.get("path"), "file path"),
                operation=_text(item.get("operation"), "operation"),  # type: ignore[arg-type]
                before_sha256=_optional_text(item.get("before_sha256")),
                after_sha256=_optional_text(item.get("after_sha256")),
                diff_sha256=_text(item.get("diff_sha256"), "diff_sha256"),
                size_bytes=_integer(item.get("size_bytes"), "size_bytes"),
            )
            for item in (_object(item, "file change") for item in raw_files)
        ),
        sandbox_results=tuple(
            SandboxResultRecord(
                result_id=_text(item.get("result_id"), "result_id"),
                argv=tuple(
                    _text(argument, "argument")
                    for argument in _array(item.get("argv"), "argv")
                ),
                status=_text(item.get("status"), "status"),  # type: ignore[arg-type]
                exit_code=_optional_integer(item.get("exit_code"), "exit_code"),
                stdout_sha256=_text(item.get("stdout_sha256"), "stdout_sha256"),
                stderr_sha256=_text(item.get("stderr_sha256"), "stderr_sha256"),
            )
            for item in (_object(item, "sandbox result") for item in raw_results)
        ),
        approval=approval,
        created_at=_text(value.get("created_at"), "created_at"),
        updated_at=_text(value.get("updated_at"), "updated_at"),
        schema_version=_text(value.get("schema_version"), "schema_version"),
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


def _change_set_digest(
    workspace: AgentWorkspaceRecord, file_changes: tuple[WorkspaceFileChange, ...]
) -> str:
    payload = {
        "project_id": workspace.project_id,
        "workspace_id": workspace.workspace_id,
        "base_snapshot_digest": workspace.snapshot.digest,
        "files": [
            {
                "path": file_change.path,
                "operation": file_change.operation,
                "before_sha256": file_change.before_sha256,
                "after_sha256": file_change.after_sha256,
                "diff_sha256": file_change.diff_sha256,
                "size_bytes": file_change.size_bytes,
            }
            for file_change in file_changes
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def _editable_path(relative: str) -> bool:
    path = PurePosixPath(relative)
    return path.name in _EDITABLE_NAMES or path.suffix.lower() in _EDITABLE_SUFFIXES


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


def _validate_patch_target(root: Path, target: Path) -> None:
    """Reject every symlink hop before any workspace mutation occurs."""
    if target.is_symlink():
        raise WorkspacePolicyError("Workspace patch target cannot be a symlink")
    current = target.parent
    while current != root:
        if current.is_symlink():
            raise WorkspacePolicyError("Workspace patch path cannot traverse a symlink")
        if root not in current.parents:
            raise WorkspacePolicyError("Workspace patch target escaped the workspace")
        current = current.parent
    resolved = target.resolve(strict=False)
    if resolved != root and root not in resolved.parents:
        raise WorkspacePolicyError("Workspace patch target escaped the workspace")


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


def _optional_integer(value: object, label: str) -> int | None:
    return None if value is None else _integer(value, label)


def _array(value: object, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise TypeError(f"{label} must be an array")
    return value
