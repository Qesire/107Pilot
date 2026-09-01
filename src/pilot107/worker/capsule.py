"""Raw Capsule build and verification primitives for Phase 0A."""

from __future__ import annotations

import ctypes
import errno
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from pilot107.agent.tasks import AgentTaskGateReceipt
from pilot107.core.evidence_binding import EvidenceBinder, EvidenceBindingError
from pilot107.core.run_store import (
    CapsuleBuildFenceConflict,
    EvidenceObjectRecord,
    RunStore,
)
from pilot107.core.states import CapsuleState, CollectionState, EvidenceSealState
from pilot107.worker.evidence import EvidenceStore


class CapsuleError(RuntimeError):
    """Raised when a capsule cannot be built or verified."""


@dataclass(frozen=True)
class CapsuleBuildResult:
    run_id: str
    capsule_id: str
    capsule_dir: Path
    manifest_sha256: str
    files_copied: int
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class CapsuleVerifyResult:
    valid: bool
    capsule_id: str | None
    checked_files: int
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class RawCapsuleReadResult:
    run_id: str
    capsule_id: str
    manifest_sha256: str
    files_copied: int
    manifest: dict[str, Any]
    valid: bool
    checked_files: int
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class CapsuleEvidenceFile:
    logical_path: str
    store_path: Path
    sha256: str
    size_bytes: int
    source_ref: str


@dataclass(frozen=True)
class CapsuleEvidenceAuthority:
    run_id: str
    object_set_digest: str
    seal_digest: str
    seal_ref: str
    files: tuple[CapsuleEvidenceFile, ...]


_BOOKKEEPING_FILES = frozenset(
    {"manifest.json", "provenance.json", "collection_policy.json", "checksums.txt"}
)


