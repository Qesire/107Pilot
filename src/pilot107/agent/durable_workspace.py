"""Crash-recoverable Workspace editing over immutable imported snapshots.

This module deliberately wraps the existing :class:`WorkspaceEditor` rather
than changing ``AgentWorkspaceRecord`` or ``WorkspaceChangeSet``.  The imported
snapshot remains immutable; every local mutation is serialized by an OS lock,
a fenced live-head lease, a durable backup, and a write-ahead journal.
"""

from __future__ import annotations

import base64
import difflib
import fcntl
import hashlib
import json
import os
import stat
import tempfile
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import IO, Any, Iterator

from pilot107.agent.project_store import ProjectStore
from pilot107.agent.workspace import (
    AgentWorkspaceRecord,
    WorkspaceChangeSet,
    WorkspaceChangeSetState,
    WorkspaceConflict,
    WorkspaceEditor,
    WorkspaceFileChange,
    WorkspacePatch,
    WorkspacePolicyError,
    _change_set_digest,
    _editable_path,
    _relative_path,
    _timestamp,
    _validate_patch_target,
)
from pilot107.agent.workspace_journal import (
    SQLiteWorkspaceMutationJournalStore,
    WorkspaceMutationFile,
    WorkspaceMutationJournal,
    WorkspaceMutationState,
)
from pilot107.agent.workspace_live import (
    SQLiteWorkspaceLiveStore,
    WorkspaceLiveConflict,
    WorkspaceWriterLease,
)


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


