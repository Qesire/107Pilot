"""Conflict-safe publication of approved Agent Workspace ChangeSets."""

from __future__ import annotations

import base64
import hashlib
import posixpath
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Protocol

from pilot107.agent.project_store import ProjectStore
from pilot107.agent.workspace import WorkspaceChangeSet, WorkspaceChangeSetState
from pilot107.core.file_uploads import authorize_owner_path


class WorkspacePublicationState(StrEnum):
    PREPARED = "prepared"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    CONFLICTED = "conflicted"
    FAILED = "failed"


@dataclass(frozen=True)
class WorkspacePublicationFile:
    path: str
    operation: Literal["create", "modify", "delete"]
    expected_sha256: str | None
    desired_sha256: str | None
    staging_path: str | None
    state: Literal["pending", "staged", "committed"] = "pending"
    size_bytes: int = 0


@dataclass(frozen=True)
class WorkspacePublication:
    publication_id: str
    change_set_id: str
    project_id: str
    workspace_id: str
    owner: str
    target_root: str
    approved_digest: str
    approved_by: str
    state: WorkspacePublicationState
    version: int
    files: tuple[WorkspacePublicationFile, ...]
    error_code: str | None
    created_at: str
    updated_at: str


class WorkspacePublicationRelay(Protocol):
    def path_sha256(
        self, *, path: str, owner: str, timeout_seconds: float = 30.0
    ) -> str | None: ...

    def make_dir(self, *, path: str, owner: str, timeout_seconds: float = 30.0) -> None: ...

    def write_bytes_chunk(
        self,
        *,
        path: str,
        data_b64: str,
        offset: int,
        owner: str,
        timeout_seconds: float = 30.0,
    ) -> int: ...

    def compare_and_swap_file(
        self,
        *,
        staged_path: str,
        target_path: str,
        expected_sha256: str | None,
        desired_sha256: str,
        owner: str,
        timeout_seconds: float = 30.0,
    ) -> str: ...

    def compare_and_delete_file(
        self,
        *,
        target_path: str,
        expected_sha256: str,
        owner: str,
        timeout_seconds: float = 30.0,
    ) -> str: ...

    def remove_path(self, *, path: str, owner: str, timeout_seconds: float = 30.0) -> None: ...