class RawCapsuleService:
    def __init__(
        self,
        *,
        store: RunStore,
        evidence_store: EvidenceStore,
        capsule_root: Path,
        creator: str = "pilot107-worker",
    ) -> None:
        self.store = store
        self.evidence_store = evidence_store
        self.capsule_root = capsule_root
        self.creator = creator

    def build_raw_capsule(
        self,
        run_id: str,
        *,
        operation_key: str | None = None,
        lease_assert: Callable[[], None] | None = None,
        failure_state: CapsuleState = CapsuleState.FAILED,
    ) -> CapsuleBuildResult:
        if failure_state not in {CapsuleState.PENDING, CapsuleState.FAILED}:
            raise ValueError("Capsule failure_state must be pending or failed")
        run = self.store.get_run(run_id)
        if run.collection_state != CollectionState.SUCCEEDED:
            raise CapsuleError(f"run evidence is not fully collected: {run.collection_state}")
        seal = self.store.get_evidence_seal(run_id)
        if (
            seal.state is not EvidenceSealState.SEALED
            or seal.digest is None
            or seal.marker_ref is None
            or seal.sealed_at is None
        ):
            raise CapsuleError("run evidence is not sealed")

        evidence_receipt = self._verify_sealed_evidence(run_id)
        authority = _capsule_authority(
            self.store,
            run_id,
            evidence_digest=evidence_receipt.evidence_digest,
            seal_digest=evidence_receipt.seal_digest or "",
            seal_ref=evidence_receipt.seal_marker_ref or "",
        )
        effective_operation_key = operation_key or hashlib.sha256(
            f"raw-capsule\0{run_id}\0{authority.seal_digest}".encode()
        ).hexdigest()
        if lease_assert is not None:
            lease_assert()
        claim = self.store.begin_capsule_build(
            run_id,
            operation_key=effective_operation_key,
        )
        if run.capsule_state is CapsuleState.READY:
            with _capsule_build_lock(self.capsule_root, run_id):
                result = self._existing_build_result(
                    run_id,
                    _capsule_dir(self.capsule_root, run_id),
                    authority=authority,
                )
            if lease_assert is not None:
                lease_assert()
            self.store.assert_capsule_build(
                run_id,
                operation_key=effective_operation_key,
                fencing_token=claim.fencing_token,
            )
            return result
        try:
            with _capsule_build_lock(self.capsule_root, run_id):
                result = self._build_raw_capsule(
                    run_id,
                    authority=authority,
                    evidence_sealed_at=seal.sealed_at,
                    operation_key=effective_operation_key,
                    fencing_token=claim.fencing_token,
                    lease_assert=lease_assert,
                )
            if lease_assert is not None:
                lease_assert()
            self.store.assert_capsule_build(
                run_id,
                operation_key=effective_operation_key,
                fencing_token=claim.fencing_token,
            )
        except Exception as exc:
            try:
                self.store.finish_capsule_build(
                    run_id,
                    operation_key=effective_operation_key,
                    fencing_token=claim.fencing_token,
                    state=failure_state,
                    event_type=(
                        "capsule.auto_build_retry"
                        if failure_state is CapsuleState.PENDING
                        else "capsule.failed"
                    ),
                    payload={"message": str(exc)},
                )
            except CapsuleBuildFenceConflict as fence_exc:
                raise fence_exc from exc
            if isinstance(exc, CapsuleError):
                raise
            raise CapsuleError("raw Capsule build failed") from exc

        self.store.finish_capsule_build(
            run_id,
            operation_key=effective_operation_key,
            fencing_token=claim.fencing_token,
            state=CapsuleState.READY,
            event_type="capsule.ready",
            payload={
                "capsule_id": result.capsule_id,
                "capsule_dir": str(result.capsule_dir),
                "manifest_sha256": result.manifest_sha256,
            },
        )
        return result

    def get_raw_capsule(self, run_id: str) -> RawCapsuleReadResult:
        run = self.store.get_run(run_id)
        if run.capsule_state != CapsuleState.READY:
            raise CapsuleError(f"raw Capsule is not ready: {run.capsule_state}")
        receipt = self._verify_sealed_evidence(run_id)
        authority = _capsule_authority(
            self.store,
            run_id,
            evidence_digest=receipt.evidence_digest,
            seal_digest=receipt.seal_digest or "",
            seal_ref=receipt.seal_marker_ref or "",
        )
        capsule_dir = _capsule_dir(self.capsule_root, run_id)
        manifest_path = capsule_dir / "manifest.json"
        verify = verify_raw_capsule(capsule_dir, authority=authority)
        if not verify.valid:
            raise CapsuleError(f"raw Capsule verification failed: {verify.errors}")
        manifest = _read_json_object(manifest_path, "raw Capsule manifest")
        capsule_id = str(manifest.get("capsule_id") or "")
        if not capsule_id:
            raise CapsuleError("raw Capsule manifest has no capsule_id")
        files = manifest.get("files")
        files_copied = len(files) if isinstance(files, list) else 0
        limitations = manifest.get("limitations")
        warnings = [str(item) for item in limitations] if isinstance(limitations, list) else []
        warnings.extend(verify.warnings)
        return RawCapsuleReadResult(
            run_id=run_id,
            capsule_id=capsule_id,
            manifest_sha256=_sha256(manifest_path),
            files_copied=files_copied,
            manifest=manifest,
            valid=verify.valid,
            checked_files=verify.checked_files,
            warnings=warnings,
            errors=verify.errors,
        )

    def _build_raw_capsule(
        self,
        run_id: str,
        *,
        authority: CapsuleEvidenceAuthority,
        evidence_sealed_at: str,
        operation_key: str,
        fencing_token: int,
        lease_assert: Callable[[], None] | None,
    ) -> CapsuleBuildResult:
        run = self.store.get_run(run_id)
        capsule_id = "capsule_" + hashlib.sha256(
            f"raw\0{run_id}\0{authority.seal_digest}".encode()
        ).hexdigest()[:32]
        capsule_dir = _capsule_dir(self.capsule_root, run_id)
        if _path_exists_without_following(capsule_dir):
            return self._existing_build_result(
                run_id,
                capsule_dir,
                authority=authority,
            )
        temp_dir = capsule_dir.with_name(f".raw-{uuid4().hex}.tmp")
        temp_dir.mkdir(parents=True, exist_ok=False)

        warnings: list[str] = []
        copied: list[dict[str, Any]] = []
        try:
            for artifact in authority.files:
                logical_path = artifact.logical_path
                source = artifact.store_path
                destination = _safe_join(temp_dir, logical_path)
                destination.parent.mkdir(parents=True, exist_ok=True)
                _copy_regular_file(source, destination)
                copied.append(
                    {
                        "logical_path": logical_path,
                        "category": logical_path.split("/", 1)[0],
                        "sha256": artifact.sha256,
                        "size_bytes": artifact.size_bytes,
                        "source": artifact.source_ref,
                        "classification": "original_or_execution_record",
                        "collection_status": "collected",
                    }
                )

            provenance = {
                "schema_version": "107pilot.capsule_provenance.v1",
                "run_id": run_id,
                "created_at": evidence_sealed_at,
                "creator": self.creator,
                "source_evidence_manifest_sha256": next(
                    item.sha256
                    for item in authority.files
                    if item.logical_path == "manifest/manifest.json"
                ),
                "source_evidence_manifest_ref": f"evidence://runs/{run_id}/manifest/manifest.json",
                "source_evidence_object_set_digest": authority.object_set_digest,
                "source_evidence_seal_digest": authority.seal_digest,
                "source_evidence_seal_ref": authority.seal_ref,
            }
            _write_json(temp_dir / "provenance.json", provenance)

            policy = {
                "schema_version": "107pilot.collection_policy.v1",
                "capsule_type": "raw",
                "copy_mode": "sealed_evidence_object_set",
                "limitations": warnings,
            }
            _write_json(temp_dir / "collection_policy.json", policy)

            manifest = {
                "schema_version": "107pilot.capsule.v1",
                "capsule_id": capsule_id,
                "capsule_type": "raw",
                "run_id": run_id,
                "created_at": evidence_sealed_at,
                "creator": self.creator,
                "source_evidence": {
                    "object_set_digest": authority.object_set_digest,
                    "seal_digest": authority.seal_digest,
                    "seal_ref": authority.seal_ref,
                },
                "run_summary": {
                    "job_id": run.job_id,
                    "terminal_state": run.terminal_state,
                    "exit_code": run.exit_code,
                    "run_state": run.state.value,
                    "collection_state": run.collection_state.value,
                },
                "files": sorted(copied, key=lambda item: item["logical_path"]),
                "limitations": warnings,
            }
            _write_json(temp_dir / "manifest.json", manifest)

            checksums = _build_checksums(temp_dir)
            (temp_dir / "checksums.txt").write_text(checksums, encoding="utf-8")
            verify = verify_raw_capsule(temp_dir, authority=authority)
            if not verify.valid:
                raise CapsuleError(f"capsule verify failed: {verify.errors}")
            if lease_assert is not None:
                lease_assert()
            self.store.assert_capsule_build(
                run_id,
                operation_key=operation_key,
                fencing_token=fencing_token,
            )
            published_here = _publish_directory_once(temp_dir, capsule_dir)
            published = verify_raw_capsule(capsule_dir, authority=authority)
            if not published.valid:
                raise CapsuleError(f"published Capsule verification failed: {published.errors}")
            if not published_here:
                shutil.rmtree(temp_dir)
        except Exception:
            if _path_exists_without_following(temp_dir) and not temp_dir.is_symlink():
                shutil.rmtree(temp_dir)
            raise

        return CapsuleBuildResult(
            run_id=run_id,
            capsule_id=capsule_id,
            capsule_dir=capsule_dir,
            manifest_sha256=_sha256(capsule_dir / "manifest.json"),
            files_copied=len(copied),
            warnings=warnings,
        )

    def _verify_sealed_evidence(self, run_id: str) -> AgentTaskGateReceipt:
        run = self.store.get_run(run_id)
        refs = tuple(
            f"evidence://runs/{run_id}/{item.logical_path}"
            for item in self.store.list_evidence_objects(run_id)
        )
        try:
            return EvidenceBinder(
                store=self.store,
                evidence_root=self.evidence_store.root,
            ).verify_terminal_gate(
                run_id,
                refs,
                {
                    "workspace_revision": run.workspace_revision,
                    "workspace_digest": run.workspace_digest,
                    "legacy_boundary": run.workspace_revision is None,
                    "source_revision": run.source_revision,
                    "platform_snapshot_ref": run.platform_snapshot_ref,
                },
            )
        except EvidenceBindingError as exc:
            raise CapsuleError(f"sealed Evidence verification failed: {exc.code}") from exc

    def _existing_build_result(
        self,
        run_id: str,
        capsule_dir: Path,
        *,
        authority: CapsuleEvidenceAuthority,
    ) -> CapsuleBuildResult:
        expected_capsule_id = "capsule_" + hashlib.sha256(
            f"raw\0{run_id}\0{authority.seal_digest}".encode()
        ).hexdigest()[:32]
        verify = verify_raw_capsule(capsule_dir, authority=authority)
        if not verify.valid or verify.capsule_id != expected_capsule_id:
            raise CapsuleError("existing raw Capsule is invalid")
        manifest = _read_json_object(capsule_dir / "manifest.json", "Capsule manifest")
        files = manifest.get("files")
        limitations = manifest.get("limitations")
        return CapsuleBuildResult(
            run_id=run_id,
            capsule_id=expected_capsule_id,
            capsule_dir=capsule_dir,
            manifest_sha256=_sha256(capsule_dir / "manifest.json"),
            files_copied=len(files) if isinstance(files, list) else 0,
            warnings=[str(item) for item in limitations] if isinstance(limitations, list) else [],
        )


