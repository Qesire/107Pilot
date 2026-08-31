"""Bind agent context to verified, bounded, and redacted Evidence objects."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from pilot107.agent.tasks import AgentTaskGateReceipt
from pilot107.core.run_store import EvidenceObjectRecord, RunStore, utc_now_iso
from pilot107.core.states import CollectionState, RunState

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
    ) -> None:
        if max_snippet_bytes <= 0:
            raise ValueError("max_snippet_bytes must be positive")
        if max_total_bytes <= 0:
            raise ValueError("max_total_bytes must be positive")
        self.store = store
        self.evidence_root = evidence_root.expanduser().resolve()
        self.max_snippet_bytes = max_snippet_bytes
        self.max_total_bytes = max_total_bytes

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
        """Re-read authoritative Run/Evidence facts and issue an immutable gate receipt.

        This method deliberately performs no orchestration.  It only verifies the
        already-persisted terminal Run, finalized object rows, and manifest.  A
        legacy workspace boundary is accepted only when it carries its real
        snapshot digest and explicitly has no live revision.
        """
        run = self.store.get_run(run_id)
        boundary = _workspace_boundary_values(workspace_boundary)

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
        effective_boundary = dict(boundary)
        for key in ("source_revision", "platform_snapshot_ref"):
            if effective_boundary[key] is None:
                effective_boundary[key] = manifest_boundary.get(key)
        _verify_provenance(
            objects=typed_objects,
            boundary=effective_boundary,
            manifest_boundary=manifest_boundary,
        )
        digest = _sha256_file(Path(manifest_obj.store_path).expanduser().resolve())
        verified_at = utc_now_iso()
        self.store.mark_evidence_integrity_checked(
            run_id,
            tuple(item.logical_path for item in typed_objects),
            checked_at=verified_at,
        )
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
        legacy = revision is None
    if not isinstance(legacy, bool) or (revision is None and not legacy) or (
        revision is not None and legacy
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
    normalized = value.strip().upper()
    return normalized in {"0", "0:0", "0:0:0"} or normalized.startswith("0:") and all(
        part in {"0", ""} for part in normalized.split(":")
    )


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
        if int(raw.get("size_bytes", -1)) != obj.size_bytes:
            raise EvidenceBindingError("manifest_size_mismatch")
        entries[ref] = raw
    if set(entries) != set(ref for ref in refs if ref != f"evidence://runs/{run_id}/manifest/manifest.json"):
        raise EvidenceBindingError("manifest_refs_incomplete")


def _verify_provenance(
    *,
    objects: list[EvidenceObjectRecord],
    boundary: dict[str, Any],
    manifest_boundary: dict[str, Any],
) -> None:
    if manifest_boundary.get("workspace_digest") is None:
        raise EvidenceBindingError("manifest_workspace_digest_missing")
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
        if (
            obj.workspace_revision is not None
            and obj.workspace_revision != boundary["workspace_revision"]
        ):
            raise EvidenceBindingError("workspace_revision_mismatch")
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