class DurableWorkspaceEditor(WorkspaceEditor):
    """WorkspaceEditor with live revision, WAL journal, backup, and recovery.

    The implementation is intentionally SQLite-scoped because the live-head and
    journal schemas currently share the ProjectStore SQLite transaction domain.
    PostgreSQL parity is a separate AC4 slice and must not be silently faked.
    """

    def __init__(
        self,
        *,
        store: ProjectStore,
        state_root: Path,
        lease_seconds: int = 60,
        max_file_bytes: int = 8 * 1024 * 1024,
        max_diff_bytes: int = 1024 * 1024,
        crash_hook: Callable[[str], None] | None = None,
        clock: Callable[[], Any] | None = None,
    ) -> None:
        super().__init__(
            store=store,
            max_file_bytes=max_file_bytes,
            max_diff_bytes=max_diff_bytes,
        )
        db_path = getattr(store, "db_path", None)
        if not isinstance(db_path, Path):
            raise TypeError("DurableWorkspaceEditor currently requires SQLiteProjectStore")
        if isinstance(lease_seconds, bool) or not 1 <= lease_seconds <= 86_400:
            raise ValueError("lease_seconds must be between 1 and 86400")
        self.state_root = state_root.resolve()
        self.lock_root = self.state_root / "locks"
        self.backup_root = self.state_root / "backups"
        self.lock_root.mkdir(parents=True, exist_ok=True)
        self.backup_root.mkdir(parents=True, exist_ok=True)
        self.lease_seconds = lease_seconds
        self.crash_hook = crash_hook
        # Construct live-store first: journal migration 007 references table 006.
        self.live_store = SQLiteWorkspaceLiveStore(db_path, clock=clock)
        self.journal_store = SQLiteWorkspaceMutationJournalStore(db_path, clock=clock)

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
        with self._workspace_lock(workspace):
            recovery = self._recover_locked(workspace)
            if recovery.conflicted:
                raise WorkspaceConflict("Workspace has an unresolved durable mutation conflict")

            head = self.live_store.ensure_head(workspace)
            before_digest, before_manifest = _capture_manifest(workspace)
            if before_digest != head.live_digest:
                raise WorkspaceConflict(
                    "Workspace local content drifted outside the live revision authority"
                )

            prepared, change_set, unified_diff = self._prepare(
                workspace,
                patches,
            )
            change_set = self.store.save_change_set(change_set, diff_text=unified_diff)
            request_key = f"workspace-edit:{head.live_revision}:{change_set.change_set_id}"
            writer_id = f"workspace-editor:{os.getpid()}:{uuid.uuid4().hex}"
            lease = self.live_store.claim_writer(
                workspace.workspace_id,
                owner=owner,
                writer_id=writer_id,
                lease_seconds=self.lease_seconds,
            )
            head = self.live_store.get_head(workspace.workspace_id, owner=owner)
            backup_dir = self._backup_path(workspace, request_key)
            journal: WorkspaceMutationJournal | None = None
            try:
                expected_manifest = _expected_manifest(before_manifest, prepared)
                expected_after_digest = _manifest_digest(expected_manifest)
                _write_backup(backup_dir, prepared)
                journal = self.journal_store.prepare(
                    head=head,
                    lease=lease,
                    request_key=request_key,
                    files=tuple(
                        WorkspaceMutationFile(
                            path=item.path,
                            operation=item.operation,  # type: ignore[arg-type]
                            before_sha256=item.change.before_sha256,
                            after_sha256=item.change.after_sha256,
                        )
                        for item in prepared
                    ),
                    backup_ref=str(backup_dir),
                    change_set_id=change_set.change_set_id,
                )
                self._crash("after_journal_prepared")
                for item in prepared:
                    lease = self.live_store.renew_writer(
                        lease,
                        lease_seconds=self.lease_seconds,
                    )
                    _apply_prepared(item, Path(workspace.local_root).resolve(strict=True))
                    self._crash(f"after_file:{item.path}")

                observed_after, _ = _capture_manifest(workspace)
                if observed_after != expected_after_digest:
                    raise WorkspaceLiveConflict(
                        "Workspace changed outside the controlled mutation while files were applied"
                    )
                journal = self.journal_store.mark_files_applied(
                    journal.mutation_id,
                    owner=owner,
                    lease=lease,
                    to_digest=observed_after,
                )
                self._crash("after_files_applied")
                journal = self.journal_store.commit(
                    journal.mutation_id,
                    owner=owner,
                    lease=lease,
                )
                self._crash("after_commit")
                _remove_backup(backup_dir)
                return change_set
            except Exception:
                # BaseException is deliberately not caught.  A process death,
                # cancellation primitive, or injected hard-crash leaves the
                # durable journal + backup for the next recovery pass.
                if journal is not None:
                    self._rollback_after_exception(workspace, journal)
                raise
            finally:
                try:
                    self.live_store.release_writer(lease)
                except (WorkspaceLiveConflict, KeyError):
                    pass

    def recover_workspace(self, workspace_id: str, owner: str) -> WorkspaceRecoveryReport:
        workspace = self.store.get_workspace(workspace_id, owner=owner)
        with self._workspace_lock(workspace):
            return self._recover_locked(workspace)

    def _recover_locked(self, workspace: AgentWorkspaceRecord) -> WorkspaceRecoveryReport:
        head = self.live_store.ensure_head(workspace)
        committed: list[str] = []
        rolled_back: list[str] = []
        conflicted: list[str] = []
        for journal in self.journal_store.list_open(
            workspace.workspace_id,
            owner=workspace.owner,
        ):
            observed, _ = _capture_manifest(workspace)
            if journal.state is WorkspaceMutationState.PREPARED:
                if observed == journal.from_digest:
                    self.journal_store.mark_rolled_back(
                        journal.mutation_id,
                        owner=workspace.owner,
                        observed_live_digest=observed,
                    )
                    _remove_backup(Path(journal.backup_ref))
                    rolled_back.append(journal.mutation_id)
                    continue
                if not _touched_paths_are_known(workspace, journal):
                    self.journal_store.mark_conflicted(
                        journal.mutation_id,
                        owner=workspace.owner,
                        error_code="WORKSPACE_RECOVERY_THIRD_STATE",
                    )
                    conflicted.append(journal.mutation_id)
                    continue
                _restore_backup(workspace, Path(journal.backup_ref))
                restored, _ = _capture_manifest(workspace)
                if restored != journal.from_digest:
                    self.journal_store.mark_conflicted(
                        journal.mutation_id,
                        owner=workspace.owner,
                        error_code="WORKSPACE_RECOVERY_DIGEST_MISMATCH",
                    )
                    conflicted.append(journal.mutation_id)
                    continue
                self.journal_store.mark_rolled_back(
                    journal.mutation_id,
                    owner=workspace.owner,
                    observed_live_digest=restored,
                )
                _remove_backup(Path(journal.backup_ref))
                rolled_back.append(journal.mutation_id)
                continue

            if journal.state is WorkspaceMutationState.FILES_APPLIED:
                if journal.to_digest is not None and observed == journal.to_digest:
                    current = self.live_store.get_head(workspace.workspace_id, owner=workspace.owner)
                    if (
                        current.live_revision == journal.from_revision
                        and current.live_digest == journal.from_digest
                    ):
                        try:
                            lease = self.live_store.claim_writer(
                                workspace.workspace_id,
                                owner=workspace.owner,
                                writer_id=f"workspace-recovery:{os.getpid()}:{uuid.uuid4().hex}",
                                lease_seconds=self.lease_seconds,
                            )
                        except WorkspaceLiveConflict:
                            raise WorkspaceConflict(
                                "Workspace recovery is waiting for the prior writer lease to expire"
                            ) from None
                        try:
                            _rebind_open_journal(self.journal_store, journal, lease)
                            self.journal_store.commit(
                                journal.mutation_id,
                                owner=workspace.owner,
                                lease=lease,
                            )
                        finally:
                            try:
                                self.live_store.release_writer(lease)
                            except WorkspaceLiveConflict:
                                pass
                        _remove_backup(Path(journal.backup_ref))
                        committed.append(journal.mutation_id)
                        head = self.live_store.get_head(
                            workspace.workspace_id,
                            owner=workspace.owner,
                        )
                        continue
                self.journal_store.mark_conflicted(
                    journal.mutation_id,
                    owner=workspace.owner,
                    error_code="WORKSPACE_FILES_APPLIED_UNPROVEN",
                )
                conflicted.append(journal.mutation_id)
        del head
        return WorkspaceRecoveryReport(
            workspace_id=workspace.workspace_id,
            committed=tuple(committed),
            rolled_back=tuple(rolled_back),
            conflicted=tuple(conflicted),
        )

    def _rollback_after_exception(
        self,
        workspace: AgentWorkspaceRecord,
        journal: WorkspaceMutationJournal,
    ) -> None:
        current = self.journal_store.get(journal.mutation_id, owner=workspace.owner)
        if current.state is not WorkspaceMutationState.PREPARED:
            # Once FILES_APPLIED is durable, do not guess a rollback.  Recovery
            # may safely commit it if the complete post-write digest is proven.
            return
        if not _touched_paths_are_known(workspace, current):
            self.journal_store.mark_conflicted(
                current.mutation_id,
                owner=workspace.owner,
                error_code="WORKSPACE_ROLLBACK_THIRD_STATE",
            )
            return
        _restore_backup(workspace, Path(current.backup_ref))
        restored, _ = _capture_manifest(workspace)
        if restored == current.from_digest:
            self.journal_store.mark_rolled_back(
                current.mutation_id,
                owner=workspace.owner,
                observed_live_digest=restored,
            )
            _remove_backup(Path(current.backup_ref))
        else:
            self.journal_store.mark_conflicted(
                current.mutation_id,
                owner=workspace.owner,
                error_code="WORKSPACE_ROLLBACK_DIGEST_MISMATCH",
            )

    def _prepare(
        self,
        workspace: AgentWorkspaceRecord,
        patches: tuple[tuple[str, str | None, WorkspacePatch], ...],
    ) -> tuple[tuple[_PreparedPatch, ...], WorkspaceChangeSet, str]:
        root = Path(workspace.local_root).resolve(strict=True)
        prepared: list[_PreparedPatch] = []
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
                    raise WorkspacePolicyError("Workspace patch target is not a contained file")
                before = resolved.read_bytes()
                mode = stat.S_IMODE(resolved.stat().st_mode)
            else:
                before = b""
                mode = 0o600
            before_digest = hashlib.sha256(before).hexdigest() if existing else None
            if patch.operation == "create":
                if existing or expected_source_digest is not None:
                    raise WorkspaceConflict("create patch no longer matches an absent source")
            else:
                if not existing:
                    raise WorkspaceConflict("patch source file no longer exists")
                if expected_source_digest != before_digest:
                    raise WorkspaceConflict("patch source digest no longer matches")
            if not _editable_path(relative):
                raise WorkspacePolicyError("Workspace patch target is not an editable file type")
            after = b"" if patch.operation == "delete" else (patch.content or "").encode()
            if len(after) > self.max_file_bytes:
                raise WorkspacePolicyError("Workspace patch exceeds the file size limit")
            try:
                before_text = before.decode("utf-8")
                after_text = after.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise WorkspacePolicyError("Workspace patches require UTF-8 text files") from exc
            unified = "".join(
                difflib.unified_diff(
                    before_text.splitlines(keepends=True),
                    after_text.splitlines(keepends=True),
                    fromfile=f"a/{relative}",
                    tofile=f"b/{relative}",
                )
            )
            total_diff_bytes += len(unified.encode())
            if total_diff_bytes > self.max_diff_bytes:
                raise WorkspacePolicyError("Workspace diff exceeds the output limit")
            after_digest = None if patch.operation == "delete" else hashlib.sha256(after).hexdigest()
            change = WorkspaceFileChange(
                path=relative,
                operation=patch.operation,
                before_sha256=before_digest,
                after_sha256=after_digest,
                diff_sha256=hashlib.sha256(unified.encode()).hexdigest(),
                size_bytes=len(after),
            )
            prepared.append(
                _PreparedPatch(
                    path=relative,
                    target=target,
                    operation=patch.operation,
                    before=before,
                    after=after,
                    existed=existing,
                    mode=mode,
                    change=change,
                    unified_diff=unified,
                )
            )
        files = tuple(item.change for item in prepared)
        digest = _change_set_digest(workspace, files)
        now = _timestamp()
        change_set = WorkspaceChangeSet(
            change_set_id=f"changeset-{digest[:24]}",
            project_id=workspace.project_id,
            workspace_id=workspace.workspace_id,
            owner=workspace.owner,
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
        return tuple(prepared), change_set, "".join(item.unified_diff for item in prepared)

    def _backup_path(self, workspace: AgentWorkspaceRecord, request_key: str) -> Path:
        digest = hashlib.sha256(request_key.encode()).hexdigest()
        return self.backup_root / workspace.owner / workspace.workspace_id / digest

    @contextmanager
    def _workspace_lock(self, workspace: AgentWorkspaceRecord) -> Iterator[None]:
        digest = hashlib.sha256(
            f"{workspace.owner}\0{workspace.workspace_id}".encode()
        ).hexdigest()
        path = self.lock_root / f"{digest}.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _crash(self, stage: str) -> None:
        if self.crash_hook is not None:
            self.crash_hook(stage)


def _capture_manifest(
    workspace: AgentWorkspaceRecord,
) -> tuple[str, dict[str, _ManifestEntry]]:
    root = Path(workspace.local_root)
    root_info = root.lstat()
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        raise WorkspaceLiveConflict("Workspace local root is not a real directory")
    root = root.resolve(strict=True)
    entries: dict[str, _ManifestEntry] = {}
    stack = [root]
    while stack:
        directory = stack.pop()
        for child in sorted(os.scandir(directory), key=lambda item: item.name, reverse=True):
            info = child.stat(follow_symlinks=False)
            path = Path(child.path)
            relative = path.relative_to(root).as_posix()
            if stat.S_ISLNK(info.st_mode):
                raise WorkspaceLiveConflict("Workspace live manifest contains a symlink")
            mode = stat.S_IMODE(info.st_mode)
            if stat.S_ISDIR(info.st_mode):
                entries[relative] = _ManifestEntry(relative, "directory", mode)
                stack.append(path)
            elif stat.S_ISREG(info.st_mode):
                entries[relative] = _ManifestEntry(
                    relative,
                    "file",
                    mode,
                    info.st_size,
                    _file_sha256(path),
                )
            else:
                raise WorkspaceLiveConflict("Workspace live manifest contains unsupported file type")
            if len(entries) > 10_000:
                raise WorkspaceLiveConflict("Workspace live manifest exceeds entry limit")
    return _manifest_digest(entries), entries


def _manifest_digest(entries: dict[str, _ManifestEntry]) -> str:
    payload_entries: list[dict[str, object]] = []
    for path in sorted(entries):
        item = entries[path]
        value: dict[str, object] = {"path": item.path, "kind": item.kind, "mode": item.mode}
        if item.kind == "file":
            value["size_bytes"] = item.size_bytes
            value["sha256"] = item.sha256
        payload_entries.append(value)
    encoded = json.dumps(
        {"schema_version": "pilot107.workspace-live-manifest/v1", "entries": payload_entries},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _expected_manifest(
    before: dict[str, _ManifestEntry],
    patches: tuple[_PreparedPatch, ...],
) -> dict[str, _ManifestEntry]:
    result = dict(before)
    for item in patches:
        if item.operation == "delete":
            result.pop(item.path, None)
            continue
        pure = PurePosixPath(item.path)
        parts = pure.parts[:-1]
        parent = PurePosixPath()
        for part in parts:
            parent /= part
            relative = parent.as_posix()
            if relative not in result:
                result[relative] = _ManifestEntry(relative, "directory", 0o700)
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
        _fsync_directory(current.parent)
        return
    _ensure_parent_directories(root, item.target.parent)
    if item.existed:
        current = item.target.resolve(strict=True)
        if _file_sha256(current) != item.change.before_sha256:
            raise WorkspaceLiveConflict("Workspace target changed before replace")
    elif item.target.exists():
        raise WorkspaceLiveConflict("Workspace create target appeared concurrently")
    _atomic_write_mode(item.target, item.after, item.mode if item.existed else 0o600)


def _write_backup(path: Path, prepared: tuple[_PreparedPatch, ...]) -> None:
    if path.exists():
        manifest_path = path / "manifest.json"
        if manifest_path.is_file():
            return
        raise WorkspaceLiveConflict("Workspace backup path exists without a durable manifest")
    path.mkdir(parents=True, mode=0o700)
    manifest: dict[str, Any] = {"schema_version": "pilot107.workspace-backup/v1", "files": []}
    for index, item in enumerate(prepared):
        blob_name: str | None = None
        if item.existed:
            blob_name = f"{index:04d}.bin"
            _durable_write(path / blob_name, item.before, 0o600)
        manifest["files"].append(
            {
                "path": item.path,
                "existed": item.existed,
                "mode": item.mode,
                "before_sha256": item.change.before_sha256,
                "after_sha256": item.change.after_sha256,
                "blob": blob_name,
            }
        )
    _durable_write(
        path / "manifest.json",
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode(),
        0o600,
    )
    _fsync_directory(path)


def _restore_backup(workspace: AgentWorkspaceRecord, backup: Path) -> None:
    manifest = _read_backup_manifest(backup)
    root = Path(workspace.local_root).resolve(strict=True)
    created_parents: list[Path] = []
    for item in manifest["files"]:
        relative = str(item["path"])
        target = root.joinpath(*PurePosixPath(relative).parts)
        _validate_patch_target(root, target)
        if bool(item["existed"]):
            blob = backup / str(item["blob"])
            content = blob.read_bytes()
            expected = item.get("before_sha256")
            if not isinstance(expected, str) or hashlib.sha256(content).hexdigest() != expected:
                raise WorkspaceLiveConflict("Workspace backup content digest is invalid")
            created_parents.extend(_ensure_parent_directories(root, target.parent))
            _atomic_write_mode(target, content, int(item["mode"]))
        else:
            if target.exists():
                info = target.lstat()
                if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                    raise WorkspaceLiveConflict("Workspace rollback target is not a regular file")
                target.unlink()
                _fsync_directory(target.parent)
    # Parent directories created by the mutation are not encoded separately;
    # remove empty ancestors that contain no imported or restored file.
    for item in reversed(manifest["files"]):
        target = root.joinpath(*PurePosixPath(str(item["path"])).parts).parent
        while target != root and target.is_dir():
            try:
                target.rmdir()
            except OSError:
                break
            target = target.parent
    del created_parents


def _touched_paths_are_known(
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
                # create-before or delete-after are both known absent states.
                continue
            return False
        try:
            if not target.is_file():
                return False
            digest = _file_sha256(target)
        except OSError:
            return False
        known = {value for value in (item.before_sha256, item.after_sha256) if value is not None}
        if digest not in known:
            return False
    return True


def _rebind_open_journal(
    store: SQLiteWorkspaceMutationJournalStore,
    journal: WorkspaceMutationJournal,
    lease: WorkspaceWriterLease,
) -> WorkspaceMutationJournal:
    if lease.workspace_id != journal.workspace_id or lease.owner != journal.owner:
        raise WorkspaceLiveConflict("Workspace recovery lease does not own journal")
    now = store._now()  # same canonical timestamp domain as the journal store
    with store.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        live = connection.execute(
            "SELECT * FROM agent_workspace_live_heads WHERE workspace_id = ? AND owner = ?",
            (journal.workspace_id, journal.owner),
        ).fetchone()
        if live is None:
            raise KeyError(journal.workspace_id)
        if (
            live["writer_id"] != lease.writer_id
            or int(live["fencing_token"]) != lease.fencing_token
            or str(live["writer_lease_expires_at"]) <= now
            or int(live["live_revision"]) != journal.from_revision
            or str(live["live_digest"]) != journal.from_digest
        ):
            raise WorkspaceLiveConflict("Workspace recovery lease is stale or head advanced")
        cursor = connection.execute(
            """
            UPDATE agent_workspace_mutation_journal
            SET writer_id = ?, fencing_token = ?, updated_at = ?
            WHERE mutation_id = ? AND owner = ? AND state IN ('prepared', 'files_applied')
              AND from_revision = ? AND from_digest = ?
            """,
            (
                lease.writer_id,
                lease.fencing_token,
                now,
                journal.mutation_id,
                journal.owner,
                journal.from_revision,
                journal.from_digest,
            ),
        )
        row = connection.execute(
            "SELECT * FROM agent_workspace_mutation_journal WHERE mutation_id = ? AND owner = ?",
            (journal.mutation_id, journal.owner),
        ).fetchone()
    if cursor.rowcount != 1 or row is None:
        raise WorkspaceLiveConflict("Workspace recovery journal could not be rebound")
    return store.get(journal.mutation_id, owner=journal.owner)


def _read_backup_manifest(path: Path) -> dict[str, Any]:
    try:
        value = json.loads((path / "manifest.json").read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkspaceLiveConflict("Workspace durable backup cannot be read") from exc
    if not isinstance(value, dict) or value.get("schema_version") != "pilot107.workspace-backup/v1":
        raise WorkspaceLiveConflict("Workspace durable backup manifest is invalid")
    files = value.get("files")
    if not isinstance(files, list) or not all(isinstance(item, dict) for item in files):
        raise WorkspaceLiveConflict("Workspace durable backup file list is invalid")
    return value


def _atomic_write_mode(destination: Path, content: bytes, mode: int) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            delete=False,
        ) as handle:
            temporary = handle.name
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, destination)
        temporary = None
        _fsync_directory(destination.parent)
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)


def _durable_write(path: Path, content: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("xb") as handle:
        os.chmod(handle.fileno(), mode)
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    _fsync_directory(path.parent)


def _ensure_parent_directories(root: Path, parent: Path) -> list[Path]:
    if parent == root:
        return []
    relative = parent.relative_to(root)
    current = root
    created: list[Path] = []
    for part in relative.parts:
        current = current / part
        if current.exists():
            info = current.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise WorkspaceLiveConflict("Workspace parent path is not a real directory")
            continue
        current.mkdir(mode=0o700)
        created.append(current)
        _fsync_directory(current.parent)
    return created


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _remove_backup(path: Path) -> None:
    if not path.exists():
        return
    for child in sorted(path.iterdir(), reverse=True):
        if child.is_dir():
            _remove_backup(child)
        else:
            child.unlink(missing_ok=True)
    path.rmdir()
    parent = path.parent
    for _ in range(2):
        try:
            parent.rmdir()
        except OSError:
            break
        parent = parent.parent