class WorkspacePublisher:
    def __init__(
        self,
        *,
        store: ProjectStore,
        relay: WorkspacePublicationRelay,
        owner_roots: tuple[str, ...],
        clock: Callable[[], datetime] | None = None,
        chunk_bytes: int = 1024 * 1024,
    ) -> None:
        if not owner_roots:
            raise ValueError("owner_roots must not be empty")
        if chunk_bytes < 1:
            raise ValueError("chunk_bytes must be positive")
        self.store = store
        self.relay = relay
        # Publisher terminology uses ``owner`` while the shared path policy's
        # documented template token is ``user``; accept both at this boundary.
        self.owner_roots = tuple(root.replace("{owner}", "{user}") for root in owner_roots)
        self._clock = clock or (lambda: datetime.now(UTC))
        self.chunk_bytes = chunk_bytes

    def prepare(
        self, change_set_id: str, *, actor: str, target_root: str | None = None
    ) -> WorkspacePublication:
        change_set = self.store.get_change_set(change_set_id, owner=actor)
        self._validate_approval(change_set, actor=actor)
        workspace = self.store.get_workspace(change_set.workspace_id, owner=actor)
        if workspace.project_id != change_set.project_id:
            raise ValueError("ChangeSet project does not own its Workspace")
        if workspace.snapshot.digest != change_set.base_snapshot_digest:
            raise ValueError("ChangeSet base snapshot digest does not match Workspace")
        root = target_root or workspace.snapshot.source_ref
        if root == "/__pilot107_blank__" or root.startswith("/__pilot107_blank__/"):
            raise ValueError("blank Workspace publication requires target_root")
        root = authorize_owner_path(self.owner_roots, owner=actor, target_path=root)

        try:
            existing = self.store.get_workspace_publication(change_set_id, owner=actor)
        except KeyError:
            existing = None
        if existing is not None:
            if (
                existing.approved_digest != change_set.digest
                or existing.approved_by != actor
                or existing.target_root != root
            ):
                raise ValueError("publication approval or target changed")
            return existing

        publication_id = (
            "publication-"
            + hashlib.sha256(
                f"{actor}\0{change_set_id}\0{change_set.digest}\0{root}".encode()
            ).hexdigest()[:24]
        )
        staging_root = posixpath.join(root, ".107pilot", "publish", change_set_id)
        files = tuple(
            WorkspacePublicationFile(
                path=self._relative_path(item.path),
                operation=item.operation,
                expected_sha256=item.before_sha256,
                desired_sha256=item.after_sha256,
                staging_path=(
                    None
                    if item.operation == "delete"
                    else posixpath.join(staging_root, self._relative_path(item.path))
                ),
                size_bytes=item.size_bytes,
            )
            for item in change_set.files
        )
        now = self._now()
        return self.store.save_workspace_publication(
            WorkspacePublication(
                publication_id=publication_id,
                change_set_id=change_set.change_set_id,
                project_id=change_set.project_id,
                workspace_id=change_set.workspace_id,
                owner=actor,
                target_root=root,
                approved_digest=change_set.digest,
                approved_by=actor,
                state=WorkspacePublicationState.PREPARED,
                version=1,
                files=files,
                error_code=None,
                created_at=now,
                updated_at=now,
            )
        )

    def publish(self, change_set_id: str, *, actor: str) -> WorkspacePublication:
        publication = self.store.get_workspace_publication(change_set_id, owner=actor)
        if publication.state in {
            WorkspacePublicationState.PUBLISHED,
            WorkspacePublicationState.CONFLICTED,
        }:
            return publication
        change_set = self.store.get_change_set(change_set_id, owner=actor)
        self._validate_approval(change_set, actor=actor)
        workspace = self.store.get_workspace(publication.workspace_id, owner=actor)

        if publication.state is WorkspacePublicationState.PREPARED:
            publication = self._replace_publication(
                publication, state=WorkspacePublicationState.PUBLISHING
            )
        if change_set.state is WorkspaceChangeSetState.APPROVED:
            change_set = self._replace_change_set(
                change_set, state=WorkspaceChangeSetState.PUBLISHING
            )

        for index, file_record in enumerate(publication.files):
            if file_record.state == "committed":
                continue
            target_path = authorize_owner_path(
                self.owner_roots,
                owner=actor,
                target_path=posixpath.join(publication.target_root, file_record.path),
            )
            current = self.relay.path_sha256(path=target_path, owner=actor)
            if file_record.operation == "delete":
                if current is None:
                    publication = self._replace_file(publication, index, state="committed")
                    continue
                if current != file_record.expected_sha256:
                    return self._conflict(publication, change_set)
                assert file_record.expected_sha256 is not None
                outcome = self.relay.compare_and_delete_file(
                    target_path=target_path,
                    expected_sha256=file_record.expected_sha256,
                    owner=actor,
                )
            else:
                assert file_record.desired_sha256 is not None
                desired_sha256 = file_record.desired_sha256
                if current == desired_sha256:
                    publication = self._replace_file(publication, index, state="committed")
                    continue
                if current != file_record.expected_sha256:
                    return self._conflict(publication, change_set)
                if file_record.state == "pending":
                    content = self._local_content(
                        workspace.local_root,
                        file_record.path,
                        expected_sha256=desired_sha256,
                        expected_size=file_record.size_bytes,
                    )
                    assert file_record.staging_path is not None
                    self.relay.make_dir(
                        path=posixpath.dirname(file_record.staging_path), owner=actor
                    )
                    self._write_staged(file_record.staging_path, content, actor=actor)
                    staged_digest = self.relay.path_sha256(
                        path=file_record.staging_path, owner=actor
                    )
                    if staged_digest != desired_sha256:
                        return self._fail(publication, change_set, "staged_digest_mismatch")
                    publication = self._replace_file(publication, index, state="staged")
                    file_record = publication.files[index]
                assert file_record.staging_path is not None
                outcome = self.relay.compare_and_swap_file(
                    staged_path=file_record.staging_path,
                    target_path=target_path,
                    expected_sha256=file_record.expected_sha256,
                    desired_sha256=desired_sha256,
                    owner=actor,
                )
            if outcome == "conflict":
                return self._conflict(publication, change_set)
            if outcome == "staged_digest_mismatch":
                return self._fail(publication, change_set, outcome)
            if outcome not in {"committed", "already_committed"}:
                return self._fail(publication, change_set, "relay_protocol_error")
            publication = self._replace_file(publication, index, state="committed")

        publication = self._replace_publication(
            publication, state=WorkspacePublicationState.PUBLISHED, error_code=None
        )
        current_change_set = self.store.get_change_set(change_set_id, owner=actor)
        if current_change_set.state is not WorkspaceChangeSetState.PUBLISHED:
            self._replace_change_set(current_change_set, state=WorkspaceChangeSetState.PUBLISHED)
        staging_root = posixpath.join(
            publication.target_root, ".107pilot", "publish", publication.change_set_id
        )
        with suppress(Exception):
            self.relay.remove_path(path=staging_root, owner=actor)
        return publication

    def reconcile(self, change_set_id: str, *, actor: str) -> WorkspacePublication:
        publication = self.store.get_workspace_publication(change_set_id, owner=actor)
        if publication.state in {
            WorkspacePublicationState.PUBLISHED,
            WorkspacePublicationState.CONFLICTED,
        }:
            return publication
        return self.publish(change_set_id, actor=actor)

    def _validate_approval(self, change_set: WorkspaceChangeSet, *, actor: str) -> None:
        if change_set.owner != actor:
            raise KeyError(change_set.change_set_id)
        if change_set.state not in {
            WorkspaceChangeSetState.APPROVED,
            WorkspaceChangeSetState.PUBLISHING,
        }:
            raise ValueError("ChangeSet is not approved for publication")
        approval = change_set.approval
        if approval is None or approval.actor != actor:
            raise ValueError("ChangeSet approval actor is invalid")
        if approval.approved_digest != change_set.digest:
            raise ValueError("ChangeSet approval digest is stale")

    def _replace_publication(
        self, current: WorkspacePublication, **changes: Any
    ) -> WorkspacePublication:
        updated = replace(current, updated_at=self._now(), **changes)
        return self.store.replace_workspace_publication(updated, expected_version=current.version)

    def _replace_file(
        self,
        current: WorkspacePublication,
        index: int,
        *,
        state: Literal["pending", "staged", "committed"],
    ) -> WorkspacePublication:
        files = list(current.files)
        files[index] = replace(files[index], state=state)
        return self._replace_publication(current, files=tuple(files))

    def _replace_change_set(
        self, current: WorkspaceChangeSet, *, state: WorkspaceChangeSetState
    ) -> WorkspaceChangeSet:
        return self.store.replace_change_set(
            replace(current, state=state, updated_at=self._now()),
            expected_version=current.version,
        )

    def _conflict(
        self, publication: WorkspacePublication, change_set: WorkspaceChangeSet
    ) -> WorkspacePublication:
        result = self._replace_publication(
            publication,
            state=WorkspacePublicationState.CONFLICTED,
            error_code="workspace_conflict",
        )
        latest = self.store.get_change_set(change_set.change_set_id, owner=change_set.owner)
        if latest.state is not WorkspaceChangeSetState.CONFLICTED:
            self._replace_change_set(latest, state=WorkspaceChangeSetState.CONFLICTED)
        return result

    def _fail(
        self,
        publication: WorkspacePublication,
        change_set: WorkspaceChangeSet,
        error_code: str,
    ) -> WorkspacePublication:
        result = self._replace_publication(
            publication, state=WorkspacePublicationState.FAILED, error_code=error_code
        )
        latest = self.store.get_change_set(change_set.change_set_id, owner=change_set.owner)
        if latest.state is not WorkspaceChangeSetState.FAILED:
            self._replace_change_set(latest, state=WorkspaceChangeSetState.FAILED)
        return result

    def _write_staged(self, path: str, content: bytes, *, actor: str) -> None:
        if not content:
            self.relay.write_bytes_chunk(path=path, data_b64="", offset=0, owner=actor)
            return
        for offset in range(0, len(content), self.chunk_bytes):
            block = content[offset : offset + self.chunk_bytes]
            self.relay.write_bytes_chunk(
                path=path,
                data_b64=base64.b64encode(block).decode("ascii"),
                offset=0 if offset == 0 else -1,
                owner=actor,
            )

    @staticmethod
    def _local_content(
        local_root: str,
        relative: str,
        *,
        expected_sha256: str,
        expected_size: int,
    ) -> bytes:
        root = Path(local_root).resolve()
        target = root.joinpath(*PurePosixPath(relative).parts).resolve()
        if target == root or root not in target.parents or not target.is_file():
            raise ValueError("Workspace publication path escaped the local root")
        content = target.read_bytes()
        if len(content) != expected_size or hashlib.sha256(content).hexdigest() != expected_sha256:
            raise ValueError("Workspace file no longer matches the approved digest")
        return content

    @staticmethod
    def _relative(value: str) -> str:
        return WorkspacePublisher._relative_path(value)

    @staticmethod
    def _relative_path(value: str) -> str:
        path = PurePosixPath(value)
        if (
            path.is_absolute()
            or not path.parts
            or any(part in {"", ".", ".."} for part in path.parts)
        ):
            raise ValueError("publication file path is invalid")
        return str(path)

    def _now(self) -> str:
        value = self._clock()
        if value.tzinfo is None:
            raise ValueError("WorkspacePublisher clock must return an aware datetime")
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def publication_payload(record: WorkspacePublication) -> dict[str, Any]:
    return {
        "publication_id": record.publication_id,
        "change_set_id": record.change_set_id,
        "project_id": record.project_id,
        "workspace_id": record.workspace_id,
        "owner": record.owner,
        "target_root": record.target_root,
        "approved_digest": record.approved_digest,
        "approved_by": record.approved_by,
        "state": record.state.value,
        "version": record.version,
        "files": [
            {
                "path": item.path,
                "operation": item.operation,
                "expected_sha256": item.expected_sha256,
                "desired_sha256": item.desired_sha256,
                "staging_path": item.staging_path,
                "state": item.state,
                "size_bytes": item.size_bytes,
            }
            for item in record.files
        ],
        "error_code": record.error_code,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
    }


