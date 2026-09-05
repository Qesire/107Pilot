"""Retired SQLite atomic Workspace editor compatibility surface.

The competition/runtime AC4 implementation is PostgreSQL-only and lives in
``postgres_workspace_atomic``. This module retains only fail-closed legacy
construction plus backend-neutral helper functions used by historical tests or
migration code. It contains no SQLite connection, migration or mutation path.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from dataclasses import replace
from pathlib import Path

from pilot107.agent import durable_workspace as _dw
from pilot107.agent.durable_workspace import _PreparedPatch
from pilot107.agent.workspace import WorkspaceChangeSet
from pilot107.agent.workspace_live import WorkspaceLiveConflict, WorkspaceLiveHead

_SQLITE_RETIRED = "SQLite atomic durable Workspace editor has been retired"


class AtomicDurableWorkspaceEditor:
    """Rejected legacy editor name; PostgreSQL AC4 is the sole authority."""

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise RuntimeError(_SQLITE_RETIRED)


def _bind_change_set_to_live_revision(
    change_set: WorkspaceChangeSet,
    head: WorkspaceLiveHead,
) -> WorkspaceChangeSet:
    """Pure identity helper retained independently of any persistence backend."""

    identity = hashlib.sha256(
        f"{change_set.digest}\0{head.live_revision}\0{head.live_digest}".encode()
    ).hexdigest()
    return replace(change_set, change_set_id=f"changeset-{identity[:24]}")


def _write_backup_atomically(
    destination: Path,
    workspace_root: Path,
    prepared: tuple[_PreparedPatch, ...],
) -> None:
    """Publish a filesystem backup atomically; this function owns no DB state."""

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


__all__ = ["AtomicDurableWorkspaceEditor"]
