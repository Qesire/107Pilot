"""Bind agent context to verified, bounded, and redacted Evidence objects."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from pilot107.agent.tasks import AgentTaskGateReceipt
from pilot107.core.run_store import (
    EvidenceObjectRecord,
    EvidenceSealClaimConflict,
    EvidenceSealFenceConflict,
    EvidenceSealRecord,
    RunStore,
    utc_now_iso,
)
from pilot107.core.states import CollectionState, EvidenceSealState, RunState

_SENSITIVE_ASSIGNMENT = re.compile(
    r"(?im)^(?P<prefix>[A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|PASSWD|API_KEY|PRIVATE_KEY)"
    r"[A-Z0-9_]*\s*=\s*).*$"
)
_SENSITIVE_JSON_VALUE = re.compile(
    r'(?i)(?P<prefix>"[^"\n]*(?:token|secret|password|passwd|api[_-]?key|private[_-]?key)'
    r'[^"\n]*"\s*:\s*)"[^"\n]*"'
)
_BEARER_TOKEN = re.compile(r"(?i)(?P<prefix>\bBearer\s+)[A-Za-z0-9._~+/=-]+")


@dataclass(frozen=True)
class BoundEvidence:
    object_id: str
    evidence_ref: str
    logical_path: str
    sha256: str
    mime_type: str
    trust: str
    snippet: str
    truncated: bool
    redactions: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, Any]:
        return {
            "object_id": self.object_id,
            "evidence_ref": self.evidence_ref,
            "logical_path": self.logical_path,
            "sha256": self.sha256,
            "mime_type": self.mime_type,
            "trust": self.trust,
            "snippet": self.snippet,
            "truncated": self.truncated,
            "redactions": list(self.redactions),
        }


@dataclass(frozen=True)
class EvidenceBundle:
    run_id: str
    objects: tuple[BoundEvidence, ...]
    rejected_refs: tuple[str, ...]
    warnings: tuple[str, ...]
    sha256: str

    def by_ref(self) -> dict[str, BoundEvidence]:
        return {obj.evidence_ref: obj for obj in self.objects}

    def to_payload(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "objects": [obj.to_payload() for obj in self.objects],
            "rejected_refs": list(self.rejected_refs),
            "warnings": list(self.warnings),
            "sha256": self.sha256,
        }


class EvidenceBinder:
    """Resolve Evidence references without trusting database paths or file contents."""

    def __init__(
        self,
        *,
        store: RunStore,
        evidence_root: Path,
        max_snippet_bytes: int = 8192,
        max_total_bytes: int = 32768,
        seal_lease_seconds: int = 300,
    ) -> None:
        if max_snippet_bytes <= 0:
            raise ValueError("max_snippet_bytes must be positive")
        if max_total_bytes <= 0:
            raise ValueError("max_total_bytes must be positive")
        if seal_lease_seconds <= 0:
            raise ValueError("seal_lease_seconds must be positive")
        self.store = store
        self.evidence_root = evidence_root.expanduser().resolve()
        self.max_snippet_bytes = max_snippet_bytes
        self.max_total_bytes = max_total_bytes
        self.seal_lease_seconds = seal_lease_seconds

    def bind(self, run_id: str, evidence_refs: tuple[str, ...] | list[str]) -> EvidenceBundle:
        run = self.store.get_run(run_id)
        objects = self.store.list_evidence_objects(run_id)
        by_logical_path = {obj.logical_path: obj for obj in objects}
        accepted: list[BoundEvidence] = []
        rejected: list[str] = []
        warnings: list[str] = []
        remaining = self.max_total_bytes

        for evidence_ref in dict.fromkeys(str(ref).strip() for ref in evidence_refs):
            if not evidence_ref:
                continue
            logical_path = _logical_path_from_ref(evidence_ref, expected_run_id=run_id)
            obj = None if logical_path is None else by_logical_path.get(logical_path)
            if obj is None:
                rejected.append(evidence_ref)
                warnings.append(f"evidence_ref_not_registered:{evidence_ref}")
                continue
            try:
                bound = self._bind_object(
                    run_id=run_id,
                    owner=run.owner,
                    evidence_ref=evidence_ref,
                    obj=obj,
                    byte_limit=min(self.max_snippet_bytes, remaining),
                )
            except EvidenceBindingError as exc:
                rejected.append(evidence_ref)
                warnings.append(f"evidence_ref_rejected:{obj.object_id}:{exc.code}")
                continue
            accepted.append(bound)
            remaining -= len(bound.snippet.encode("utf-8"))
            if remaining <= 0:
                warnings.append("evidence_bundle_total_limit_reached")
                break

        digest_payload = [
            {
                "object_id": item.object_id,
                "sha256": item.sha256,
                "snippet_sha256": hashlib.sha256(item.snippet.encode("utf-8")).hexdigest(),
            }
            for item in accepted
        ]
        digest = hashlib.sha256(
            json.dumps(
                {"run_id": run_id, "objects": digest_payload},
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return EvidenceBundle(
            run_id=run_id,
            objects=tuple(accepted),
            rejected_refs=tuple(rejected),
            warnings=tuple(dict.fromkeys(warnings)),
            sha256=digest,
        )

    def verify_terminal_gate(
        self,
        run_id: str,
        evidence_refs: tuple[str, ...] | list[str],
        workspace_boundary: Mapping[str, Any] | Any,
        *,
        task_id: str | None = None,
    ) -> AgentTaskGateReceipt:
        """Issue a receipt only for a sealed, still-consistent Evidence tree."""

        seal = self.store.get_evidence_seal(run_id)
        if seal.state is not EvidenceSealState.SEALED:
            raise EvidenceBindingError(
                "evidence_seal_invalid"
                if seal.state is EvidenceSealState.INVALID
                else "evidence_not_sealed"
            )
        if seal.digest is None or seal.marker_ref is None:
            raise EvidenceBindingError("evidence_seal_metadata_missing")
        receipt = self._verify_terminal_gate_facts(
            run_id,
            evidence_refs,
            workspace_boundary,
            task_id=task_id,
        )
        marker_bytes = _seal_marker_bytes(
            run=self.store.get_run(run_id),
            objects=self.store.list_evidence_objects(run_id),
            object_set_digest=receipt.evidence_digest,
        )
        expected_ref = _seal_marker_ref(run_id)
        if seal.marker_ref != expected_ref:
            raise EvidenceBindingError("evidence_seal_marker_binding_mismatch")
        persisted = _read_seal_marker(self.evidence_root, run_id)
        if persisted != marker_bytes:
            raise EvidenceBindingError("evidence_seal_marker_mismatch")
        digest = hashlib.sha256(persisted).hexdigest()
        if digest != seal.digest:
            raise EvidenceBindingError("evidence_seal_digest_mismatch")
        return replace(
            receipt,
            seal_digest=digest,
            seal_marker_ref=seal.marker_ref,
        )

    def seal_terminal_evidence(
        self,
        run_id: str,
        evidence_refs: tuple[str, ...] | list[str],
        workspace_boundary: Mapping[str, Any] | Any,
        *,
        task_id: str | None = None,
    ) -> EvidenceSealRecord:
        """Recoverably publish one terminal Run's registered Evidence as read-only."""

        run = self.store.get_run(run_id)
        if run.state is not RunState.SUCCEEDED:
            raise EvidenceBindingError("run_not_succeeded")
        if run.collection_state is CollectionState.FAILED:
            raise EvidenceBindingError("evidence_unavailable")
        if run.collection_state is not CollectionState.SUCCEEDED:
            raise EvidenceBindingError("collection_incomplete")
        claim_owner = f"evidence-binder:{uuid.uuid4().hex}"
        try:
            current = self.store.begin_evidence_seal(
                run_id,
                claim_owner=claim_owner,
                lease_seconds=self.seal_lease_seconds,
            )
        except EvidenceSealClaimConflict as exc:
            raise EvidenceBindingError("evidence_seal_awaiting") from exc
        except ValueError as exc:
            raise EvidenceBindingError("evidence_seal_invalid") from exc
        if current.state is EvidenceSealState.SEALED:
            return current
        fencing_token = current.fencing_token

        def renew_claim() -> None:
            try:
                self.store.renew_evidence_seal_claim(
                    run_id,
                    claim_owner=claim_owner,
                    fencing_token=fencing_token,
                    lease_seconds=self.seal_lease_seconds,
                )
            except EvidenceSealFenceConflict as exc:
                raise EvidenceBindingError("evidence_seal_awaiting") from exc

        try:
            receipt = self._verify_terminal_gate_facts(
                run_id,
                evidence_refs,
                workspace_boundary,
                task_id=task_id,
            )
            renew_claim()
            run = self.store.get_run(run_id)
            objects = self.store.list_evidence_objects(run_id)
            paths = _validate_registered_tree(
                evidence_root=self.evidence_root,
                run_id=run_id,
                objects=objects,
            )
            renew_claim()
            marker_bytes = _seal_marker_bytes(
                run=run,
                objects=objects,
                object_set_digest=receipt.evidence_digest,
            )
            _write_seal_marker(self.evidence_root, run_id, marker_bytes)
            renew_claim()
            before = {path: _stable_file_fingerprint(str(path)) for path in paths}
            _make_evidence_tree_read_only(
                run_root=(self.evidence_root / "runs" / run_id),
                files=paths,
            )
            _make_marker_read_only(self.evidence_root, run_id)
            for path, fingerprint in before.items():
                if _stable_file_fingerprint(str(path)) != fingerprint:
                    raise EvidenceBindingError("evidence_file_changed_during_seal")
            renew_claim()
            digest = hashlib.sha256(marker_bytes).hexdigest()
            return self.store.complete_evidence_seal(
                run_id,
                claim_owner=claim_owner,
                fencing_token=fencing_token,
                digest=digest,
                marker_ref=_seal_marker_ref(run_id),
            )
        except EvidenceBindingError as exc:
            if exc.code == "evidence_seal_awaiting":
                raise
            try:
                self.store.invalidate_evidence_seal(
                    run_id,
                    claim_owner=claim_owner,
                    fencing_token=fencing_token,
                    reason=exc.code,
                )
            except EvidenceSealFenceConflict as fence_exc:
                raise EvidenceBindingError("evidence_seal_awaiting") from fence_exc
            raise
        except ValueError as exc:
            try:
                self.store.invalidate_evidence_seal(
                    run_id,
                    claim_owner=claim_owner,
                    fencing_token=fencing_token,
                    reason=str(exc),
                )
            except EvidenceSealFenceConflict as fence_exc:
                raise EvidenceBindingError("evidence_seal_awaiting") from fence_exc
            raise EvidenceBindingError("evidence_seal_invalid") from exc
        except EvidenceSealFenceConflict as exc:
            raise EvidenceBindingError("evidence_seal_awaiting") from exc

    def _verify_terminal_gate_facts(
        self,
        run_id: str,
        evidence_refs: tuple[str, ...] | list[str],
        workspace_boundary: Mapping[str, Any] | Any,
        *,
        task_id: str | None = None,
    ) -> AgentTaskGateReceipt:
        """Re-read authoritative Run/Evidence facts without claiming filesystem atomicity.

        This method deliberately performs no orchestration.  It only verifies the
        already-persisted terminal Run, finalized object rows, and manifest.  A
        legacy workspace boundary is accepted only when it carries its real
        snapshot digest and explicitly has no live revision.
        """
        run = self.store.get_run(run_id)
        if run.state is not RunState.SUCCEEDED:
            raise EvidenceBindingError("run_not_succeeded")
        if not _successful_exit_code(run.exit_code):
            raise EvidenceBindingError("run_exit_code_not_success")
        if not run.job_id:
            raise EvidenceBindingError("run_job_id_missing")
        if run.collection_state is CollectionState.FAILED:
            raise EvidenceBindingError("evidence_unavailable")
        if run.collection_state is not CollectionState.SUCCEEDED:
            raise EvidenceBindingError("collection_incomplete")

        supplied_boundary = _workspace_boundary_values(workspace_boundary)
        boundary = _run_provenance_boundary(run)
        for key in (
            "workspace_digest",
            "workspace_revision",
            "legacy_boundary",
            "source_revision",
            "platform_snapshot_ref",
        ):
            supplied_value = supplied_boundary.get(key)
            if supplied_value is not None and supplied_value != boundary[key]:
                raise EvidenceBindingError(f"caller_boundary_{key}_mismatch")

        refs = tuple(dict.fromkeys(str(ref).strip() for ref in evidence_refs if str(ref).strip()))
        if not refs:
            raise EvidenceBindingError("evidence_refs_missing")
        bundle = self.bind(run_id, refs)
        if bundle.rejected_refs:
            raise EvidenceBindingError("integrity_verification_failed")
        if len(bundle.objects) != len(refs):
            raise EvidenceBindingError("evidence_refs_incomplete")

        objects = {obj.logical_path: obj for obj in self.store.list_evidence_objects(run_id)}
        bound_objects = [objects.get(item.logical_path) for item in bundle.objects]
        if any(obj is None for obj in bound_objects):
            raise EvidenceBindingError("evidence_refs_incomplete")
        typed_objects = [obj for obj in bound_objects if obj is not None]
        if any(obj.collection_status != "collected" for obj in typed_objects):
            raise EvidenceBindingError("evidence_not_collected")
        if any(obj.finalized_at is None for obj in typed_objects):
            raise EvidenceBindingError("evidence_not_finalized")

        manifest_obj = objects.get("manifest/manifest.json")
        manifest_ref = f"evidence://runs/{run_id}/manifest/manifest.json"
        if manifest_obj is None or manifest_ref not in refs:
            raise EvidenceBindingError("manifest_missing")
        manifest_payload = _read_manifest(manifest_obj)
        if manifest_payload.get("run_id") != run_id:
            raise EvidenceBindingError("manifest_run_binding_mismatch")
        if manifest_payload.get("owner") != run.owner:
            raise EvidenceBindingError("manifest_owner_binding_mismatch")
        if str(manifest_payload.get("job_id") or "") != str(run.job_id):
            raise EvidenceBindingError("manifest_job_binding_mismatch")
        _verify_manifest_artifacts(
            run_id=run_id,
            manifest_payload=manifest_payload,
            objects=objects,
            refs=refs,
        )

        manifest_boundary = _workspace_boundary_values(manifest_payload, allow_missing=True)
        for key in ("workspace_digest", "workspace_revision", "legacy_boundary"):
            if manifest_boundary.get(key) is not None and manifest_boundary[key] != boundary[key]:
                raise EvidenceBindingError(f"workspace_boundary_{key}_mismatch")
        effective_boundary = dict(boundary)
        for key in ("workspace_digest", "workspace_revision", "legacy_boundary"):
            if manifest_boundary.get(key) is not None:
                effective_boundary[key] = manifest_boundary[key]
        for key in ("source_revision", "platform_snapshot_ref"):
            persisted_value = manifest_boundary.get(key)
            if persisted_value is None:
                raise EvidenceBindingError(f"{key}_binding_missing")
            if effective_boundary[key] is not None and effective_boundary[key] != persisted_value:
                raise EvidenceBindingError(f"{key}_binding_mismatch")
            effective_boundary[key] = persisted_value
        _verify_provenance(
            objects=typed_objects,
            boundary=effective_boundary,
            manifest_boundary=manifest_boundary,
        )
        typed_paths = tuple(item.logical_path for item in typed_objects)
        before_fingerprints = {
            path: _stable_file_fingerprint(objects[path].store_path) for path in typed_paths
        }
        verified_at = utc_now_iso()
        digest = self.store.mark_evidence_integrity_checked(
            run_id,
            typed_paths,
            checked_at=verified_at,
        )
        try:
            for path in typed_paths:
                if _stable_file_fingerprint(objects[path].store_path) != before_fingerprints[path]:
                    raise EvidenceBindingError("evidence_file_changed_after_integrity_freeze")
        except EvidenceBindingError:
            self.store.revoke_evidence_integrity(run_id, typed_paths)
            raise
        typed_path_set = set(typed_paths)
        persisted_checked_at = {
            obj.integrity_checked_at
            for obj in self.store.list_evidence_objects(run_id)
            if obj.logical_path in typed_path_set
        }
        if len(persisted_checked_at) != 1 or None in persisted_checked_at:
            raise EvidenceBindingError("integrity_timestamp_missing")
        persisted_timestamp = next(iter(persisted_checked_at))
        if persisted_timestamp is None:
            raise EvidenceBindingError("integrity_timestamp_missing")
        verified_at = persisted_timestamp
        return AgentTaskGateReceipt(
            task_id=task_id or run_id,
            run_id=run_id,
            run_terminal_state="completed",
            evidence_refs=refs,
            evidence_digest=digest,
            integrity_verified_at=verified_at,
            workspace_revision=effective_boundary["workspace_revision"],
            workspace_digest=effective_boundary["workspace_digest"],
            legacy_boundary=effective_boundary["legacy_boundary"],
            capsule_ref=None,
            capsule_state="not_required",
            platform_snapshot_ref=effective_boundary["platform_snapshot_ref"],
            source_revision=effective_boundary["source_revision"],
            terminal_at=run.updated_at,
        )

    def _bind_object(
        self,
        *,
        run_id: str,
        owner: str,
        evidence_ref: str,
        obj: EvidenceObjectRecord,
        byte_limit: int,
    ) -> BoundEvidence:
        if byte_limit <= 0:
            raise EvidenceBindingError("bundle_limit")
        if obj.collection_status != "collected":
            raise EvidenceBindingError("not_collected")
        expected_source_uri = f"evidence://runs/{run_id}/{obj.logical_path}"
        if obj.source_uri != expected_source_uri:
            raise EvidenceBindingError("source_uri_binding_mismatch")
        if not obj.sha256 or not re.fullmatch(r"[0-9a-fA-F]{64}", obj.sha256):
            raise EvidenceBindingError("missing_sha256")
        if obj.mutable_during_run and obj.finalized_at is None:
            raise EvidenceBindingError("mutable_not_finalized")
        if not _is_text_mime_type(obj.mime_type):
            raise EvidenceBindingError("unsupported_mime_type")

        path = Path(obj.store_path).expanduser()
        if path.is_symlink():
            raise EvidenceBindingError("symlink")
        try:
            resolved = path.resolve(strict=True)
        except OSError as exc:
            raise EvidenceBindingError("missing_file") from exc
        run_root = (self.evidence_root / "runs" / run_id).resolve()
        if resolved != run_root and not resolved.is_relative_to(run_root):
            raise EvidenceBindingError("outside_run_root")
        try:
            if not resolved.is_file():
                raise EvidenceBindingError("not_regular_file")
            file_size = resolved.stat().st_size
            if obj.size_bytes is not None and file_size != obj.size_bytes:
                raise EvidenceBindingError("size_mismatch")
            actual_sha256 = _sha256_file(resolved)
            if actual_sha256.lower() != obj.sha256.lower():
                raise EvidenceBindingError("sha256_mismatch")
            with resolved.open("rb") as handle:
                data = handle.read(byte_limit + 1)
        except EvidenceBindingError:
            raise
        except OSError as exc:
            raise EvidenceBindingError("unreadable_file") from exc
        truncated = len(data) > byte_limit or file_size > byte_limit
        text = data[:byte_limit].decode("utf-8", errors="replace")
        redacted, redactions = redact_evidence_text(text, owner=owner)
        return BoundEvidence(
            object_id=obj.object_id,
            evidence_ref=evidence_ref,
            logical_path=obj.logical_path,
            sha256=actual_sha256,
            mime_type=obj.mime_type or "text/plain",
            trust=_trust_for_object(obj),
            snippet=redacted,
            truncated=truncated,
            redactions=redactions,
        )