def publication_from_payload(value: Mapping[str, Any]) -> WorkspacePublication:
    raw_files = value.get("files")
    if not isinstance(raw_files, list):
        raise TypeError("publication files must be an array")
    return WorkspacePublication(
        publication_id=str(value["publication_id"]),
        change_set_id=str(value["change_set_id"]),
        project_id=str(value["project_id"]),
        workspace_id=str(value["workspace_id"]),
        owner=str(value["owner"]),
        target_root=str(value["target_root"]),
        approved_digest=str(value["approved_digest"]),
        approved_by=str(value["approved_by"]),
        state=WorkspacePublicationState(str(value["state"])),
        version=int(value["version"]),
        files=tuple(
            WorkspacePublicationFile(
                path=str(item["path"]),
                operation=str(item["operation"]),  # type: ignore[arg-type]
                expected_sha256=(
                    None if item.get("expected_sha256") is None else str(item["expected_sha256"])
                ),
                desired_sha256=(
                    None if item.get("desired_sha256") is None else str(item["desired_sha256"])
                ),
                staging_path=(
                    None if item.get("staging_path") is None else str(item["staging_path"])
                ),
                state=str(item["state"]),  # type: ignore[arg-type]
                size_bytes=int(item["size_bytes"]),
            )
            for item in raw_files
            if isinstance(item, Mapping)
        ),
        error_code=None if value.get("error_code") is None else str(value["error_code"]),
        created_at=str(value["created_at"]),
        updated_at=str(value["updated_at"]),
    )
