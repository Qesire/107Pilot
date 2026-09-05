"""Atomic AC4 orchestration for SQLite Workspace mutations.

``DurableWorkspaceEditor`` owns filesystem validation, recovery, live-head
fencing, backup semantics, and manifest verification.  This subclass closes the
remaining database prepare window: the DRAFT ChangeSet and PREPARED mutation
journal are committed in one SQLite transaction before any Workspace file is
changed.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from pathlib import Path

from pilot107.agent import durable_workspace as _dw
from pilot107.agent.durable_workspace import DurableWorkspaceEditor
from pilot107.agent.workspace import (
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
from pilot107.agent.workspace_live import WorkspaceLiveConflict, WorkspaceLiveHead, WorkspaceWriterLease


class AtomicDurableWorkspaceEditor(DurableWorkspaceEditor):
    """Durable editor with an atomic ChangeSet/PREPARED-journal boundary."""

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
            before_digest, before_manifest = _dw._capture_manifest(workspace)
            if before_digest != head.live_digest:
                raise WorkspaceConflict(
                    "Workspace local content drifted outside the live revision authority"
                )
            prepared, change_set, unified_diff = self._prepare(workspace, patches)
            lease = self.live_store.claim_writer(
                workspace.workspace_id,
                owner=owner,
                writer_id=f"workspace-editor:{os.getpid()}:{uuid.uuid4().hex}",
                lease_seconds=self.lease_seconds,
            )
            head = self.live_store.get_head(workspace.workspace_id, owner=owner)
            # The journal request is a mutation-attempt identity, not a content
            # identity. Tool-level idempotency remains owned by AgentOperation.
            request_key = (
                f"workspace-edit:{head.live_revision}:{change_set.change_set_id}:"
                f"{uuid.uuid4().hex}"
            )
            backup = self._backup_path(workspace, request_key)
            journal: WorkspaceMutationJournal | None = None
            try:
                expected_after = _dw._manifest_digest(
                    _dw._expected_manifest(before_manifest, prepared)
                )
                _write_backup_atomically(
                    backup,
                    Path(workspace.local_root).resolve(strict=True),
                    prepared,
                )
                journal = _atomic_prepare(
                    self,
                    head=head,
                    lease=lease,
                    request_key=request_key,
                    change_set=change_set,
                    diff_text=unified_diff,
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
                journal = self.journal_store.mark_files_applied(
                    journal.mutation_id,
                    owner=owner,
                    lease=lease,
                    to_digest=observed_after,
                )
                self._crash("after_files_applied")
                self.journal_store.commit(
                    journal.mutation_id,
                    owner=owner,
                    lease=lease,
                )
                self._crash("after_commit")
                _dw._remove_tree(backup)
                return change_set
            except Exception:
                if journal is not None:
                    self._rollback_exception(workspace, journal)
                raise
            finally:
                try:
                    self.live_store.release_writer(lease)
                except (WorkspaceLiveConflict, KeyError):
                    pass


def _atomic_prepare(
    editor: AtomicDurableWorkspaceEditor,
    *,
    head: WorkspaceLiveHead,
    lease: WorkspaceWriterLease,
    request_key: str,
    change_set: WorkspaceChangeSet,
    diff_text: str,
    files: tuple[WorkspaceMutationFile, ...],
    backup_ref: str,
) -> WorkspaceMutationJournal:
    if (
        change_set.workspace_id != head.workspace_id
        or change_set.project_id != head.project_id
        or change_set.owner != head.owner
        or lease.workspace_id != head.workspace_id
        or lease.owner != head.owner
        or lease.writer_id != head.writer_id
        or lease.fencing_token != head.fencing_token
    ):
        raise WorkspaceLiveConflict("Workspace atomic prepare binding is invalid")
    if change_set.state.value != "draft" or change_set.version != 1:
        raise WorkspaceLiveConflict("Workspace atomic prepare requires a fresh DRAFT ChangeSet")
    if not isinstance(diff_text, str) or len(diff_text.encode()) > editor.max_diff_bytes:
        raise WorkspacePolicyError("Workspace diff exceeds the output limit")

    normalized_files = tuple(sorted(files, key=lambda item: item.path))
    if not normalized_files or len(normalized_files) > 256:
        raise WorkspacePolicyError("Workspace mutation file plan is invalid")
    if len({item.path for item in normalized_files}) != len(normalized_files):
        raise WorkspacePolicyError("Workspace mutation file plan has duplicate paths")

    now = editor.journal_store._now()  # noqa: SLF001 - shared SQLite transaction clock
    mutation_id = "workspace-mutation-" + hashlib.sha256(
        f"{head.workspace_id}\0{request_key}".encode()
    ).hexdigest()
    file_payload = [
        {
            "path": item.path,
            "operation": item.operation,
            "before_sha256": item.before_sha256,
            "after_sha256": item.after_sha256,
        }
        for item in normalized_files
    ]
    files_json = _canonical_json(file_payload)
    intent_payload = {
        "workspace_id": head.workspace_id,
        "project_id": head.project_id,
        "owner": head.owner,
        "request_key": request_key,
        "change_set_id": change_set.change_set_id,
        "from_revision": head.live_revision,
        "from_digest": head.live_digest,
        "files": file_payload,
    }
    intent_digest = hashlib.sha256(_canonical_json(intent_payload).encode()).hexdigest()
    payload_json = _canonical_json(change_set_payload(change_set))
    journal = WorkspaceMutationJournal(
        mutation_id=mutation_id,
        workspace_id=head.workspace_id,
        project_id=head.project_id,
        owner=head.owner,
        request_key=request_key,
        intent_digest=intent_digest,
        change_set_id=change_set.change_set_id,
        from_revision=head.live_revision,
        from_digest=head.live_digest,
        to_revision=None,
        to_digest=None,
        writer_id=lease.writer_id,
        fencing_token=lease.fencing_token,
        state=WorkspaceMutationState.PREPARED,
        files=normalized_files,
        backup_ref=backup_ref,
        error_code=None,
        created_at=now,
        updated_at=now,
    )

    with editor.journal_store.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        live = connection.execute(
            """
            SELECT writer_id, writer_lease_expires_at, fencing_token,
                   live_revision, live_digest
            FROM agent_workspace_live_heads
            WHERE workspace_id = ? AND owner = ?
            """,
            (head.workspace_id, head.owner),
        ).fetchone()
        if live is None:
            raise KeyError(head.workspace_id)
        if (
            live["writer_id"] != lease.writer_id
            or int(live["fencing_token"]) != lease.fencing_token
            or str(live["writer_lease_expires_at"]) <= now
            or int(live["live_revision"]) != head.live_revision
            or str(live["live_digest"]) != head.live_digest
        ):
            raise WorkspaceLiveConflict("Workspace live head changed before atomic prepare")

        existing_journal = connection.execute(
            """
            SELECT intent_digest FROM agent_workspace_mutation_journal
            WHERE workspace_id = ? AND request_key = ?
            """,
            (head.workspace_id, request_key),
        ).fetchone()
        if existing_journal is not None:
            raise WorkspaceLiveConflict("Workspace mutation attempt identity already exists")

        existing_change = connection.execute(
            """
            SELECT project_id, workspace_id, digest, state, version,
                   payload_json, diff_text
            FROM agent_workspace_changesets
            WHERE change_set_id = ? AND owner = ?
            """,
            (change_set.change_set_id, change_set.owner),
        ).fetchone()
        if existing_change is None:
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
        else:
            if (
                str(existing_change["project_id"]) != change_set.project_id
                or str(existing_change["workspace_id"]) != change_set.workspace_id
                or str(existing_change["digest"]) != change_set.digest
                or str(existing_change["state"]) != "draft"
                or int(existing_change["version"]) != 1
                or str(existing_change["payload_json"]) != payload_json
                or str(existing_change["diff_text"]) != diff_text
            ):
                raise WorkspaceConflict(
                    "ChangeSet content identity is already bound to a different lifecycle state"
                )
            authoritative = connection.execute(
                """
                SELECT 1 FROM agent_workspace_mutation_journal
                WHERE change_set_id = ? AND owner = ?
                  AND state IN ('files_applied', 'committed')
                LIMIT 1
                """,
                (change_set.change_set_id, change_set.owner),
            ).fetchone()
            if authoritative is not None:
                raise WorkspaceConflict(
                    "ChangeSet content identity already has an authoritative mutation"
                )

        connection.execute(
            """
            INSERT INTO agent_workspace_mutation_journal (
                mutation_id, workspace_id, project_id, owner, request_key,
                intent_digest, change_set_id, from_revision, from_digest,
                to_revision, to_digest, writer_id, fencing_token, state,
                files_json, backup_ref, error_code, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, 'prepared', ?, ?, NULL, ?, ?)
            """,
            (
                journal.mutation_id,
                journal.workspace_id,
                journal.project_id,
                journal.owner,
                journal.request_key,
                journal.intent_digest,
                journal.change_set_id,
                journal.from_revision,
                journal.from_digest,
                journal.writer_id,
                journal.fencing_token,
                files_json,
                journal.backup_ref,
                journal.created_at,
                journal.updated_at,
            ),
        )
    return journal


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
        raise ValueError("Workspace atomic prepare payload is not finite JSON") from exc