class EvidenceBindingError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _workspace_boundary_values(
    value: Mapping[str, Any] | Any, *, allow_missing: bool = False
) -> dict[str, Any]:
    def get(name: str, *aliases: str) -> Any:
        names = (name,) + aliases
        for candidate in names:
            if isinstance(value, Mapping) and candidate in value:
                return value[candidate]
            if hasattr(value, candidate):
                return getattr(value, candidate)
        return None

    digest = get("workspace_digest", "live_digest", "snapshot_digest")
    revision = get("workspace_revision", "revision")
    legacy = get("legacy_boundary")
    source = get("source_revision")
    platform = get("platform_snapshot_ref", "platform_snapshot_id")
    if digest is None:
        if allow_missing:
            return {
                "workspace_digest": None,
                "workspace_revision": revision,
                "legacy_boundary": legacy,
                "source_revision": source,
                "platform_snapshot_ref": platform,
            }
        raise EvidenceBindingError("workspace_boundary_missing")
    digest = str(digest)
    if not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
        raise EvidenceBindingError("workspace_digest_invalid")
    if revision is not None:
        try:
            revision = int(revision)
        except (TypeError, ValueError) as exc:
            raise EvidenceBindingError("workspace_revision_invalid") from exc
        if revision < 0:
            raise EvidenceBindingError("workspace_revision_invalid")
    if legacy is None:
        raise EvidenceBindingError("workspace_boundary_legacy_marker_missing")
    if (
        not isinstance(legacy, bool)
        or (revision is None and not legacy)
        or (revision is not None and legacy)
    ):
        raise EvidenceBindingError("workspace_boundary_invalid")
    return {
        "workspace_digest": digest.lower(),
        "workspace_revision": revision,
        "legacy_boundary": legacy,
        "source_revision": None if source is None else str(source),
        "platform_snapshot_ref": None if platform is None else str(platform),
    }


