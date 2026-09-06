"""Atomic AC4 orchestration for SQLite Workspace mutations.

``DurableWorkspaceEditor`` owns filesystem validation, recovery, live-head
fencing, backup semantics, and manifest verification. This subclass closes the
remaining publication window: a PREPARED journal exists before Workspace files
change, while the ChangeSet, live-head advance, and COMMITTED journal receipt
become visible together in one SQLite transaction after filesystem verification.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from contextlib import suppress
from dataclasses import replace
from pathlib import Path

from pilot107.agent import durable_workspace as _dw
from pilot107.agent.durable_workspace import (
    DurableWorkspaceEditor,
    WorkspaceRecoveryReport,
)
from pilot107.agent.operation_context import current_agent_operation_key
from pilot107.agent.workspace import (
    AgentWorkspaceRecord,
    WorkspaceChangeSet,
    WorkspaceConflict,
    WorkspacePatch,
    WorkspacePolicyError,
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


class AtomicDurableWorkspaceEditor(DurableWorkspaceEditor):
    """Durable editor whose ChangeSet exists iff the live mutation committed."""

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
                # The filesystem is now fully applied and verified. The journal
                # intentionally remains PREPARED until the transaction below
                # publishes every authoritative database fact together.
                self._crash("after_files_applied")
                lease = self.live_store.renew_writer(
                    lease, lease_seconds=self.lease_seconds
                )
                _atomic_finalize(
                    self,
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
                try:
                    self.live_store.release_writer(lease)
                except (WorkspaceLiveConflict, KeyError):
                    pass

    def _recover_locked(self, workspace: AgentWorkspaceRecord) -> WorkspaceRecoveryReport:
        report = super()._recover_locked(workspace)
        _reclaim_workspace_backups(self, workspace)
        return report


def _bind_change_set_to_live_revision(
    change_set: WorkspaceChangeSet,
    head: WorkspaceLiveHead,
) -> WorkspaceChangeSet:
    identity = hashlib.sha256(
        f"{change_set.digest}\0{head.live_revision}\0{head.live_digest}".encode()
    ).hexdigest()
    return replace(change_set, change_set_id=f"changeset-{identity[:24]}")


def _atomic_finalize(
    editor: AtomicDurableWorkspaceEditor,
    *,
    journal: WorkspaceMutationJournal,
    lease: WorkspaceWriterLease,
    change_set: WorkspaceChangeSet,
    diff_text: str,
    to_digest: str,
) -> None:
    if journal.state is not WorkspaceMutationState.PREPARED or journal.change_set_id is not None:
        raise WorkspaceLiveConflict("Workspace journal is not a fresh PREPARED mutation")
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
        raise WorkspaceLiveConflict("Workspace atomic finalize requires a fresh DRAFT ChangeSet")
    if not isinstance(diff_text, str) or len(diff_text.encode()) > editor.max_diff_bytes:
        raise WorkspacePolicyError("Workspace diff exceeds the output limit")
    if len(to_digest) != 64 or any(
        character not in "0123456789abcdef" for character in to_digest
    ):
        raise WorkspaceLiveConflict("Workspace target digest is invalid")

    now = editor.journal_store._now()  # noqa: SLF001 - shared SQLite transaction clock
    payload_json = _canonical_json(change_set_payload(change_set))
    with editor.journal_store.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        current = connection.execute(
            """
            SELECT state, change_set_id, from_revision, from_digest,
                   writer_id, fencing_token
            FROM agent_workspace_mutation_journal
            WHERE mutation_id = ? AND owner = ?
            """,
            (journal.mutation_id, journal.owner),
        ).fetchone()
        if current is None:
            raise KeyError(journal.mutation_id)
        if (
            str(current["state"]) != "prepared"
            or current["change_set_id"] is not None
            or int(current["from_revision"]) != journal.from_revision
            or str(current["from_digest"]) != journal.from_digest
            or current["writer_id"] != lease.writer_id
            or int(current["fencing_token"]) != lease.fencing_token
        ):
            raise WorkspaceLiveConflict("Workspace journal changed before atomic finalize")

        live = connection.execute(
            """
            SELECT writer_id, writer_lease_expires_at, fencing_token,
                   live_revision, live_digest
            FROM agent_workspace_live_heads
            WHERE workspace_id = ? AND owner = ?
            """,
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
            raise WorkspaceLiveConflict("Workspace live head changed before atomic finalize")

        existing = connection.execute(
            """
            SELECT 1 FROM agent_workspace_changesets
            WHERE change_set_id = ? AND owner = ?
            """,
            (change_set.change_set_id, change_set.owner),
        ).fetchone()
        if existing is not None:
            raise WorkspaceConflict("Revision-bound ChangeSet identity already exists")

        connection.execute(
            """
            INSERT INTO agent_workspace_changesets (
                change_set_id, project_id, workspace_id, owner, digest,
                state, version, payload_json, diff_text, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'draft', 1, ?, ?, ?, ?)
            """,
            (
                change_set.change_set_id,
                change_set.project_id,
                change_set.workspace_id,
                change_set.owner,
                change_set.digest,
                payload_json,
                diff_text,
                change_set.created_at,
                change_set.updated_at,
            ),
        )
        live_update = connection.execute(
            """
            UPDATE agent_workspace_live_heads
            SET live_revision = live_revision + 1, live_digest = ?, updated_at = ?
            WHERE workspace_id = ? AND owner = ? AND writer_id = ?
              AND fencing_token = ? AND writer_lease_expires_at > ?
              AND live_revision = ? AND live_digest = ?
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
            raise WorkspaceLiveConflict("Workspace live CAS failed during atomic finalize")
        journal_update = connection.execute(
            """
            UPDATE agent_workspace_mutation_journal
            SET state = 'committed', change_set_id = ?,
                to_revision = from_revision + 1, to_digest = ?, updated_at = ?
            WHERE mutation_id = ? AND owner = ? AND state = 'prepared'
              AND change_set_id IS NULL AND writer_id = ? AND fencing_token = ?
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
            raise WorkspaceLiveConflict("Workspace journal CAS failed during atomic finalize")


def _reclaim_workspace_backups(
    editor: AtomicDurableWorkspaceEditor,
    workspace: AgentWorkspaceRecord,
) -> None:
    root = editor.backup_root / workspace.owner / workspace.workspace_id
    if not root.exists():
        return
    root = root.resolve(strict=True)
    retained: set[Path] = set()
    with editor.journal_store.connect() as connection:
        rows = connection.execute(
            """
            SELECT state, backup_ref FROM agent_workspace_mutation_journal
            WHERE workspace_id = ? AND owner = ?
            """,
            (workspace.workspace_id, workspace.owner),
        ).fetchall()
    for row in rows:
        if str(row["state"]) not in {"prepared", "files_applied", "conflicted"}:
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


def _write_backup_atomically(
    destination: Path,
    workspace_root: Path,
    prepared: tuple[object, ...],
) -> None:
    if destination.exists():
        if (destination / "manifest.json").is_file():
            return
        raise WorkspaceLiveConflict("Workspace backup exists without durable manifest")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = destination.with_name(f".{destination.name}.tmp-{uuid.uuid4().hex}")
    try:
        _dw._write_backup(temporary, workspace_root, prepared)  # type: ignore[arg-type]
        os.replace(temporary, destination)
        _dw._fsync_dir(destination.parent)
    finally:
        if temporary.exists():
            _dw._remove_tree(temporary)


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("Workspace atomic finalize payload is not finite JSON") from exc
