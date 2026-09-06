"""PostgreSQL-authoritative AC4 Workspace mutation editor.

``Workspace`` here is the Agent-owned writable workspace. Filesystem preparation,
manifest verification, backup/rollback and OS locking are backend-neutral local
mechanics. PostgreSQL is the only durable authority for live-head state, writer
fencing, mutation journals and ChangeSet publication.
"""

from __future__ import annotations

import difflib
import fcntl
import hashlib
import os
import stat
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import Any

from pilot107.agent import durable_workspace as _dw
from pilot107.agent.durable_workspace import WorkspaceRecoveryReport, _PreparedPatch
from pilot107.agent.operation_context import current_agent_operation_key
from pilot107.agent.postgres_workspace_durability import (
    PostgresWorkspaceLiveStore,
    PostgresWorkspaceMutationJournalStore,
    _live_authority_matches,
    _row_to_journal,
)
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
    change_set_payload,
)
from pilot107.agent.workspace_journal import (
    WorkspaceMutationFile,
    WorkspaceMutationJournal,
    WorkspaceMutationState,
)
from pilot107.agent.workspace_live import (
    WorkspaceLiveConflict,
    WorkspaceLiveHead,
    WorkspaceWriterLease,
)


class PostgresAtomicDurableWorkspaceEditor(WorkspaceEditor):
    """AC4 editor with PostgreSQL as its sole durable state authority."""

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
        dsn = getattr(store, "dsn", None)
        if not isinstance(dsn, str) or not dsn:
            raise TypeError(
                "PostgresAtomicDurableWorkspaceEditor requires PostgreSQL ProjectStore"
            )
        if isinstance(lease_seconds, bool) or not 1 <= lease_seconds <= 86_400:
            raise ValueError("lease_seconds must be between 1 and 86400")
        self.state_root = state_root.resolve()
        self.lock_root = self.state_root / "locks"
        self.backup_root = self.state_root / "backups"
        self.lock_root.mkdir(parents=True, exist_ok=True)
        self.backup_root.mkdir(parents=True, exist_ok=True)
        self.lease_seconds = lease_seconds
        self.crash_hook = crash_hook
        self.live_store: PostgresWorkspaceLiveStore = PostgresWorkspaceLiveStore(
            dsn, clock=clock
        )
        self.journal_store: PostgresWorkspaceMutationJournalStore = (
            PostgresWorkspaceMutationJournalStore(dsn, clock=clock)
        )

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
            observed_head = self.live_store.ensure_head(workspace)
            before_digest, before_manifest = _dw._capture_manifest(workspace)
            if before_digest != observed_head.live_digest:
                raise WorkspaceConflict(
                    "Workspace local content drifted outside the live revision authority"
                )
            prepared, proposed_change_set, unified_diff = self._prepare(workspace, patches)
            lease = self.live_store.claim_writer(
                workspace.workspace_id,
                owner=owner,
                writer_id=f"workspace-editor:{os.getpid()}:{uuid.uuid4().hex}",
                lease_seconds=self.lease_seconds,
            )
            journal: WorkspaceMutationJournal | None = None
            backup: Path | None = None
            try:
                head = self.live_store.get_head(workspace.workspace_id, owner=owner)
                if (
                    head.live_revision != observed_head.live_revision
                    or head.live_digest != observed_head.live_digest
                ):
                    raise WorkspaceLiveConflict(
                        "Workspace live head advanced before writer fence was acquired"
                    )
                fenced_digest, fenced_manifest = _dw._capture_manifest(workspace)
                if fenced_digest != before_digest or fenced_manifest != before_manifest:
                    raise WorkspaceConflict(
                        "Workspace local content changed before writer fence was acquired"
                    )
                change_set = _bind_change_set_to_live_revision(proposed_change_set, head)
                operation_key = current_agent_operation_key()
                request_key = operation_key or (
                    f"workspace-edit:{head.live_revision}:{change_set.change_set_id}:"
                    f"{uuid.uuid4().hex}"
                )
                backup = self._backup_path(
                    workspace,
                    f"workspace-backup:{head.live_revision}:{change_set.change_set_id}",
                )
                expected_after = _dw._manifest_digest(
                    _dw._expected_manifest(before_manifest, prepared)
                )
                _write_backup_atomically(
                    backup,
                    Path(workspace.local_root).resolve(strict=True),
                    prepared,
                )
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
                    backup_ref=str(backup),
                    change_set_id=None,
                )
                self._crash("after_journal_prepared")
                root = Path(workspace.local_root).resolve(strict=True)
                for item in prepared:
                    lease = self.live_store.renew_writer(
                        lease, lease_seconds=self.lease_seconds
                    )
                    _dw._apply_prepared(item, root)
                    self._crash(f"after_file:{item.path}")
                observed_after, _ = _dw._capture_manifest(workspace)
                if observed_after != expected_after:
                    raise WorkspaceLiveConflict(
                        "Workspace changed outside the controlled mutation"
                    )
                self._crash("after_files_applied")
                lease = self.live_store.renew_writer(
                    lease, lease_seconds=self.lease_seconds
                )
                self._atomic_finalize(
                    journal=journal,
                    lease=lease,
                    change_set=change_set,
                    diff_text=unified_diff,
                    to_digest=observed_after,
                )
                self._crash("after_commit")
                with suppress(OSError):
                    _dw._remove_tree(backup)
                return change_set
            except Exception:
                if journal is not None:
                    self._rollback_exception(workspace, journal)
                elif backup is not None:
                    with suppress(OSError):
                        _dw._remove_tree(backup)
                raise
            finally:
                with suppress(WorkspaceLiveConflict, KeyError):
                    self.live_store.release_writer(lease)

    def recover_workspace(self, workspace_id: str, owner: str) -> WorkspaceRecoveryReport:
        workspace = self.store.get_workspace(workspace_id, owner=owner)
        with self._workspace_lock(workspace):
            return self._recover_locked(workspace)

    def _rollback_exception(
        self,
        workspace: AgentWorkspaceRecord,
        journal: WorkspaceMutationJournal,
    ) -> None:
        current = self.journal_store.get(journal.mutation_id, owner=workspace.owner)
        if current.state is not WorkspaceMutationState.PREPARED:
            return
        if not _dw._touched_paths_known(workspace, current):
            self._conflict(current, "WORKSPACE_ROLLBACK_THIRD_STATE")
            return
        _dw._restore_backup(workspace, Path(current.backup_ref))
        restored, _ = _dw._capture_manifest(workspace)
        if restored == current.from_digest:
            self.journal_store.mark_rolled_back(
                current.mutation_id,
                owner=workspace.owner,
                observed_live_digest=restored,
            )
            _dw._remove_tree(Path(current.backup_ref))
        else:
            self._conflict(current, "WORKSPACE_ROLLBACK_DIGEST_MISMATCH")

    def _conflict(self, journal: WorkspaceMutationJournal, code: str) -> None:
        self.journal_store.mark_conflicted(
            journal.mutation_id,
            owner=journal.owner,
            error_code=code,
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
                raise WorkspacePolicyError(
                    "Workspace patch batch contains duplicate paths"
                )
            seen.add(relative)
            if not isinstance(patch, WorkspacePatch):
                raise TypeError("patch must be WorkspacePatch")
            target = root.joinpath(*PurePosixPath(relative).parts)
            _validate_patch_target(root, target)
            exists = target.exists()
            if exists:
                resolved = target.resolve(strict=True)
                if not resolved.is_relative_to(root) or not resolved.is_file():
                    raise WorkspacePolicyError(
                        "Workspace patch target is not a contained file"
                    )
                before = resolved.read_bytes()
                mode = stat.S_IMODE(resolved.stat().st_mode)
            else:
                before = b""
                mode = 0o600
            before_digest = hashlib.sha256(before).hexdigest() if exists else None
            if patch.operation == "create":
                if exists or expected_source_digest is not None:
                    raise WorkspaceConflict(
                        "create patch no longer matches an absent source"
                    )
            else:
                if not exists:
                    raise WorkspaceConflict("patch source file no longer exists")
                if expected_source_digest != before_digest:
                    raise WorkspaceConflict("patch source digest no longer matches")
            if not _editable_path(relative):
                raise WorkspacePolicyError(
                    "Workspace patch target is not an editable file type"
                )
            after = b"" if patch.operation == "delete" else (patch.content or "").encode()
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
            total_diff_bytes += len(unified.encode())
            if total_diff_bytes > self.max_diff_bytes:
                raise WorkspacePolicyError("Workspace diff exceeds the output limit")
            after_digest = (
                None
                if patch.operation == "delete"
                else hashlib.sha256(after).hexdigest()
            )
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
                    existed=exists,
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
        return tuple(prepared), change_set, "".join(
            item.unified_diff for item in prepared
        )

    def _backup_path(self, workspace: AgentWorkspaceRecord, request_key: str) -> Path:
        digest = hashlib.sha256(request_key.encode()).hexdigest()
        return self.backup_root / workspace.owner / workspace.workspace_id / digest

    @contextmanager
    def _workspace_lock(self, workspace: AgentWorkspaceRecord) -> Iterator[None]:
        name = hashlib.sha256(
            f"{workspace.owner}\0{workspace.workspace_id}".encode()
        ).hexdigest()
        path = self.lock_root / f"{name}.lock"
        with path.open("a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _crash(self, stage: str) -> None:
        if self.crash_hook is not None:
            self.crash_hook(stage)

    def _atomic_finalize(
        self,
        *,
        journal: WorkspaceMutationJournal,
        lease: WorkspaceWriterLease,
        change_set: WorkspaceChangeSet,
        diff_text: str,
        to_digest: str,
    ) -> None:
        if (
            journal.state is not WorkspaceMutationState.PREPARED
            or journal.change_set_id is not None
        ):
            raise WorkspaceLiveConflict(
                "Workspace journal is not a fresh PREPARED mutation"
            )
        if (
            journal.workspace_id != change_set.workspace_id
            or journal.project_id != change_set.project_id
            or journal.owner != change_set.owner
            or lease.workspace_id != journal.workspace_id
            or lease.owner != journal.owner
            or lease.writer_id != journal.writer_id
            or lease.fencing_token != journal.fencing_token
        ):
            raise WorkspaceLiveConflict("Workspace atomic finalize binding is invalid")
        if change_set.state.value != "draft" or change_set.version != 1:
            raise WorkspaceLiveConflict(
                "Workspace atomic finalize requires a fresh DRAFT ChangeSet"
            )
        if not isinstance(diff_text, str) or len(diff_text.encode()) > self.max_diff_bytes:
            raise WorkspacePolicyError("Workspace diff exceeds the output limit")
        if len(to_digest) != 64 or any(
            character not in "0123456789abcdef" for character in to_digest
        ):
            raise WorkspaceLiveConflict("Workspace target digest is invalid")

        now = self.journal_store._now()  # noqa: SLF001 - one PostgreSQL tx clock
        payload = change_set_payload(change_set)
        with self.journal_store.connect() as connection, connection.transaction():
            journal_row = connection.execute(
                """
                SELECT * FROM agent_workspace_mutation_journal
                WHERE mutation_id = %s AND owner = %s
                FOR UPDATE
                """,
                (journal.mutation_id, journal.owner),
            ).fetchone()
            if journal_row is None:
                raise KeyError(journal.mutation_id)
            current = _row_to_journal(journal_row)
            if (
                current.state is not WorkspaceMutationState.PREPARED
                or current.change_set_id is not None
                or current.from_revision != journal.from_revision
                or current.from_digest != journal.from_digest
                or current.writer_id != lease.writer_id
                or current.fencing_token != lease.fencing_token
            ):
                raise WorkspaceLiveConflict(
                    "Workspace journal changed before atomic finalize"
                )

            live = connection.execute(
                """
                SELECT * FROM agent_workspace_live_heads
                WHERE workspace_id = %s AND owner = %s
                FOR UPDATE
                """,
                (journal.workspace_id, journal.owner),
            ).fetchone()
            if live is None:
                raise KeyError(journal.workspace_id)
            if not _live_authority_matches(
                live,
                lease=lease,
                expected_revision=journal.from_revision,
                expected_digest=journal.from_digest,
                now=now,
            ):
                raise WorkspaceLiveConflict(
                    "Workspace live head changed before atomic finalize"
                )

            existing = connection.execute(
                """
                SELECT 1 FROM agent_workspace_changesets
                WHERE change_set_id = %s AND owner = %s
                """,
                (change_set.change_set_id, change_set.owner),
            ).fetchone()
            if existing is not None:
                raise WorkspaceConflict(
                    "Revision-bound ChangeSet identity already exists"
                )

            connection.execute(
                """
                INSERT INTO agent_workspace_changesets (
                    change_set_id, project_id, workspace_id, owner, digest,
                    state, version, payload_json, diff_text, created_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, 'draft', 1, %s, %s, %s, %s)
                """,
                (
                    change_set.change_set_id,
                    change_set.project_id,
                    change_set.workspace_id,
                    change_set.owner,
                    change_set.digest,
                    self.journal_store._jsonb(payload),  # noqa: SLF001
                    diff_text,
                    change_set.created_at,
                    change_set.updated_at,
                ),
            )
            live_update = connection.execute(
                """
                UPDATE agent_workspace_live_heads
                SET live_revision = live_revision + 1, live_digest = %s, updated_at = %s
                WHERE workspace_id = %s AND owner = %s AND writer_id = %s
                  AND fencing_token = %s AND writer_lease_expires_at > %s
                  AND live_revision = %s AND live_digest = %s
                """,
                (
                    to_digest,
                    now,
                    journal.workspace_id,
                    journal.owner,
                    lease.writer_id,
                    lease.fencing_token,
                    now,
                    journal.from_revision,
                    journal.from_digest,
                ),
            )
            if live_update.rowcount != 1:
                raise WorkspaceLiveConflict(
                    "Workspace live CAS failed during atomic finalize"
                )
            journal_update = connection.execute(
                """
                UPDATE agent_workspace_mutation_journal
                SET state = 'committed', change_set_id = %s,
                    to_revision = from_revision + 1, to_digest = %s, updated_at = %s
                WHERE mutation_id = %s AND owner = %s AND state = 'prepared'
                  AND change_set_id IS NULL AND writer_id = %s AND fencing_token = %s
                """,
                (
                    change_set.change_set_id,
                    to_digest,
                    now,
                    journal.mutation_id,
                    journal.owner,
                    lease.writer_id,
                    lease.fencing_token,
                ),
            )
            if journal_update.rowcount != 1:
                raise WorkspaceLiveConflict(
                    "Workspace journal CAS failed during atomic finalize"
                )

    def _recover_locked(self, workspace: AgentWorkspaceRecord) -> WorkspaceRecoveryReport:
        self.live_store.ensure_head(workspace)
        committed: list[str] = []
        rolled_back: list[str] = []
        conflicted: list[str] = []
        for journal in self.journal_store.list_open(
            workspace.workspace_id, owner=workspace.owner
        ):
            observed, _ = _dw._capture_manifest(workspace)
            if journal.state is WorkspaceMutationState.PREPARED:
                if observed == journal.from_digest:
                    self.journal_store.mark_rolled_back(
                        journal.mutation_id,
                        owner=workspace.owner,
                        observed_live_digest=observed,
                    )
                    _dw._remove_tree(Path(journal.backup_ref))
                    rolled_back.append(journal.mutation_id)
                    continue
                if not _dw._touched_paths_known(workspace, journal):
                    self._conflict(journal, "WORKSPACE_RECOVERY_THIRD_STATE")
                    conflicted.append(journal.mutation_id)
                    continue
                _dw._restore_backup(workspace, Path(journal.backup_ref))
                restored, _ = _dw._capture_manifest(workspace)
                if restored != journal.from_digest:
                    self._conflict(
                        journal, "WORKSPACE_RECOVERY_DIGEST_MISMATCH"
                    )
                    conflicted.append(journal.mutation_id)
                    continue
                self.journal_store.mark_rolled_back(
                    journal.mutation_id,
                    owner=workspace.owner,
                    observed_live_digest=restored,
                )
                _dw._remove_tree(Path(journal.backup_ref))
                rolled_back.append(journal.mutation_id)
                continue
            if journal.state is WorkspaceMutationState.FILES_APPLIED:
                if journal.to_digest is not None and observed == journal.to_digest:
                    head = self.live_store.get_head(
                        workspace.workspace_id, owner=workspace.owner
                    )
                    if (
                        head.live_revision == journal.from_revision
                        and head.live_digest == journal.from_digest
                    ):
                        try:
                            lease = self.live_store.claim_writer(
                                workspace.workspace_id,
                                owner=workspace.owner,
                                writer_id=(
                                    f"workspace-recovery:{os.getpid()}:"
                                    f"{uuid.uuid4().hex}"
                                ),
                                lease_seconds=self.lease_seconds,
                            )
                        except WorkspaceLiveConflict:
                            raise WorkspaceConflict(
                                "Workspace recovery is waiting for the prior writer lease"
                            ) from None
                        try:
                            self._rebind_files_applied(journal, lease)
                            self.journal_store.commit(
                                journal.mutation_id,
                                owner=workspace.owner,
                                lease=lease,
                            )
                        finally:
                            with suppress(WorkspaceLiveConflict):
                                self.live_store.release_writer(lease)
                        _dw._remove_tree(Path(journal.backup_ref))
                        committed.append(journal.mutation_id)
                        continue
                self._conflict(journal, "WORKSPACE_FILES_APPLIED_UNPROVEN")
                conflicted.append(journal.mutation_id)
        self._reclaim_workspace_backups(workspace)
        return WorkspaceRecoveryReport(
            workspace_id=workspace.workspace_id,
            committed=tuple(committed),
            rolled_back=tuple(rolled_back),
            conflicted=tuple(conflicted),
        )

    def _rebind_files_applied(
        self,
        journal: WorkspaceMutationJournal,
        lease: WorkspaceWriterLease,
    ) -> None:
        if lease.workspace_id != journal.workspace_id or lease.owner != journal.owner:
            raise WorkspaceLiveConflict(
                "Workspace recovery lease does not own journal"
            )
        now = self.journal_store._now()  # noqa: SLF001
        with self.journal_store.connect() as connection, connection.transaction():
            live = connection.execute(
                """
                SELECT * FROM agent_workspace_live_heads
                WHERE workspace_id = %s AND owner = %s
                FOR UPDATE
                """,
                (journal.workspace_id, journal.owner),
            ).fetchone()
            if live is None:
                raise KeyError(journal.workspace_id)
            if not _live_authority_matches(
                live,
                lease=lease,
                expected_revision=journal.from_revision,
                expected_digest=journal.from_digest,
                now=now,
            ):
                raise WorkspaceLiveConflict(
                    "Workspace recovery lease is stale or head advanced"
                )
            updated = connection.execute(
                """
                UPDATE agent_workspace_mutation_journal
                SET writer_id = %s, fencing_token = %s, updated_at = %s
                WHERE mutation_id = %s AND owner = %s AND state = 'files_applied'
                  AND from_revision = %s AND from_digest = %s
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
            if updated.rowcount != 1:
                raise WorkspaceLiveConflict(
                    "Workspace recovery journal could not be rebound"
                )

    def _reclaim_workspace_backups(self, workspace: AgentWorkspaceRecord) -> None:
        root = self.backup_root / workspace.owner / workspace.workspace_id
        if not root.exists():
            return
        root = root.resolve(strict=True)
        retained: set[Path] = set()
        with self.journal_store.connect() as connection:
            rows = connection.execute(
                """
                SELECT state, backup_ref FROM agent_workspace_mutation_journal
                WHERE workspace_id = %s AND owner = %s
                """,
                (workspace.workspace_id, workspace.owner),
            ).fetchall()
        for row in rows:
            if str(row["state"]) not in {
                "prepared",
                "files_applied",
                "conflicted",
            }:
                continue
            candidate = Path(str(row["backup_ref"])).resolve(strict=False)
            if candidate == root or candidate.is_relative_to(root):
                retained.add(candidate)
        for child in tuple(root.iterdir()):
            resolved = child.resolve(strict=False)
            if resolved in retained:
                continue
            with suppress(OSError):
                if child.is_symlink() or not child.is_dir():
                    child.unlink(missing_ok=True)
                else:
                    _dw._remove_tree(child)
        with suppress(OSError):
            root.rmdir()


def _bind_change_set_to_live_revision(
    change_set: WorkspaceChangeSet,
    head: WorkspaceLiveHead,
) -> WorkspaceChangeSet:
    identity = hashlib.sha256(
        f"{change_set.digest}\0{head.live_revision}\0{head.live_digest}".encode()
    ).hexdigest()
    return replace(change_set, change_set_id=f"changeset-{identity[:24]}")


def _write_backup_atomically(
    destination: Path,
    workspace_root: Path,
    prepared: tuple[_PreparedPatch, ...],
) -> None:
    if destination.exists():
        if (destination / "manifest.json").is_file():
            return
        raise WorkspaceLiveConflict(
            "Workspace backup exists without durable manifest"
        )
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = destination.with_name(
        f".{destination.name}.tmp-{uuid.uuid4().hex}"
    )
    try:
        _dw._write_backup(temporary, workspace_root, prepared)
        os.replace(temporary, destination)
        _dw._fsync_dir(destination.parent)
    finally:
        if temporary.exists():
            _dw._remove_tree(temporary)


__all__ = ["PostgresAtomicDurableWorkspaceEditor"]