def _successful_exit_code(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip() == "0:0"


def _stable_file_fingerprint(path_value: str) -> tuple[int, int, int, str]:
    try:
        path = Path(path_value).expanduser().resolve(strict=True)
        before = path.lstat()
        digest = _sha256_file(path)
        after = path.lstat()
    except OSError as exc:
        raise EvidenceBindingError("evidence_file_unreadable") from exc
    before_identity = (before.st_ino, before.st_size, before.st_mtime_ns)
    after_identity = (after.st_ino, after.st_size, after.st_mtime_ns)
    if before_identity != after_identity:
        raise EvidenceBindingError("evidence_file_changed_during_integrity_check")
    return (*after_identity, digest)


def _run_provenance_boundary(run: Any) -> dict[str, Any]:
    digest = run.workspace_digest
    if digest is None:
        raise EvidenceBindingError("run_provenance_missing")
    digest = str(digest).lower()
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise EvidenceBindingError("run_workspace_digest_invalid")
    if run.workspace_revision is not None:
        raise EvidenceBindingError("live_workspace_revision_unsupported")
    if not isinstance(run.source_revision, str) or not run.source_revision:
        raise EvidenceBindingError("run_source_revision_missing")
    if not isinstance(run.platform_snapshot_ref, str) or not run.platform_snapshot_ref:
        raise EvidenceBindingError("run_platform_snapshot_missing")
    return {
        "workspace_digest": digest,
        "workspace_revision": None,
        "legacy_boundary": True,
        "source_revision": run.source_revision,
        "platform_snapshot_ref": run.platform_snapshot_ref,
    }


def _read_manifest(obj: EvidenceObjectRecord) -> dict[str, Any]:
    try:
        payload = json.loads(
            Path(obj.store_path).expanduser().resolve().read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise EvidenceBindingError("manifest_unreadable") from exc
    if not isinstance(payload, dict) or payload.get("schema") != "pilot107.evidence_manifest.v1":
        raise EvidenceBindingError("manifest_invalid")
    return payload


def _verify_manifest_artifacts(
    *,
    run_id: str,
    manifest_payload: dict[str, Any],
    objects: dict[str, EvidenceObjectRecord],
    refs: tuple[str, ...],
) -> None:
    raw_artifacts = manifest_payload.get("artifacts")
    if not isinstance(raw_artifacts, list):
        raise EvidenceBindingError("manifest_artifacts_invalid")
    entries: dict[str, dict[str, Any]] = {}
    for raw in raw_artifacts:
        if not isinstance(raw, dict):
            raise EvidenceBindingError("manifest_artifacts_invalid")
        logical_path = str(raw.get("logical_path") or "")
        ref = str(raw.get("evidence_ref") or "")
        if not logical_path or ref != f"evidence://runs/{run_id}/{logical_path}":
            raise EvidenceBindingError("manifest_ref_binding_mismatch")
        if logical_path in entries:
            raise EvidenceBindingError("manifest_duplicate_artifact")
        obj = objects.get(logical_path)
        if obj is None or obj.sha256 is None or obj.size_bytes is None:
            raise EvidenceBindingError("manifest_object_missing")
        if str(raw.get("sha256") or "").lower() != obj.sha256.lower():
            raise EvidenceBindingError("manifest_sha256_mismatch")
        raw_size = raw.get("size_bytes")
        if isinstance(raw_size, bool) or not isinstance(raw_size, int):
            raise EvidenceBindingError("manifest_size_mismatch")
        manifest_size = raw_size
        if manifest_size != obj.size_bytes:
            raise EvidenceBindingError("manifest_size_mismatch")
        entries[ref] = raw
    expected_refs = set(
        ref for ref in refs if ref != f"evidence://runs/{run_id}/manifest/manifest.json"
    )
    registered_objects = [
        obj for logical_path, obj in objects.items() if logical_path != "manifest/manifest.json"
    ]
    if any(
        obj.collection_status != "collected" or obj.finalized_at is None
        for obj in registered_objects
    ):
        raise EvidenceBindingError("registered_evidence_incomplete")
    registered_refs = {
        f"evidence://runs/{run_id}/{logical_path}"
        for logical_path, obj in objects.items()
        if logical_path != "manifest/manifest.json"
    }
    if set(entries) != expected_refs or set(entries) != registered_refs:
        raise EvidenceBindingError("manifest_refs_incomplete")


def _verify_provenance(
    *,
    objects: list[EvidenceObjectRecord],
    boundary: dict[str, Any],
    manifest_boundary: dict[str, Any],
) -> None:
    if manifest_boundary.get("workspace_digest") is None:
        raise EvidenceBindingError("manifest_workspace_digest_missing")
    for key, code in (
        ("source_revision", "source_binding_missing"),
        ("platform_snapshot_ref", "platform_binding_missing"),
    ):
        if boundary[key] is None:
            raise EvidenceBindingError(code)
    for key in (
        "workspace_digest",
        "workspace_revision",
        "legacy_boundary",
        "source_revision",
        "platform_snapshot_ref",
    ):
        expected = boundary[key]
        manifest_value = manifest_boundary.get(key)
        if manifest_value is not None and manifest_value != expected:
            raise EvidenceBindingError(f"manifest_{key}_mismatch")
    for obj in objects:
        if obj.workspace_digest is None:
            raise EvidenceBindingError("workspace_binding_missing")
        if obj.workspace_digest.lower() != boundary["workspace_digest"]:
            raise EvidenceBindingError("workspace_binding_mismatch")
        if boundary["workspace_revision"] is not None and obj.workspace_revision is None:
            raise EvidenceBindingError("workspace_revision_missing")
        if (
            obj.workspace_revision is not None
            and obj.workspace_revision != boundary["workspace_revision"]
        ):
            raise EvidenceBindingError("workspace_revision_mismatch")
        if obj.source_revision is None:
            raise EvidenceBindingError("source_binding_missing")
        if obj.platform_snapshot_ref is None:
            raise EvidenceBindingError("platform_binding_missing")
        for attr, key, code in (
            ("source_revision", "source_revision", "source_binding_mismatch"),
            ("platform_snapshot_ref", "platform_snapshot_ref", "platform_binding_mismatch"),
        ):
            value = getattr(obj, attr)
            expected = boundary[key]
            if value is not None and expected is not None and value != expected:
                raise EvidenceBindingError(code)


def redact_evidence_text(value: str, *, owner: str) -> tuple[str, tuple[str, ...]]:
    redacted = value
    reports: list[str] = []

    redacted, count = _SENSITIVE_ASSIGNMENT.subn(r"\g<prefix><redacted>", redacted)
    if count:
        reports.append("sensitive_assignment")
    redacted, count = _SENSITIVE_JSON_VALUE.subn(r'\g<prefix>"<redacted>"', redacted)
    if count:
        reports.append("sensitive_json_value")
    redacted, count = _BEARER_TOKEN.subn(r"\g<prefix><redacted>", redacted)
    if count:
        reports.append("bearer_token")
    if owner:
        home = f"/public/home/{owner}"
        if home in redacted:
            redacted = redacted.replace(home, "<home>")
            reports.append("owner_home")
    return redacted, tuple(reports)


def _logical_path_from_ref(evidence_ref: str, *, expected_run_id: str) -> str | None:
    parsed = urlparse(evidence_ref)
    if parsed.scheme != "evidence" or parsed.netloc != "runs" or parsed.query or parsed.fragment:
        return None
    parts = [unquote(part) for part in parsed.path.split("/") if part]
    if len(parts) < 2 or parts[0] != expected_run_id:
        return None
    logical_parts = parts[1:]
    if any(part in {".", ".."} or not part for part in logical_parts):
        return None
    return "/".join(logical_parts)


def _is_text_mime_type(mime_type: str | None) -> bool:
    if mime_type is None:
        return False
    return mime_type.startswith("text/") or mime_type in {
        "application/json",
        "application/x-jsonlines",
    }


def _trust_for_object(obj: EvidenceObjectRecord) -> str:
    if obj.logical_path.startswith("slurm/"):
        return "trusted_scheduler_metadata"
    if obj.logical_path.startswith("manifest/"):
        return "trusted_evidence_manifest"
    if obj.logical_path == "submission/slurm_submit_response.json":
        return "trusted_scheduler_metadata"
    return "untrusted_run_content"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _seal_marker_ref(run_id: str) -> str:
    return f"evidence-seal://runs/{run_id}/seal.json"


def _seal_marker_bytes(
    *,
    run: Any,
    objects: list[EvidenceObjectRecord],
    object_set_digest: str,
) -> bytes:
    payload = {
        "schema": "pilot107.evidence_seal.v1",
        "run_id": run.run_id,
        "object_set_digest": object_set_digest,
        "objects": [
            {
                "logical_path": obj.logical_path,
                "sha256": obj.sha256,
                "size_bytes": obj.size_bytes,
            }
            for obj in sorted(objects, key=lambda item: item.logical_path)
        ],
        "provenance": {
            "workspace_revision": run.workspace_revision,
            "workspace_digest": run.workspace_digest,
            "source_revision": run.source_revision,
            "platform_snapshot_ref": run.platform_snapshot_ref,
        },
    }
    return (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _validate_registered_tree(
    *,
    evidence_root: Path,
    run_id: str,
    objects: list[EvidenceObjectRecord],
) -> tuple[Path, ...]:
    run_root_path = evidence_root / "runs" / run_id
    if run_root_path.is_symlink():
        raise EvidenceBindingError("evidence_run_root_symlink")
    try:
        run_root = run_root_path.resolve(strict=True)
    except OSError as exc:
        raise EvidenceBindingError("evidence_run_root_missing") from exc
    if not run_root.is_dir():
        raise EvidenceBindingError("evidence_run_root_invalid")
    expected: dict[Path, EvidenceObjectRecord] = {}
    for obj in objects:
        logical = Path(obj.logical_path)
        if logical.is_absolute() or ".." in logical.parts:
            raise EvidenceBindingError("evidence_logical_path_invalid")
        path = run_root / logical
        if path.is_symlink():
            raise EvidenceBindingError("symlink")
        try:
            resolved = path.resolve(strict=True)
            metadata = path.lstat()
        except OSError as exc:
            raise EvidenceBindingError("missing_file") from exc
        if not resolved.is_relative_to(run_root):
            raise EvidenceBindingError("outside_run_root")
        if Path(obj.store_path).expanduser().resolve(strict=True) != resolved:
            raise EvidenceBindingError("store_path_binding_mismatch")
        if not stat.S_ISREG(metadata.st_mode):
            raise EvidenceBindingError("not_regular_file")
        if obj.size_bytes is None or metadata.st_size != obj.size_bytes:
            raise EvidenceBindingError("size_mismatch")
        if obj.sha256 is None or _sha256_file(resolved).lower() != obj.sha256.lower():
            raise EvidenceBindingError("sha256_mismatch")
        expected[resolved] = obj
    if not expected:
        raise EvidenceBindingError("evidence_refs_missing")
    discovered: set[Path] = set()
    for path in run_root.rglob("*"):
        if path.is_symlink():
            raise EvidenceBindingError("symlink")
        metadata = path.lstat()
        if stat.S_ISDIR(metadata.st_mode):
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise EvidenceBindingError("special_file")
        discovered.add(path.resolve(strict=True))
    if discovered != set(expected):
        raise EvidenceBindingError("evidence_registered_tree_mismatch")
    return tuple(sorted(expected, key=lambda item: item.as_posix()))


@contextmanager
def _open_seal_directory(
    evidence_root: Path,
    run_id: str,
    *,
    create: bool,
) -> Iterator[tuple[int, int]]:
    """Open the managed marker directory without following any child symlink.

    The deployment target is Linux; ``O_NOFOLLOW`` and directory-relative APIs
    are deliberately required rather than silently weakening this boundary.
    """

    if re.fullmatch(r"[A-Za-z0-9_.:-]+", run_id) is None:
        raise EvidenceBindingError("evidence_seal_run_id_invalid")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    root_fd: int | None = None
    seals_fd: int | None = None
    run_fd: int | None = None
    try:
        root_fd = os.open(evidence_root, flags)
        seals_fd = _open_directory_component(root_fd, "seals", flags=flags, create=create)
        run_fd = _open_directory_component(seals_fd, run_id, flags=flags, create=create)
        yield run_fd, seals_fd
    except EvidenceBindingError:
        raise
    except OSError as exc:
        raise EvidenceBindingError("evidence_seal_marker_directory_invalid") from exc
    finally:
        for descriptor in (run_fd, seals_fd, root_fd):
            if descriptor is not None:
                os.close(descriptor)


def _open_directory_component(
    parent_fd: int,
    name: str,
    *,
    flags: int,
    create: bool,
) -> int:
    created = False
    if create:
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent_fd)
            created = True
        except FileExistsError:
            created = False
    descriptor = os.open(name, flags, dir_fd=parent_fd)
    try:
        if created:
            os.fsync(parent_fd)
    except BaseException:
        with suppress(OSError):
            os.close(descriptor)
        raise
    return descriptor


def _read_file_at(directory_fd: int, name: str) -> bytes:
    descriptor: int | None = None
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=directory_fd,
        )
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise EvidenceBindingError("evidence_seal_marker_not_regular")
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 64 * 1024):
            chunks.append(chunk)
        return b"".join(chunks)
    except EvidenceBindingError:
        raise
    except OSError as exc:
        raise EvidenceBindingError("evidence_seal_marker_unreadable") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _read_seal_marker(evidence_root: Path, run_id: str) -> bytes:
    with _open_seal_directory(evidence_root, run_id, create=False) as (run_fd, _):
        return _read_file_at(run_fd, "seal.json")


