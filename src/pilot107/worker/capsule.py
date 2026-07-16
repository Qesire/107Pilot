"""Raw Capsule build and verification primitives for Phase 0A."""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from pilot107.core.run_store import RunStore, utc_now_iso
from pilot107.core.states import CapsuleState, CollectionState
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

    def build_raw_capsule(self, run_id: str) -> CapsuleBuildResult:
        run = self.store.get_run(run_id)
        if run.collection_state != CollectionState.SUCCEEDED:
            raise CapsuleError(f"run evidence is not fully collected: {run.collection_state}")

        self.store.update_capsule_state(run_id, CapsuleState.RUNNING, event_type="capsule.running")
        try:
            result = self._build_raw_capsule(run_id)
        except CapsuleError as exc:
            self.store.update_capsule_state(
                run_id,
                CapsuleState.FAILED,
                event_type="capsule.failed",
                payload={"message": str(exc)},
            )
            raise
        except Exception as exc:
            message = "raw Capsule build failed"
            self.store.update_capsule_state(
                run_id,
                CapsuleState.FAILED,
                event_type="capsule.failed",
                payload={"message": message},
            )
            raise CapsuleError(message) from exc

        self.store.update_capsule_state(
            run_id,
            CapsuleState.READY,
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
        capsule_dir = (self.capsule_root / "runs" / run_id / "raw").resolve()
        manifest_path = capsule_dir / "manifest.json"
        if not manifest_path.is_file():
            raise CapsuleError("raw Capsule manifest is missing")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise CapsuleError("raw Capsule manifest is unreadable") from exc
        if not isinstance(manifest, dict):
            raise CapsuleError("raw Capsule manifest is invalid")
        verify = verify_raw_capsule(capsule_dir)
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

    def _build_raw_capsule(self, run_id: str) -> CapsuleBuildResult:
        run = self.store.get_run(run_id)
        evidence_root = self.evidence_store.run_root(run_id).resolve()
        evidence_manifest_path = evidence_root / "manifest" / "manifest.json"
        if not evidence_manifest_path.exists():
            raise CapsuleError(f"evidence manifest missing: {evidence_manifest_path}")
        evidence_manifest = json.loads(evidence_manifest_path.read_text(encoding="utf-8"))

        capsule_id = f"capsule_{uuid4().hex}"
        capsule_dir = (self.capsule_root / "runs" / run_id / "raw").resolve()
        temp_dir = capsule_dir.with_name(f".raw-{uuid4().hex}.tmp")
        if temp_dir.exists():
            shutil.rmtree(temp_dir)
        temp_dir.mkdir(parents=True, exist_ok=False)

        warnings: list[str] = []
        copied: list[dict[str, Any]] = []
        try:
            for artifact in evidence_manifest.get("artifacts", []):
                logical_path = str(artifact.get("logical_path", ""))
                source = _safe_join(evidence_root, logical_path)
                destination = _safe_join(temp_dir, logical_path)
                if not source.exists() or not source.is_file():
                    warnings.append(f"missing evidence artifact skipped: {logical_path}")
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
                copied.append(
                    {
                        "logical_path": logical_path,
                        "category": logical_path.split("/", 1)[0],
                        "sha256": _sha256(destination),
                        "size_bytes": destination.stat().st_size,
                        "source": f"evidence://runs/{run_id}/{logical_path}",
                        "classification": "original_or_execution_record",
                        "collection_status": "collected",
                    }
                )

            provenance = {
                "schema_version": "107pilot.capsule_provenance.v1",
                "run_id": run_id,
                "created_at": utc_now_iso(),
                "creator": self.creator,
                "source_evidence_manifest_sha256": _sha256(evidence_manifest_path),
                "source_evidence_manifest_ref": f"evidence://runs/{run_id}/manifest/manifest.json",
            }
            _write_json(temp_dir / "provenance.json", provenance)

            policy = {
                "schema_version": "107pilot.collection_policy.v1",
                "capsule_type": "raw",
                "copy_mode": "manifest_artifacts_only",
                "limitations": warnings,
            }
            _write_json(temp_dir / "collection_policy.json", policy)

            manifest = {
                "schema_version": "107pilot.capsule.v1",
                "capsule_id": capsule_id,
                "capsule_type": "raw",
                "run_id": run_id,
                "created_at": utc_now_iso(),
                "creator": self.creator,
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
            verify = verify_raw_capsule(temp_dir)
            if not verify.valid:
                raise CapsuleError(f"capsule verify failed: {verify.errors}")

            if capsule_dir.exists():
                shutil.rmtree(capsule_dir)
            capsule_dir.parent.mkdir(parents=True, exist_ok=True)
            temp_dir.replace(capsule_dir)
        except Exception:
            if temp_dir.exists():
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


def verify_raw_capsule(capsule_dir: Path) -> CapsuleVerifyResult:
    capsule_dir = capsule_dir.resolve()
    manifest_path = capsule_dir / "manifest.json"
    checksums_path = capsule_dir / "checksums.txt"
    errors: list[str] = []
    warnings: list[str] = []
    capsule_id: str | None = None
    checked_files = 0

    if not manifest_path.exists():
        errors.append("manifest.json missing")
        return CapsuleVerifyResult(False, None, checked_files, warnings, errors)
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        capsule_id = str(manifest.get("capsule_id") or "")
    except json.JSONDecodeError as exc:
        errors.append(f"manifest.json is not valid JSON: {exc}")
        return CapsuleVerifyResult(False, None, checked_files, warnings, errors)

    if manifest.get("schema_version") != "107pilot.capsule.v1":
        errors.append("unsupported manifest schema_version")
    if manifest.get("capsule_type") != "raw":
        errors.append("unsupported capsule_type")

    for file_entry in manifest.get("files", []):
        logical_path = str(file_entry.get("logical_path", ""))
        try:
            path = _safe_join(capsule_dir, logical_path)
        except CapsuleError as exc:
            errors.append(str(exc))
            continue
        if not path.exists() or not path.is_file():
            errors.append(f"manifest file missing: {logical_path}")
            continue
        expected_sha = str(file_entry.get("sha256", ""))
        actual_sha = _sha256(path)
        if expected_sha != actual_sha:
            errors.append(f"manifest sha256 mismatch: {logical_path}")

    if not checksums_path.exists():
        errors.append("checksums.txt missing")
    else:
        for line_number, line in enumerate(
            checksums_path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if not line.strip():
                continue
            try:
                expected_sha, logical_path = line.split("  ", 1)
            except ValueError:
                errors.append(f"invalid checksum line {line_number}")
                continue
            try:
                path = _safe_join(capsule_dir, logical_path)
            except CapsuleError as exc:
                errors.append(str(exc))
                continue
            if not path.exists() or not path.is_file():
                errors.append(f"checksum file missing: {logical_path}")
                continue
            checked_files += 1
            if _sha256(path) != expected_sha:
                errors.append(f"checksum mismatch: {logical_path}")

    return CapsuleVerifyResult(
        valid=not errors,
        capsule_id=capsule_id or None,
        checked_files=checked_files,
        warnings=warnings,
        errors=errors,
    )


def _safe_join(root: Path, logical_path: str) -> Path:
    if not logical_path or logical_path.startswith("/") or ".." in Path(logical_path).parts:
        raise CapsuleError(f"unsafe capsule path: {logical_path!r}")
    root = root.resolve()
    path = (root / logical_path).resolve()
    if path != root and not path.is_relative_to(root):
        raise CapsuleError(f"capsule path escaped root: {logical_path!r}")
    return path


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _build_checksums(capsule_dir: Path) -> str:
    lines: list[str] = []
    for path in sorted(item for item in capsule_dir.rglob("*") if item.is_file()):
        if path.name == "checksums.txt":
            continue
        logical_path = path.relative_to(capsule_dir).as_posix()
        lines.append(f"{_sha256(path)}  {logical_path}")
    return "\n".join(lines) + ("\n" if lines else "")