def capsule_authority_from_store(store: RunStore, run_id: str) -> CapsuleEvidenceAuthority:
    seal = store.get_evidence_seal(run_id)
    if (
        seal.state is not EvidenceSealState.SEALED
        or seal.digest is None
        or seal.marker_ref is None
    ):
        raise CapsuleError("sealed Evidence authority is unavailable")
    objects = store.list_evidence_objects(run_id)
    object_set_digests = {
        item.integrity_object_set_digest
        for item in objects
        if item.integrity_object_set_digest is not None
    }
    if len(object_set_digests) != 1:
        raise CapsuleError("sealed Evidence object-set authority is unavailable")
    return _capsule_authority(
        store,
        run_id,
        evidence_digest=next(iter(object_set_digests)),
        seal_digest=seal.digest,
        seal_ref=seal.marker_ref,
    )


def verify_raw_capsule(
    capsule_dir: Path,
    *,
    authority: CapsuleEvidenceAuthority | None = None,
) -> CapsuleVerifyResult:
    errors: list[str] = []
    warnings: list[str] = []
    capsule_id: str | None = None
    checked_files = 0
    try:
        files, directories = _scan_capsule_tree(capsule_dir)
    except CapsuleError as exc:
        errors.append(str(exc))
        return CapsuleVerifyResult(False, None, checked_files, warnings, errors)
    if authority is None:
        errors.append("sealed Evidence authority is required")
    manifest_path = files.get("manifest.json")
    checksums_path = files.get("checksums.txt")
    if manifest_path is None:
        errors.append("manifest.json missing")
        return CapsuleVerifyResult(False, None, checked_files, warnings, errors)
    try:
        manifest = _read_json_object(manifest_path, "manifest.json")
    except CapsuleError as exc:
        errors.append(str(exc))
        return CapsuleVerifyResult(False, None, checked_files, warnings, errors)
    capsule_id = str(manifest.get("capsule_id") or "")
    if manifest.get("schema_version") != "107pilot.capsule.v1":
        errors.append("unsupported manifest schema_version")
    if manifest.get("capsule_type") != "raw":
        errors.append("unsupported capsule_type")
    raw_entries = manifest.get("files")
    entries = raw_entries if isinstance(raw_entries, list) else []
    if not isinstance(raw_entries, list):
        errors.append("manifest files must be a list")
    if not entries:
        errors.append("manifest payload is empty")
    payload_paths: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            errors.append("manifest file entry is invalid")
            continue
        logical_path = str(entry.get("logical_path") or "")
        try:
            _validate_logical_path(logical_path)
        except CapsuleError as exc:
            errors.append(str(exc))
            continue
        if logical_path in payload_paths:
            errors.append(f"duplicate manifest file: {logical_path}")
            continue
        payload_paths.add(logical_path)
        path = files.get(logical_path)
        if path is None:
            errors.append(f"manifest file missing: {logical_path}")
            continue
        try:
            digest, size = _regular_file_digest(path)
        except CapsuleError as exc:
            errors.append(str(exc))
            continue
        if str(entry.get("sha256") or "") != digest:
            errors.append(f"manifest sha256 mismatch: {logical_path}")
        if entry.get("size_bytes") != size:
            errors.append(f"manifest size mismatch: {logical_path}")

    expected_files = set(_BOOKKEEPING_FILES) | payload_paths
    actual_files = set(files)
    for missing in sorted(expected_files - actual_files):
        errors.append(f"required Capsule file missing: {missing}")
    for extra in sorted(actual_files - expected_files):
        errors.append(f"unexpected Capsule file: {extra}")
    expected_directories = {
        parent.as_posix()
        for logical_path in payload_paths
        for parent in Path(logical_path).parents
        if parent != Path(".")
    }
    for extra in sorted(directories - expected_directories):
        errors.append(f"unexpected Capsule directory: {extra}")
    for missing in sorted(expected_directories - directories):
        errors.append(f"required Capsule directory missing: {missing}")

    if checksums_path is None:
        errors.append("checksums.txt missing")
    else:
        try:
            checksum_text = _read_regular_bytes(checksums_path).decode("utf-8")
        except (CapsuleError, UnicodeDecodeError) as exc:
            errors.append(f"checksums.txt is unreadable: {exc}")
            checksum_text = ""
        declared: set[str] = set()
        for line_number, line in enumerate(checksum_text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                expected_sha, logical_path = line.split("  ", 1)
            except ValueError:
                errors.append(f"invalid checksum line {line_number}")
                continue
            if logical_path in declared:
                errors.append(f"duplicate checksum file: {logical_path}")
                continue
            declared.add(logical_path)
            path = files.get(logical_path)
            if path is None:
                errors.append(f"checksum file missing: {logical_path}")
                continue
            checked_files += 1
            try:
                digest, _ = _regular_file_digest(path)
            except CapsuleError as exc:
                errors.append(str(exc))
                continue
            if digest != expected_sha:
                errors.append(f"checksum mismatch: {logical_path}")
        expected_checksum_files = actual_files - {"checksums.txt"}
        for missing in sorted(expected_checksum_files - declared):
            errors.append(f"checksum declaration missing: {missing}")
        for extra in sorted(declared - expected_checksum_files):
            errors.append(f"unexpected checksum declaration: {extra}")
        if not errors:
            canonical = _checksums_for_files(files)
            if checksum_text != canonical:
                errors.append("checksums.txt is not canonical")

    if authority is not None:
        expected_entries = [_authority_manifest_entry(item) for item in authority.files]
        if manifest.get("run_id") != authority.run_id:
            errors.append("manifest Run authority mismatch")
        if entries != expected_entries:
            errors.append("manifest payload does not match sealed Evidence authority")
        expected_source = {
            "object_set_digest": authority.object_set_digest,
            "seal_digest": authority.seal_digest,
            "seal_ref": authority.seal_ref,
        }
        if manifest.get("source_evidence") != expected_source:
            errors.append("manifest source Evidence authority mismatch")
        expected_capsule_id = "capsule_" + hashlib.sha256(
            f"raw\0{authority.run_id}\0{authority.seal_digest}".encode()
        ).hexdigest()[:32]
        if capsule_id != expected_capsule_id:
            errors.append("capsule_id authority mismatch")
        provenance_path = files.get("provenance.json")
        if provenance_path is not None:
            try:
                provenance = _read_json_object(provenance_path, "provenance.json")
            except CapsuleError as exc:
                errors.append(str(exc))
            else:
                expected_provenance = {
                    "source_evidence_object_set_digest": authority.object_set_digest,
                    "source_evidence_seal_digest": authority.seal_digest,
                    "source_evidence_seal_ref": authority.seal_ref,
                }
                if any(
                    provenance.get(key) != value
                    for key, value in expected_provenance.items()
                ):
                    errors.append("provenance source Evidence authority mismatch")

    return CapsuleVerifyResult(
        valid=not errors,
        capsule_id=capsule_id or None,
        checked_files=checked_files,
        warnings=warnings,
        errors=errors,
    )


def _safe_join(root: Path, logical_path: str) -> Path:
    _validate_logical_path(logical_path)
    return root.absolute() / logical_path


def _sha256(path: Path) -> str:
    return _regular_file_digest(path)[0]


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _build_checksums(capsule_dir: Path) -> str:
    files, _ = _scan_capsule_tree(capsule_dir)
    return _checksums_for_files(files)


def _checksums_for_files(files: dict[str, Path]) -> str:
    lines = [
        f"{_sha256(files[logical_path])}  {logical_path}"
        for logical_path in sorted(files)
        if logical_path != "checksums.txt"
    ]
    return "\n".join(lines) + ("\n" if lines else "")


def _capsule_authority(
    store: RunStore,
    run_id: str,
    *,
    evidence_digest: str,
    seal_digest: str,
    seal_ref: str,
) -> CapsuleEvidenceAuthority:
    if re.fullmatch(r"[0-9a-f]{64}", evidence_digest) is None:
        raise CapsuleError("Evidence object-set digest is invalid")
    if re.fullmatch(r"[0-9a-f]{64}", seal_digest) is None or not seal_ref:
        raise CapsuleError("Evidence seal authority is invalid")
    files: list[CapsuleEvidenceFile] = []
    for item in sorted(store.list_evidence_objects(run_id), key=lambda value: value.logical_path):
        _validate_authoritative_object(item, run_id, evidence_digest)
        assert item.sha256 is not None
        assert item.size_bytes is not None
        assert item.source_uri is not None
        files.append(
            CapsuleEvidenceFile(
                logical_path=item.logical_path,
                store_path=Path(item.store_path),
                sha256=item.sha256.lower(),
                size_bytes=item.size_bytes,
                source_ref=item.source_uri,
            )
        )
    if not files or not any(item.logical_path == "manifest/manifest.json" for item in files):
        raise CapsuleError("sealed Evidence authority has no complete payload")
    return CapsuleEvidenceAuthority(
        run_id=run_id,
        object_set_digest=evidence_digest,
        seal_digest=seal_digest,
        seal_ref=seal_ref,
        files=tuple(files),
    )


def _validate_authoritative_object(
    item: EvidenceObjectRecord,
    run_id: str,
    evidence_digest: str,
) -> None:
    _validate_logical_path(item.logical_path)
    if item.run_id != run_id:
        raise CapsuleError("Evidence object Run authority mismatch")
    expected_ref = f"evidence://runs/{run_id}/{item.logical_path}"
    if item.source_uri != expected_ref:
        raise CapsuleError("Evidence object source authority mismatch")
    if item.collection_status != "collected" or item.finalized_at is None:
        raise CapsuleError("Evidence object is not finalized")
    if item.integrity_checked_at is None or item.integrity_invalidated_at is not None:
        raise CapsuleError("Evidence object integrity authority is invalid")
    if item.integrity_object_set_digest != evidence_digest:
        raise CapsuleError("Evidence object-set authority mismatch")
    if item.sha256 is None or re.fullmatch(r"[0-9a-fA-F]{64}", item.sha256) is None:
        raise CapsuleError("Evidence object sha256 authority is invalid")
    if item.size_bytes is None or item.size_bytes < 0:
        raise CapsuleError("Evidence object size authority is invalid")


def _authority_manifest_entry(item: CapsuleEvidenceFile) -> dict[str, Any]:
    return {
        "logical_path": item.logical_path,
        "category": item.logical_path.split("/", 1)[0],
        "sha256": item.sha256,
        "size_bytes": item.size_bytes,
        "source": item.source_ref,
        "classification": "original_or_execution_record",
        "collection_status": "collected",
    }


def _validate_logical_path(logical_path: str) -> None:
    logical = Path(logical_path)
    if (
        not logical_path
        or logical.is_absolute()
        or logical_path.startswith("/")
        or ".." in logical.parts
        or "." in logical.parts
        or logical.as_posix() != logical_path
    ):
        raise CapsuleError(f"unsafe capsule path: {logical_path!r}")


def _scan_capsule_tree(capsule_dir: Path) -> tuple[dict[str, Path], set[str]]:
    root = capsule_dir.absolute()
    _assert_no_symlink_path(root, require_final_directory=True)
    files: dict[str, Path] = {}
    directories: set[str] = set()

    def visit(directory: Path, prefix: Path) -> None:
        try:
            entries = list(os.scandir(directory))
        except OSError as exc:
            raise CapsuleError("Capsule directory is unreadable") from exc
        for entry in entries:
            logical = (prefix / entry.name).as_posix()
            try:
                metadata = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise CapsuleError(f"Capsule member is unreadable: {logical}") from exc
            if stat.S_ISLNK(metadata.st_mode):
                raise CapsuleError(f"Capsule symlink is forbidden: {logical}")
            path = Path(entry.path)
            if stat.S_ISDIR(metadata.st_mode):
                directories.add(logical)
                visit(path, prefix / entry.name)
                continue
            if not stat.S_ISREG(metadata.st_mode):
                raise CapsuleError(f"Capsule special file is forbidden: {logical}")
            if metadata.st_nlink != 1:
                raise CapsuleError(f"Capsule hardlink is forbidden: {logical}")
            files[logical] = path

    visit(root, Path())
    return files, directories


def _assert_no_symlink_path(path: Path, *, require_final_directory: bool) -> None:
    absolute = path.absolute()
    current = Path(absolute.anchor)
    for component in absolute.parts[1:]:
        current /= component
        try:
            metadata = current.lstat()
        except FileNotFoundError as exc:
            raise CapsuleError(f"Capsule path is missing: {current}") from exc
        except OSError as exc:
            raise CapsuleError(f"Capsule path is unreadable: {current}") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise CapsuleError(f"Capsule path symlink is forbidden: {current}")
    if require_final_directory and not stat.S_ISDIR(absolute.lstat().st_mode):
        raise CapsuleError("Capsule root is not a directory")


def _read_regular_bytes(path: Path) -> bytes:
    descriptor: int | None = None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise CapsuleError(f"Capsule member is not an independent regular file: {path}")
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        return b"".join(chunks)
    except CapsuleError:
        raise
    except OSError as exc:
        raise CapsuleError(f"Capsule member is unreadable: {path}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _regular_file_digest(path: Path) -> tuple[str, int]:
    content = _read_regular_bytes(path)
    return hashlib.sha256(content).hexdigest(), len(content)


def _copy_regular_file(source: Path, destination: Path) -> None:
    source_fd: int | None = None
    destination_fd: int | None = None
    try:
        source_fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        source_metadata = os.fstat(source_fd)
        if not stat.S_ISREG(source_metadata.st_mode):
            raise CapsuleError("Evidence source is not a regular file")
        destination_fd = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
        )
        while chunk := os.read(source_fd, 1024 * 1024):
            view = memoryview(chunk)
            while view:
                written = os.write(destination_fd, view)
                if written <= 0:
                    raise OSError("short Capsule copy write")
                view = view[written:]
        os.fsync(destination_fd)
    except CapsuleError:
        raise
    except OSError as exc:
        raise CapsuleError("Evidence copy into Capsule failed") from exc
    finally:
        if destination_fd is not None:
            os.close(destination_fd)
        if source_fd is not None:
            os.close(source_fd)


def _publish_directory_once(source: Path, destination: Path) -> bool:
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise CapsuleError("atomic no-replace Capsule publication is unavailable")
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    result = renameat2(
        -100,
        os.fsencode(source),
        -100,
        os.fsencode(destination),
        1,
    )
    if result == 0:
        return True
    error = ctypes.get_errno()
    if error == errno.EEXIST:
        return False
    raise CapsuleError(f"raw Capsule publication failed: errno={error}")


def _capsule_dir(capsule_root: Path, run_id: str) -> Path:
    if re.fullmatch(r"[A-Za-z0-9_.:-]+", run_id) is None:
        raise CapsuleError("Capsule run_id is invalid")
    return capsule_root.absolute() / "runs" / run_id / "raw"


def _path_exists_without_following(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise CapsuleError(f"Capsule path is unreadable: {path}") from exc
    return True


def _read_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(_read_regular_bytes(path).decode("utf-8"))
    except (CapsuleError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CapsuleError(f"{label} is unreadable") from exc
    if not isinstance(value, dict):
        raise CapsuleError(f"{label} is invalid")
    return value


@contextmanager
def _capsule_build_lock(capsule_root: Path, run_id: str) -> Iterator[None]:
    if re.fullmatch(r"[A-Za-z0-9_.:-]+", run_id) is None:
        raise CapsuleError("Capsule run_id is invalid")
    root = capsule_root.absolute()
    _ensure_secure_directory(root)
    runs_root = root / "runs"
    _ensure_secure_directory(runs_root)
    run_root = runs_root / run_id
    _ensure_secure_directory(run_root)
    lock_path = run_root / ".raw.lock"
    try:
        descriptor = os.open(
            lock_path,
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
        )
    except OSError as exc:
        raise CapsuleError("Capsule build lock is unavailable") from exc
    with os.fdopen(descriptor, "a+b") as handle:
        metadata = os.fstat(handle.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise CapsuleError("Capsule build lock is not a regular file")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _ensure_secure_directory(path: Path) -> None:
    absolute = path.absolute()
    current = Path(absolute.anchor)
    for component in absolute.parts[1:]:
        current /= component
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            try:
                current.mkdir(mode=0o700)
            except OSError as exc:
                raise CapsuleError(f"Capsule directory creation failed: {current}") from exc
            metadata = current.lstat()
        except OSError as exc:
            raise CapsuleError(f"Capsule directory is unreadable: {current}") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise CapsuleError(f"Capsule path symlink is forbidden: {current}")
        if not stat.S_ISDIR(metadata.st_mode):
            raise CapsuleError(f"Capsule path is not a directory: {current}")