def _write_seal_marker(evidence_root: Path, run_id: str, content: bytes) -> None:
    with _open_seal_directory(evidence_root, run_id, create=True) as (run_fd, _):
        try:
            existing = _read_file_at(run_fd, "seal.json")
        except EvidenceBindingError as exc:
            if exc.code != "evidence_seal_marker_unreadable" or not _missing_marker_at(run_fd):
                raise
        else:
            if existing != content:
                raise EvidenceBindingError("evidence_seal_marker_mismatch")
            return

        temporary_name = f".seal-{uuid.uuid4().hex}.tmp"
        descriptor: int | None = None
        try:
            descriptor = os.open(
                temporary_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=run_fd,
            )
            view = memoryview(content)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("short Evidence seal marker write")
                view = view[written:]
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            os.replace(
                temporary_name,
                "seal.json",
                src_dir_fd=run_fd,
                dst_dir_fd=run_fd,
            )
            if _read_file_at(run_fd, "seal.json") != content:
                raise EvidenceBindingError("evidence_seal_marker_mismatch")
            os.fsync(run_fd)
        except EvidenceBindingError:
            raise
        except OSError as exc:
            raise EvidenceBindingError("evidence_seal_marker_write_failed") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
            with suppress(FileNotFoundError):
                os.unlink(temporary_name, dir_fd=run_fd)


