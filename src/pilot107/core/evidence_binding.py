"""Bind agent context to verified, bounded, and redacted Evidence objects."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from pilot107.core.run_store import EvidenceObjectRecord, RunStore

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
