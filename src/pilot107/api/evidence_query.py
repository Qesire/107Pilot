"""Evidence query read model for the future HTTP API."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pilot107.core.run_store import RunStore
from pilot107.worker.evidence import EvidenceStore

DEFAULT_PREVIEW_MAX_BYTES = 128 * 1024


class EvidencePreviewUnavailable(RuntimeError):
    """Raised when a registered Evidence object cannot be read safely."""


@dataclass(frozen=True)
class EvidenceTreeNode:
    name: str
    kind: str
    logical_path: str
    size_bytes: int | None = None
    sha256: str | None = None
    content_type: str | None = None
    children: list[EvidenceTreeNode] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": self.name,
            "kind": self.kind,
            "logical_path": self.logical_path,
        }
        if self.size_bytes is not None:
            payload["size_bytes"] = self.size_bytes
        if self.sha256 is not None:
            payload["sha256"] = self.sha256
        if self.content_type is not None:
            payload["content_type"] = self.content_type
        if self.children:
            payload["children"] = [child.to_dict() for child in self.children]
        return payload


class EvidenceQueryService:
    def __init__(self, *, store: RunStore, evidence_store: EvidenceStore) -> None:
        self.store = store
        self.evidence_store = evidence_store

    def get_evidence_tree(self, run_id: str) -> dict[str, Any]:
        run = self.store.get_run(run_id)
        tasks = self.store.list_collection_tasks(run_id)
        run_root = self.evidence_store.run_root(run_id)
        root_node = self._scan_run_root(run_root)
        return {
            "run_id": run.run_id,
            "owner": run.owner,
            "job_id": run.job_id,
            "run_state": run.state.value,
            "collection_state": run.collection_state.value,
            "tasks": [
                {
                    "task_id": int(task["task_id"]),
                    "task_type": str(task["task_type"]),
                    "state": str(task["state"]),
                    "attempts": int(task["attempts"]),
                    "updated_at": str(task["updated_at"]),
                }
                for task in tasks
            ],
            "objects": [
                {
                    "object_id": obj.object_id,
                    "category": obj.category,
                    "logical_path": obj.logical_path,
                    "source_uri": obj.source_uri,
                    "sha256": obj.sha256,
                    "size_bytes": obj.size_bytes,
                    "mime_type": obj.mime_type,
                    "collection_status": obj.collection_status,
                    "mutable_during_run": obj.mutable_during_run,
                    "finalized_at": obj.finalized_at,
                }
                for obj in self.store.list_evidence_objects(run_id)
            ],
            "tree": root_node.to_dict(),
        }

    def get_object_preview(
        self,
        run_id: str,
        object_id: str,
        *,
        max_bytes: int = DEFAULT_PREVIEW_MAX_BYTES,
    ) -> dict[str, Any]:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        selected = next(
            (obj for obj in self.store.list_evidence_objects(run_id) if obj.object_id == object_id),
            None,
        )
        if selected is None:
            raise KeyError(object_id)

        run_root = self.evidence_store.run_root(run_id).resolve()
        logical = Path(selected.logical_path)
        if logical.is_absolute() or ".." in logical.parts:
            raise EvidencePreviewUnavailable("Evidence logical path is invalid")
        path = (run_root / logical).resolve()
        if path == run_root or not path.is_relative_to(run_root):
            raise EvidencePreviewUnavailable("Evidence object escaped its run root")
        if Path(selected.store_path).resolve() != path:
            raise EvidencePreviewUnavailable("Evidence object binding is inconsistent")
        if not path.is_file():
            raise EvidencePreviewUnavailable("Evidence object content is missing")

        metadata = {
            "object_id": selected.object_id,
            "category": selected.category,
            "logical_path": selected.logical_path,
            "source_uri": selected.source_uri,
            "sha256": selected.sha256,
            "size_bytes": selected.size_bytes,
            "mime_type": selected.mime_type,
            "collection_status": selected.collection_status,
            "mutable_during_run": selected.mutable_during_run,
            "finalized_at": selected.finalized_at,
        }
        if not _previewable(selected.mime_type, selected.logical_path):
            return {
                **metadata,
                "preview": {
                    "available": False,
                    "reason": "binary_or_unsupported_content",
                    "max_bytes": max_bytes,
                },
            }

        with path.open("rb") as handle:
            data = handle.read(max_bytes + 1)
        truncated = len(data) > max_bytes
        visible = data[:max_bytes]
        integrity = "not_checked"
        if not truncated and selected.sha256:
            integrity = (
                "verified" if hashlib.sha256(visible).hexdigest() == selected.sha256 else "mismatch"
            )
        return {
            **metadata,
            "preview": {
                "available": True,
                "content": visible.decode("utf-8", errors="replace"),
                "encoding": "utf-8",
                "bytes_read": len(visible),
                "max_bytes": max_bytes,
                "truncated": truncated,
                "integrity": integrity,
            },
        }

    def _scan_run_root(self, run_root: Path) -> EvidenceTreeNode:
        if not run_root.exists():
            return EvidenceTreeNode(name=run_root.name, kind="directory", logical_path="")
        return self._scan_node(run_root, run_root)

    def _scan_node(self, run_root: Path, path: Path) -> EvidenceTreeNode:
        relative = "" if path == run_root else path.relative_to(run_root).as_posix()
        if path.is_dir():
            children = [self._scan_node(run_root, child) for child in sorted(path.iterdir())]
            return EvidenceTreeNode(
                name=path.name,
                kind="directory",
                logical_path=relative,
                children=children,
            )
        data = path.read_bytes()
        return EvidenceTreeNode(
            name=path.name,
            kind="file",
            logical_path=relative,
            size_bytes=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            content_type=_content_type(path),
        )


def _content_type(path: Path) -> str:
    if path.suffix == ".json":
        return "application/json"
    if path.suffix == ".sh":
        return "text/x-shellscript"
    if path.suffix == ".txt":
        return "text/plain"
    return "application/octet-stream"


def _previewable(mime_type: str | None, logical_path: str) -> bool:
    normalized = (mime_type or "").lower()
    if normalized.startswith("text/"):
        return True
    if normalized in {"application/json", "application/jsonl", "application/x-ndjson"}:
        return True
    return Path(logical_path).suffix.lower() in {
        ".json",
        ".jsonl",
        ".log",
        ".sbatch",
        ".sh",
        ".txt",
    }