def _missing_marker_at(directory_fd: int) -> bool:
    try:
        os.stat("seal.json", dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return True
    return False


def _make_evidence_tree_read_only(*, run_root: Path, files: tuple[Path, ...]) -> None:
    for path in files:
        path.chmod(0o444)
    directories = [path for path in run_root.rglob("*") if path.is_dir()]
    directories.append(run_root)
    for directory in sorted(directories, key=lambda item: len(item.parts), reverse=True):
        directory.chmod(0o555)


def _make_marker_read_only(evidence_root: Path, run_id: str) -> None:
    with _open_seal_directory(evidence_root, run_id, create=False) as (run_fd, seals_fd):
        marker_fd: int | None = None
        try:
            marker_fd = os.open(
                "seal.json",
                os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=run_fd,
            )
            if not stat.S_ISREG(os.fstat(marker_fd).st_mode):
                raise EvidenceBindingError("evidence_seal_marker_not_regular")
            os.fchmod(marker_fd, 0o444)
            os.fsync(marker_fd)
        except EvidenceBindingError:
            raise
        except OSError as exc:
            raise EvidenceBindingError("evidence_seal_marker_unreadable") from exc
        finally:
            if marker_fd is not None:
                os.close(marker_fd)
        os.fchmod(run_fd, 0o555)
        os.fsync(run_fd)
        os.fsync(seals_fd)
