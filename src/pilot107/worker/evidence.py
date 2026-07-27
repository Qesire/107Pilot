"""Evidence collection primitives for Phase 0A."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import posixpath
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from pilot107.adapters.slurm import CommandResult, SimulatorExecutor, SlurmTransportError
from pilot107.core.contract_v2 import parse_expected_output
from pilot107.core.contracts import ContractStore
from pilot107.core.identity import UserIdentity
from pilot107.core.path_policy import OwnerRootPolicyError, resolve_owner_roots
from pilot107.core.paths import PathPolicyError, SafePath, authorize_path, reject_special_file
from pilot107.core.run_store import RunRecord, RunStore, utc_now_iso


@dataclass(frozen=True)
class EvidenceArtifact:
    logical_path: str
    path: Path
    size_bytes: int
    sha256: str
    content_type: str


@dataclass(frozen=True)
class EvidenceCollectionResult:
    run_id: str
    task_type: str
    artifacts: list[EvidenceArtifact] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class EvidencePolicy:
    max_depth: int = 3
    max_files: int = 1000
    max_single_read_bytes: int = 10 * 1024 * 1024
    max_total_inventory_bytes: int = 100 * 1024 * 1024
    excluded_patterns: tuple[str, ...] = (
        "slurm-*.out",
        "slurm-*.err",
        "pilot107-submit-*.sbatch",
    )


@dataclass(frozen=True)
class EvidenceCapability:
    transport: str
    can_stat: bool
    can_tail: bool
    can_inventory: bool
    can_copy_selected: bool
    authorized_roots: tuple[str, ...]
    max_single_read_bytes: int
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class EvidenceRoot:
    run_id: str
    path: SafePath


@dataclass(frozen=True)
class FileStat:
    path: str
    kind: str
    size_bytes: int
    mtime_epoch: float
    owner_readable: bool


@dataclass(frozen=True)
class TextTail:
    path: str
    max_bytes: int
    tail: str
    bytes_read: int
    truncated: bool
    sha256: str


@dataclass(frozen=True)
class InventoryFile:
    path: str
    relative_path: str
    size_bytes: int
    mtime_epoch: float
    sha256: str | None


@dataclass(frozen=True)
class OutputInventory:
    root: str
    files: list[InventoryFile]
    skipped: list[str] = field(default_factory=list)
    total_size_bytes: int = 0


class EvidenceTransport(Protocol):
    def probe(self, identity: UserIdentity) -> EvidenceCapability:
        """Return transport capabilities visible to a user identity."""

    def prepare_run_root(
        self,
        identity: UserIdentity,
        run_id: str,
        policy: EvidencePolicy,
    ) -> EvidenceRoot:
        """Prepare and authorize a run evidence root."""

    def stat(self, identity: UserIdentity, path: SafePath) -> FileStat:
        """Read metadata for an authorized path."""

    def read_text_tail(self, identity: UserIdentity, path: SafePath, max_bytes: int) -> TextTail:
        """Read the UTF-8 tail of an authorized regular file."""

    def read_bytes_range(
        self,
        identity: UserIdentity,
        path: SafePath,
        offset: int,
        length: int,
    ) -> bytes:
        """Read a bounded byte range from an authorized regular file."""

    def inventory(
        self,
        identity: UserIdentity,
        root: SafePath,
        policy: EvidencePolicy,
    ) -> OutputInventory:
        """Inventory authorized output files under a root."""


class CollectionTaskHandler(Protocol):
    def collect(self, *, run: RunRecord, task_type: str) -> EvidenceCollectionResult:
        """Execute one collection task for a run."""


class AuthorizedFilesystemEvidenceTransport:
    """Evidence transport for service-side reads from explicitly authorized roots."""

    def __init__(
        self,
        *,
        allowed_roots: list[str | Path],
        run_root_base: str | Path | None = None,
        max_single_read_bytes: int = 10 * 1024 * 1024,
    ) -> None:
        self._allowed_root_paths = [
            Path(root).expanduser().resolve(strict=True) for root in allowed_roots
        ]
        self.allowed_roots: list[str | Path] = list(self._allowed_root_paths)
        self.run_root_base = (
            self._allowed_root_paths[0]
            if run_root_base is None
            else authorize_path(str(run_root_base), self.allowed_roots).resolved
        )
        self.max_single_read_bytes = max_single_read_bytes

    def probe(self, identity: UserIdentity) -> EvidenceCapability:
        return EvidenceCapability(
            transport="authorized_filesystem",
            can_stat=True,
            can_tail=True,
            can_inventory=True,
            can_copy_selected=False,
            authorized_roots=tuple(str(root) for root in self._allowed_root_paths),
            max_single_read_bytes=self.max_single_read_bytes,
            notes=(f"identity={identity.username}",),
        )

    def prepare_run_root(
        self,
        identity: UserIdentity,
        run_id: str,
        policy: EvidencePolicy,
    ) -> EvidenceRoot:
        if not re.fullmatch(r"[A-Za-z0-9_.:-]+", run_id):
            raise ValueError(f"unsafe run_id: {run_id!r}")
        root = self.run_root_base / ".107pilot" / "runs" / run_id
        root.mkdir(parents=True, exist_ok=True)
        return EvidenceRoot(
            run_id=run_id,
            path=authorize_path(str(root), self.allowed_roots),
        )

    def stat(self, identity: UserIdentity, path: SafePath) -> FileStat:
        safe = self._authorize_safe_path(path)
        reject_special_file(safe.resolved)
        stat_result = safe.resolved.stat()
        return FileStat(
            path=str(safe.resolved),
            kind=_filesystem_kind(safe.resolved),
            size_bytes=stat_result.st_size,
            mtime_epoch=stat_result.st_mtime,
            owner_readable=safe.resolved.is_file() and bool(stat_result.st_mode & 0o400),
        )

    def read_text_tail(self, identity: UserIdentity, path: SafePath, max_bytes: int) -> TextTail:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        safe = self._authorize_safe_path(path)
        file_size = safe.resolved.stat().st_size
        length = min(max_bytes, self.max_single_read_bytes)
        data = self.read_bytes_range(
            identity,
            safe,
            offset=max(0, file_size - length),
            length=length,
        )
        full_sha = hashlib.sha256(safe.resolved.read_bytes()).hexdigest()
        return TextTail(
            path=str(safe.resolved),
            max_bytes=max_bytes,
            tail=data.decode("utf-8", errors="replace"),
            bytes_read=len(data),
            truncated=file_size > length,
            sha256=full_sha,
        )

    def read_bytes_range(
        self,
        identity: UserIdentity,
        path: SafePath,
        offset: int,
        length: int,
    ) -> bytes:
        if offset < 0:
            raise ValueError("offset must be non-negative")
        if length <= 0:
            raise ValueError("length must be positive")
        if length > self.max_single_read_bytes:
            raise ValueError("length exceeds max_single_read_bytes")
        safe = self._authorize_safe_path(path)
        reject_special_file(safe.resolved)
        if not safe.resolved.is_file():
            raise ValueError(f"path is not a regular file: {safe.resolved}")
        with safe.resolved.open("rb") as handle:
            handle.seek(offset)
            return handle.read(length)

    def inventory(
        self,
        identity: UserIdentity,
        root: SafePath,
        policy: EvidencePolicy,
    ) -> OutputInventory:
        safe_root = self._authorize_safe_path(root)
        if not safe_root.resolved.is_dir():
            raise ValueError(f"inventory root is not a directory: {safe_root.resolved}")
        files: list[InventoryFile] = []
        skipped: list[str] = []
        total_size = 0
        for path in sorted(item for item in safe_root.resolved.rglob("*") if item.is_file()):
            relative = path.relative_to(safe_root.resolved).as_posix()
            depth = len(Path(relative).parts)
            if depth > policy.max_depth:
                skipped.append(f"{relative}: depth")
                continue
            if any(
                fnmatch.fnmatch(Path(relative).name, pattern)
                for pattern in policy.excluded_patterns
            ):
                skipped.append(f"{relative}: excluded")
                continue
            if len(files) >= policy.max_files:
                skipped.append(f"{relative}: max_files")
                continue
            safe_file = authorize_path(str(path), self.allowed_roots)
            reject_special_file(safe_file.resolved)
            stat_result = safe_file.resolved.stat()
            if total_size + stat_result.st_size > policy.max_total_inventory_bytes:
                skipped.append(f"{relative}: max_total_inventory_bytes")
                continue
            total_size += stat_result.st_size
            sha256 = (
                None
                if stat_result.st_size > policy.max_single_read_bytes
                else hashlib.sha256(safe_file.resolved.read_bytes()).hexdigest()
            )
            files.append(
                InventoryFile(
                    path=str(safe_file.resolved),
                    relative_path=relative,
                    size_bytes=stat_result.st_size,
                    mtime_epoch=stat_result.st_mtime,
                    sha256=sha256,
                )
            )
        return OutputInventory(
            root=str(safe_root.resolved),
            files=files,
            skipped=skipped,
            total_size_bytes=total_size,
        )

    def _authorize_safe_path(self, path: SafePath) -> SafePath:
        return authorize_path(str(path.resolved), self.allowed_roots)


class DockerVolumeEvidenceTransport(AuthorizedFilesystemEvidenceTransport):
    """Evidence transport for Docker shared volumes mounted into the worker."""

    def probe(self, identity: UserIdentity) -> EvidenceCapability:
        capability = super().probe(identity)
        return EvidenceCapability(
            transport="docker_volume",
            can_stat=capability.can_stat,
            can_tail=capability.can_tail,
            can_inventory=capability.can_inventory,
            can_copy_selected=capability.can_copy_selected,
            authorized_roots=capability.authorized_roots,
            max_single_read_bytes=capability.max_single_read_bytes,
            notes=capability.notes,
        )


class EvidenceStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def run_root(self, run_id: str) -> Path:
        return self.root / "runs" / run_id

    def write_text(
        self,
        *,
        run_id: str,
        logical_path: str,
        content: str,
        content_type: str,
    ) -> EvidenceArtifact:
        path = self._resolve(run_id=run_id, logical_path=logical_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return self._artifact(logical_path=logical_path, path=path, content_type=content_type)

    def write_json(
        self,
        *,
        run_id: str,
        logical_path: str,
        payload: dict[str, Any],
    ) -> EvidenceArtifact:
        content = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        return self.write_text(
            run_id=run_id,
            logical_path=logical_path,
            content=content,
            content_type="application/json",
        )

    def _resolve(self, *, run_id: str, logical_path: str) -> Path:
        if logical_path.startswith("/") or ".." in Path(logical_path).parts:
            raise ValueError(f"unsafe evidence logical path: {logical_path!r}")
        run_root = self.run_root(run_id).resolve()
        path = (run_root / logical_path).resolve()
        if path != run_root and not path.is_relative_to(run_root):
            raise ValueError(f"evidence path escaped run root: {logical_path!r}")
        return path

    def _artifact(self, *, logical_path: str, path: Path, content_type: str) -> EvidenceArtifact:
        data = path.read_bytes()
        return EvidenceArtifact(
            logical_path=logical_path,
            path=path,
            size_bytes=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            content_type=content_type,
        )


class DemoEvidenceCollector:
    """Generate complete local Evidence for the Web demonstration backend."""

    def __init__(self, *, store: EvidenceStore, run_store: RunStore) -> None:
        self.store = store
        self.run_store = run_store

    def collect(self, *, run: RunRecord, task_type: str) -> EvidenceCollectionResult:
        match task_type:
            case "submission_snapshot":
                return self._submission_snapshot(run)
            case "runtime_status":
                return self._runtime_status(run)
            case "terminal_accounting":
                return self._terminal_accounting(run)
            case "logs_finalize":
                return self._logs_finalize(run)
            case "environment_finalize":
                return self._environment_finalize(run)
            case "outputs_inventory":
                return self._outputs_inventory(run)
            case "result_summary":
                return self._result_summary(run)
            case _:
                return EvidenceCollectionResult(
                    run_id=run.run_id,
                    task_type=task_type,
                    warnings=[f"no demo collector for task type: {task_type}"],
                )

    def _submission_snapshot(self, run: RunRecord) -> EvidenceCollectionResult:
        artifacts = [
            self.store.write_json(
                run_id=run.run_id,
                logical_path="submission/slurm_submit_response.json",
                payload={
                    "collector": "demo_submission_snapshot",
                    "collected_at": utc_now_iso(),
                    "job_id": run.job_id,
                    "submit_strategy": run.submit_strategy,
                    "raw_response": run.submit_response,
                },
            ),
            self.store.write_text(
                run_id=run.run_id,
                logical_path="submission/user_script.original.sh",
                content=run.script,
                content_type="text/x-shellscript",
            ),
            self.store.write_text(
                run_id=run.run_id,
                logical_path="submission/submitted_script.resolved.sh",
                content=run.script,
                content_type="text/x-shellscript",
            ),
            self.store.write_text(
                run_id=run.run_id,
                logical_path="submission/execution_wrapper.generated.sh",
                content=generated_execution_wrapper(run),
                content_type="text/x-shellscript",
            ),
            *write_official_request_artifacts(
                store=self.store,
                run=run,
                collector="demo_submission_snapshot",
            ),
        ]
        artifacts.extend(
            write_timeline_artifacts(store=self.store, run_store=self.run_store, run=run)
        )
        artifacts.append(self._write_manifest(run, warnings=[]))
        return EvidenceCollectionResult(
            run_id=run.run_id,
            task_type="submission_snapshot",
            artifacts=artifacts,
        )

    def _runtime_status(self, run: RunRecord) -> EvidenceCollectionResult:
        artifacts = [self.store.write_json(
            run_id=run.run_id,
            logical_path="slurm/runtime_status.json",
            payload={
                "schema": "pilot107.slurm.runtime_status.v1",
                "collector": "demo_runtime_status",
                "collected_at": utc_now_iso(),
                "availability": "known",
                "job": {
                    "job_id": run.job_id,
                    "owner": run.owner,
                    "state": run.state.value,
                    "reason": None,
                    "partition": run.resource_plan.get("partition"),
                },
            },
        )]
        artifacts.append(self._write_manifest(run, warnings=[]))
        return EvidenceCollectionResult(
            run_id=run.run_id,
            task_type="runtime_status",
            artifacts=artifacts,
        )

    def _terminal_accounting(self, run: RunRecord) -> EvidenceCollectionResult:
        job_id = _require_job_id(run)
        accounting_stdout = (
            f"{job_id}|{run.owner}|{run.resource_plan.get('account', '')}|"
            f"{run.resource_plan.get('partition', '')}|{run.resource_plan.get('qos', '')}|"
            f"COMPLETED|0:0|00:00:01|1|cpu=1|cpu=1|demo-node|"
            f"{run.created_at}|{utc_now_iso()}\n"
        )
        accounting_fields = (
            "job_id",
            "owner",
            "account",
            "partition",
            "qos",
            "state",
            "exit_code",
            "elapsed",
            "allocated_cpus",
            "requested_tres",
            "allocated_tres",
            "node_list",
            "start",
            "end",
        )
        artifacts = [
            self.store.write_json(
                run_id=run.run_id,
                logical_path="slurm/accounting.json",
                payload={
                    "collector": "demo_terminal_accounting",
                    "collected_at": utc_now_iso(),
                    "command": ["demo", "sacct"],
                    "returncode": 0,
                    "stdout": accounting_stdout,
                    "stderr": "",
                    "fields": list(accounting_fields),
                    "records": _parse_pipe_records(accounting_stdout, accounting_fields),
                },
            ),
            self.store.write_json(
                run_id=run.run_id,
                logical_path="slurm/job_detail.json",
                payload={
                    "collector": "demo_terminal_accounting",
                    "collected_at": utc_now_iso(),
                    "command": ["demo", "scontrol", "show", "job", job_id],
                    "returncode": 0,
                    "stdout": f"JobId={job_id} UserId={run.owner} JobState=COMPLETED ExitCode=0:0",
                    "stderr": "",
                },
            ),
        ]
        artifacts.append(self._write_manifest(run, warnings=[]))
        return EvidenceCollectionResult(
            run_id=run.run_id,
            task_type="terminal_accounting",
            artifacts=artifacts,
        )

    def _logs_finalize(self, run: RunRecord) -> EvidenceCollectionResult:
        stdout = (
            "107Pilot demo backend\n"
            f"run_id={run.run_id}\n"
            f"job_id={run.job_id}\n"
            "status=completed\n"
        )
        stderr = "demo backend: no stderr\n"
        artifacts = [
            self.store.write_json(
                run_id=run.run_id,
                logical_path="logs/stdout.tail.json",
                payload={
                    "collector": "demo_logs_finalize",
                    "collected_at": utc_now_iso(),
                    "source_path": f"demo://runs/{run.run_id}/stdout",
                    "stream": "stdout",
                    "metadata": {
                        "status": "present",
                        "type": "regular file",
                        "size_bytes": len(stdout.encode("utf-8")),
                        "owner": run.owner,
                        "group": run.owner,
                    },
                    "tail_bytes": len(stdout.encode("utf-8")),
                    "tail": stdout,
                    "sha256": hashlib.sha256(stdout.encode("utf-8")).hexdigest(),
                },
            ),
            self.store.write_json(
                run_id=run.run_id,
                logical_path="logs/stderr.tail.json",
                payload={
                    "collector": "demo_logs_finalize",
                    "collected_at": utc_now_iso(),
                    "source_path": f"demo://runs/{run.run_id}/stderr",
                    "stream": "stderr",
                    "metadata": {
                        "status": "present",
                        "type": "regular file",
                        "size_bytes": len(stderr.encode("utf-8")),
                        "owner": run.owner,
                        "group": run.owner,
                    },
                    "tail_bytes": len(stderr.encode("utf-8")),
                    "tail": stderr,
                    "sha256": hashlib.sha256(stderr.encode("utf-8")).hexdigest(),
                },
            ),
        ]
        artifacts.append(self._write_manifest(run, warnings=[]))
        return EvidenceCollectionResult(
            run_id=run.run_id,
            task_type="logs_finalize",
            artifacts=artifacts,
        )

    def _environment_finalize(self, run: RunRecord) -> EvidenceCollectionResult:
        artifacts = [
            self.store.write_json(
                run_id=run.run_id,
                logical_path="environment/summary.json",
                payload={
                    "collector": "demo_environment_finalize",
                    "collected_at": utc_now_iso(),
                    "scope": "demo-backend",
                    "run_id": run.run_id,
                    "owner": run.owner,
                    "job_id": run.job_id,
                    "workdir": run.workdir,
                    "results": {
                        "hostname": {"returncode": 0, "stdout": "demo-node\n", "stderr": ""},
                        "id": {
                            "returncode": 0,
                            "stdout": f"uid=1000({run.owner}) gid=1000({run.owner})\n",
                            "stderr": "",
                        },
                        "env": {
                            "returncode": 0,
                            "stdout": f"USER={run.owner}\nPILOT107_RUN_ID={run.run_id}\n",
                            "stderr": "",
                        },
                    },
                },
            ),
            self.store.write_json(
                run_id=run.run_id,
                logical_path="run/environment/basic.json",
                payload=basic_environment_payload(
                    collector="demo_environment_finalize",
                    run=run,
                    collected_at=utc_now_iso(),
                    scope="demo-backend",
                    results={},
                ),
            ),
        ]
        artifacts.extend(
            write_timeline_artifacts(store=self.store, run_store=self.run_store, run=run)
        )
        artifacts.append(self._write_manifest(run, warnings=[]))
        return EvidenceCollectionResult(
            run_id=run.run_id,
            task_type="environment_finalize",
            artifacts=artifacts,
        )

    def _outputs_inventory(self, run: RunRecord) -> EvidenceCollectionResult:
        output = f"demo output for {run.run_id}\n"
        output_artifact = self.store.write_text(
            run_id=run.run_id,
            logical_path="outputs/files/pilot107-demo-output/result.txt",
            content=output,
            content_type="text/plain",
        )
        artifacts = [
            output_artifact,
            self.store.write_json(
                run_id=run.run_id,
                logical_path="outputs/inventory.json",
                payload={
                    "collector": "demo_outputs_inventory",
                    "collected_at": utc_now_iso(),
                    "workdir": run.workdir,
                    "max_depth": 3,
                    "excluded_patterns": ["slurm-*.out", "slurm-*.err", "pilot107-submit-*.sbatch"],
                    "files": [
                        {
                            "path": f"demo://runs/{run.run_id}/pilot107-demo-output/result.txt",
                            "relative_path": "pilot107-demo-output/result.txt",
                            "size_bytes": output_artifact.size_bytes,
                            "owner": run.owner,
                            "group": run.owner,
                            "sha256": output_artifact.sha256,
                        }
                    ],
                    "command": ["demo", "find"],
                    "returncode": 0,
                    "stderr": "",
                },
            ),
        ]
        artifacts.append(self._write_manifest(run, warnings=[]))
        return EvidenceCollectionResult(
            run_id=run.run_id,
            task_type="outputs_inventory",
            artifacts=artifacts,
        )

    def _result_summary(self, run: RunRecord) -> EvidenceCollectionResult:
        objects = self.run_store.list_evidence_objects(run.run_id)
        object_paths = {obj.logical_path for obj in objects}
        required_paths = {
            "slurm/accounting.json",
            "slurm/job_detail.json",
            "logs/stdout.tail.json",
            "logs/stderr.tail.json",
            "environment/summary.json",
            "outputs/inventory.json",
        }
        missing = sorted(required_paths - object_paths)
        if missing:
            raise SlurmTransportError(f"result summary prerequisites missing: {missing}")
        artifacts = [
            self.store.write_json(
                run_id=run.run_id,
                logical_path="derived/result_summary.v1.json",
                payload={
                    "schema": "pilot107.result_summary.v1",
                    "collector": "demo_result_summary",
                    "created_at": utc_now_iso(),
                    "run_id": run.run_id,
                    "owner": run.owner,
                    "job_id": run.job_id,
                    "run_state": run.state.value,
                    "terminal_state": run.terminal_state,
                    "exit_code": run.exit_code,
                    "result_status": run.result_status.value,
                    "collection_state": run.collection_state.value,
                    "outputs": {
                        "file_count": 1,
                        "total_size_bytes": sum(
                            obj.size_bytes or 0
                            for obj in objects
                            if obj.logical_path.startswith("outputs/files/")
                        ),
                    },
                    "objects": [
                        {
                            "category": obj.category,
                            "logical_path": obj.logical_path,
                            "sha256": obj.sha256,
                            "size_bytes": obj.size_bytes,
                        }
                        for obj in objects
                    ],
                },
            )
        ]
        artifacts.extend(
            write_timeline_artifacts(store=self.store, run_store=self.run_store, run=run)
        )
        artifacts.append(self._write_manifest(run, warnings=[]))
        return EvidenceCollectionResult(
            run_id=run.run_id,
            task_type="result_summary",
            artifacts=artifacts,
        )

    def _write_manifest(self, run: RunRecord, *, warnings: list[str]) -> EvidenceArtifact:
        run_root = self.store.run_root(run.run_id)
        artifacts = []
        if run_root.exists():
            for path in sorted(item for item in run_root.rglob("*") if item.is_file()):
                logical_path = path.relative_to(run_root).as_posix()
                if logical_path == "manifest/manifest.json":
                    continue
                artifacts.append(
                    EvidenceArtifact(
                        logical_path=logical_path,
                        path=path,
                        size_bytes=path.stat().st_size,
                        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                        content_type=_content_type(path),
                    )
                )
        collected_at = utc_now_iso()
        self.run_store.upsert_evidence_objects(
            run.run_id,
            [
                _evidence_object_payload(
                    run_id=run.run_id,
                    run_root=run_root,
                    artifact=artifact,
                    finalized_at=collected_at,
                )
                for artifact in artifacts
            ],
        )
        manifest = self.store.write_json(
            run_id=run.run_id,
            logical_path="manifest/manifest.json",
            payload={
                "schema": "pilot107.evidence_manifest.v1",
                "run_id": run.run_id,
                "owner": run.owner,
                "job_id": run.job_id,
                "run_state": run.state.value,
                "exit_code": run.exit_code,
                "collected_at": collected_at,
                "artifacts": [
                    {
                        "logical_path": artifact.logical_path,
                        "size_bytes": artifact.size_bytes,
                        "sha256": artifact.sha256,
                        "content_type": artifact.content_type,
                        "evidence_ref": f"evidence://runs/{run.run_id}/{artifact.logical_path}",
                    }
                    for artifact in sorted(artifacts, key=lambda item: item.logical_path)
                ],
                "warnings": warnings,
            },
        )
        self.run_store.upsert_evidence_objects(
            run.run_id,
            [
                _evidence_object_payload(
                    run_id=run.run_id,
                    run_root=run_root,
                    artifact=manifest,
                    finalized_at=collected_at,
                )
            ],
        )
        return manifest


class DockerSlurmEvidenceCollector:
    def __init__(
        self,
        *,
        store: EvidenceStore,
        executor: SimulatorExecutor,
        allowed_roots: list[str],
        run_store: RunStore | None = None,
        evidence_transport: EvidenceTransport | None = None,
        evidence_policy: EvidencePolicy | None = None,
        log_tail_bytes: int = 65536,
        timeout_seconds: float = 20.0,
        contract_store: ContractStore | None = None,
    ) -> None:
        self.store = store
        self.executor = executor
        self.allowed_roots = allowed_roots
        self.run_store = run_store
        self.evidence_transport = evidence_transport
        self.evidence_policy = evidence_policy or EvidencePolicy()
        self.log_tail_bytes = log_tail_bytes
        self.timeout_seconds = timeout_seconds
        self.contract_store = contract_store

    def collect(self, *, run: RunRecord, task_type: str) -> EvidenceCollectionResult:
        match task_type:
            case "submission_snapshot":
                return self._collect_submission_snapshot(run)
            case "runtime_status":
                return self._collect_runtime_status(run)
            case "terminal_accounting":
                return self._collect_terminal_accounting(run)
            case "logs_finalize":
                return self._collect_logs_finalize(run)
            case "environment_finalize":
                return self._collect_environment_finalize(run)
            case "outputs_inventory":
                return self._collect_outputs_inventory(run)
            case "result_summary":
                return self._collect_result_summary(run)
            case _:
                return EvidenceCollectionResult(
                    run_id=run.run_id,
                    task_type=task_type,
                    warnings=[f"no collector for task type: {task_type}"],
                )

    def _collect_submission_snapshot(self, run: RunRecord) -> EvidenceCollectionResult:
        artifacts = [
            self.store.write_json(
                run_id=run.run_id,
                logical_path="submission/slurm_submit_response.json",
                payload={
                    "collector": "submission_snapshot",
                    "collected_at": utc_now_iso(),
                    "job_id": run.job_id,
                    "submit_strategy": run.submit_strategy,
                    "raw_response": run.submit_response,
                },
            ),
            self.store.write_text(
                run_id=run.run_id,
                logical_path="submission/user_script.original.sh",
                content=run.script,
                content_type="text/x-shellscript",
            ),
            self.store.write_text(
                run_id=run.run_id,
                logical_path="submission/submitted_script.resolved.sh",
                content=run.script,
                content_type="text/x-shellscript",
            ),
            self.store.write_text(
                run_id=run.run_id,
                logical_path="submission/execution_wrapper.generated.sh",
                content=generated_execution_wrapper(run),
                content_type="text/x-shellscript",
            ),
            *write_official_request_artifacts(
                store=self.store,
                run=run,
                collector="submission_snapshot",
            ),
        ]
        artifacts.extend(
            write_timeline_artifacts(store=self.store, run_store=self.run_store, run=run)
        )
        artifacts.append(self._write_manifest(run, extra_artifacts=artifacts, warnings=[]))
        return EvidenceCollectionResult(
            run_id=run.run_id,
            task_type="submission_snapshot",
            artifacts=artifacts,
        )

    def _collect_runtime_status(self, run: RunRecord) -> EvidenceCollectionResult:
        job_id = _require_job_id(run)
        fields = (
            "job_id",
            "owner",
            "state",
            "reason",
            "partition",
            "name",
            "cpus",
            "minimum_memory",
            "tres_per_node",
        )
        result = self._run_user(
            run,
            [
                "squeue",
                "-h",
                "-j",
                job_id,
                "-o",
                "%i|%u|%T|%R|%P|%j|%C|%m|%b",
            ],
        )
        warnings: list[str] = []
        record = None
        availability = "known"
        if result.result.returncode != 0:
            availability = "unavailable"
            warnings.append("squeue returned non-zero")
        elif result.result.stdout.strip():
            rows = _parse_pipe_records(result.result.stdout, fields)
            if len(rows) != 1:
                raise ValueError("squeue runtime status must contain exactly one job row")
            record = rows[0]
            if record["job_id"] != job_id or record["owner"] != run.owner:
                raise ValueError("squeue runtime status owner or job ID mismatch")
        else:
            availability = "unavailable"
            warnings.append("job was not present in squeue during runtime status collection")
        artifacts = [self.store.write_json(
            run_id=run.run_id,
            logical_path="slurm/runtime_status.json",
            payload={
                "schema": "pilot107.slurm.runtime_status.v1",
                "collector": "runtime_status",
                "collected_at": utc_now_iso(),
                "availability": availability,
                "job": record,
                "command": result.argv,
                "returncode": result.result.returncode,
                "stderr": result.result.stderr,
            },
        )]
        artifacts.append(
            self._write_manifest(run, extra_artifacts=artifacts, warnings=warnings)
        )
        return EvidenceCollectionResult(
            run_id=run.run_id,
            task_type="runtime_status",
            artifacts=artifacts,
            warnings=warnings,
        )

    def _collect_terminal_accounting(self, run: RunRecord) -> EvidenceCollectionResult:
        job_id = _require_job_id(run)
        artifacts: list[EvidenceArtifact] = []
        warnings: list[str] = []

        accounting_fields = (
            "job_id",
            "owner",
            "account",
            "partition",
            "qos",
            "state",
            "exit_code",
            "elapsed",
            "allocated_cpus",
            "requested_tres",
            "allocated_tres",
            "node_list",
            "start",
            "end",
        )

        sacct = self._run_user(
            run,
            [
                "sacct",
                "-nP",
                "-j",
                job_id,
                "-X",
                "-o",
                (
                    "JobIDRaw,User,Account,Partition,QOS,State,ExitCode,Elapsed,"
                    "AllocCPUS,ReqTRES,AllocTRES,NodeList,Start,End"
                ),
            ],
        )
        accounting_records = (
            _parse_pipe_records(sacct.result.stdout, accounting_fields)
            if sacct.result.returncode == 0 and sacct.result.stdout.strip()
            else []
        )
        if any(item["owner"] != run.owner for item in accounting_records):
            raise ValueError("sacct accounting owner mismatch")
        artifacts.append(
            self.store.write_json(
                run_id=run.run_id,
                logical_path="slurm/accounting.json",
                payload={
                    "collector": "terminal_accounting",
                    "collected_at": utc_now_iso(),
                    "command": sacct.argv,
                    "returncode": sacct.result.returncode,
                    "stdout": sacct.result.stdout,
                    "stderr": sacct.result.stderr,
                    "fields": list(accounting_fields),
                    "records": accounting_records,
                },
            )
        )

        scontrol = self._run_user(run, ["scontrol", "-o", "show", "job", job_id])
        artifacts.append(
            self.store.write_json(
                run_id=run.run_id,
                logical_path="slurm/job_detail.json",
                payload={
                    "collector": "terminal_accounting",
                    "collected_at": utc_now_iso(),
                    "command": scontrol.argv,
                    "returncode": scontrol.result.returncode,
                    "stdout": scontrol.result.stdout,
                    "stderr": scontrol.result.stderr,
                },
            )
        )
        if sacct.result.returncode != 0:
            warnings.append("sacct returned non-zero")
        if scontrol.result.returncode != 0:
            warnings.append("scontrol returned non-zero")

        artifacts.append(self._write_manifest(run, extra_artifacts=artifacts, warnings=warnings))
        return EvidenceCollectionResult(
            run_id=run.run_id,
            task_type="terminal_accounting",
            artifacts=artifacts,
            warnings=warnings,
        )

    def _collect_logs_finalize(self, run: RunRecord) -> EvidenceCollectionResult:
        job_id = _require_job_id(run)
        if self.evidence_transport is not None:
            return self._collect_logs_finalize_via_transport(run, job_id)

        artifacts: list[EvidenceArtifact] = []
        warnings: list[str] = []
        for stream_name, container_path in {
            "stdout": _slurm_log_path(run, job_id, "out"),
            "stderr": _slurm_log_path(run, job_id, "err"),
        }.items():
            authorized = self._authorize_source_path(container_path, user=run.owner)
            metadata = self._file_metadata(run, authorized)
            tail = self._tail_file(run, authorized)
            sha = self._sha256_file(run, authorized)
            payload = {
                "collector": "logs_finalize",
                "collected_at": utc_now_iso(),
                "source_path": authorized,
                "stream": stream_name,
                "metadata": metadata,
                "tail_bytes": self.log_tail_bytes,
                "tail": tail,
                "sha256": sha,
            }
            if metadata["status"] == "missing":
                warnings.append(f"{stream_name} log missing")
            artifacts.append(
                self.store.write_json(
                    run_id=run.run_id,
                    logical_path=f"logs/{stream_name}.tail.json",
                    payload=payload,
                )
            )

        artifacts.append(self._write_manifest(run, extra_artifacts=artifacts, warnings=warnings))
        return EvidenceCollectionResult(
            run_id=run.run_id,
            task_type="logs_finalize",
            artifacts=artifacts,
            warnings=warnings,
        )

    def _collect_logs_finalize_via_transport(
        self,
        run: RunRecord,
        job_id: str,
    ) -> EvidenceCollectionResult:
        artifacts: list[EvidenceArtifact] = []
        warnings: list[str] = []
        identity = UserIdentity(username=run.owner)
        transport = self._require_evidence_transport()
        for stream_name, source_path in {
            "stdout": _slurm_log_path(run, job_id, "out"),
            "stderr": _slurm_log_path(run, job_id, "err"),
        }.items():
            safe_path = self._safe_source_path(source_path, user=run.owner)
            metadata, tail, sha = self._transport_log_payload(identity, safe_path)
            payload = {
                "collector": "logs_finalize",
                "transport": transport.probe(identity).transport,
                "collected_at": utc_now_iso(),
                "source_path": str(safe_path.resolved),
                "stream": stream_name,
                "metadata": metadata,
                "tail_bytes": self.log_tail_bytes,
                "tail": tail,
                "sha256": sha,
            }
            if metadata["status"] == "missing":
                warnings.append(f"{stream_name} log missing")
            artifacts.append(
                self.store.write_json(
                    run_id=run.run_id,
                    logical_path=f"logs/{stream_name}.tail.json",
                    payload=payload,
                )
            )

        artifacts.append(self._write_manifest(run, extra_artifacts=artifacts, warnings=warnings))
        return EvidenceCollectionResult(
            run_id=run.run_id,
            task_type="logs_finalize",
            artifacts=artifacts,
            warnings=warnings,
        )

    def _collect_environment_finalize(self, run: RunRecord) -> EvidenceCollectionResult:
        artifacts: list[EvidenceArtifact] = []
        warnings: list[str] = []
        commands = {
            "pwd": ["pwd"],
            "whoami": ["whoami"],
            "date_iso": ["date", "-Is"],
            "hostname": ["hostname"],
            "id": ["id"],
            "python_version": ["python", "-V"],
            "which_python": ["which", "python"],
            "env": ["env"],
        }
        results: dict[str, dict[str, Any]] = {}
        for name, argv in commands.items():
            executed = self._run_user(run, argv, cwd=run.workdir)
            if executed.result.returncode != 0:
                warnings.append(f"{name} returned non-zero")
            stdout = executed.result.stdout
            if name == "env":
                stdout = _filter_environment(stdout)
            results[name] = {
                "command": executed.argv,
                "returncode": executed.result.returncode,
                "stdout": stdout,
                "stderr": executed.result.stderr,
            }

        artifacts.append(
            self.store.write_json(
                run_id=run.run_id,
                logical_path="environment/summary.json",
                payload={
                    "collector": "environment_finalize",
                    "collected_at": utc_now_iso(),
                    "scope": "simulator-login-node-user-probe",
                    "run_id": run.run_id,
                    "owner": run.owner,
                    "job_id": run.job_id,
                    "workdir": run.workdir,
                    "results": results,
                },
            )
        )
        artifacts.append(
            self.store.write_json(
                run_id=run.run_id,
                logical_path="run/environment/basic.json",
                payload=basic_environment_payload(
                    collector="environment_finalize",
                    run=run,
                    collected_at=utc_now_iso(),
                    scope="simulator-login-node-user-probe",
                    results=results,
                ),
            )
        )
        gpu_artifact, gpu_warning = self._gpu_environment_artifact(run)
        if gpu_artifact is not None:
            artifacts.append(gpu_artifact)
        if gpu_warning is not None:
            warnings.append(gpu_warning)
        artifacts.extend(
            write_timeline_artifacts(store=self.store, run_store=self.run_store, run=run)
        )
        artifacts.append(self._write_manifest(run, extra_artifacts=artifacts, warnings=warnings))
        return EvidenceCollectionResult(
            run_id=run.run_id,
            task_type="environment_finalize",
            artifacts=artifacts,
            warnings=warnings,
        )

    def _collect_outputs_inventory(self, run: RunRecord) -> EvidenceCollectionResult:
        if self.evidence_transport is not None:
            return self._collect_outputs_inventory_via_transport(run)

        workdir = self._authorize_source_path(run.workdir, user=run.owner)
        find = self._run_user(
            run,
            [
                "find",
                workdir,
                "-maxdepth",
                "3",
                "-type",
                "f",
                "-printf",
                "%p|%s|%T@|%u|%g\n",
            ],
        )
        warnings: list[str] = []
        if find.result.returncode != 0:
            warnings.append("find returned non-zero")
        expected_outputs = self._resolve_expected_outputs(run)
        started_at_iso = self._resolve_started_at(run)
        started_at_epoch = _iso_to_epoch(started_at_iso)
        baseline_map = _load_baseline(self.store.run_root(run.run_id))
        files = self._parse_inventory_rows(
            run=run,
            workdir=workdir,
            stdout=find.result.stdout if find.result.returncode == 0 else "",
            warnings=warnings,
            expected_outputs=expected_outputs,
            started_at_epoch=started_at_epoch,
            baseline_map=baseline_map,
        )
        _append_missing_expected(files, expected_outputs, baseline_map)
        artifacts = [
            self.store.write_json(
                run_id=run.run_id,
                logical_path="outputs/inventory.json",
                payload={
                    "collector": "outputs_inventory",
                    "collected_at": utc_now_iso(),
                    "workdir": workdir,
                    "max_depth": 3,
                    "excluded_patterns": ["slurm-*.out", "slurm-*.err", "pilot107-submit-*.sbatch"],
                    "run_started_at": started_at_iso,
                    "expected_outputs": expected_outputs,
                    "attribution_summary": _attribution_summary(files),
                    "files": files,
                    "command": find.argv,
                    "returncode": find.result.returncode,
                    "stderr": find.result.stderr,
                },
            )
        ]
        artifacts.append(self._write_manifest(run, extra_artifacts=artifacts, warnings=warnings))
        return EvidenceCollectionResult(
            run_id=run.run_id,
            task_type="outputs_inventory",
            artifacts=artifacts,
            warnings=warnings,
        )

    def _collect_outputs_inventory_via_transport(self, run: RunRecord) -> EvidenceCollectionResult:
        workdir = self._safe_source_path(run.workdir, user=run.owner)
        identity = UserIdentity(username=run.owner)
        transport = self._require_evidence_transport()
        policy = EvidencePolicy(
            max_depth=3,
            max_files=self.evidence_policy.max_files,
            max_single_read_bytes=self.evidence_policy.max_single_read_bytes,
            max_total_inventory_bytes=self.evidence_policy.max_total_inventory_bytes,
            excluded_patterns=self.evidence_policy.excluded_patterns,
        )
        inventory = transport.inventory(identity, workdir, policy)
        warnings = list(inventory.skipped)
        expected_outputs = self._resolve_expected_outputs(run)
        started_at_iso = self._resolve_started_at(run)
        started_at_epoch = _iso_to_epoch(started_at_iso)
        baseline_map = _load_baseline(self.store.run_root(run.run_id))
        files: list[dict[str, Any]] = []
        for file in inventory.files:
            if _is_excluded_output(file.relative_path):
                continue
            is_expected = file.relative_path in expected_outputs
            baseline_entry = baseline_map.get(file.relative_path) if is_expected else None
            eligible = is_expected or (
                baseline_entry is None
                and (
                    started_at_epoch is None
                    or file.mtime_epoch > started_at_epoch
                )
            )
            final_sha = file.sha256 if eligible else None
            attribution = compute_file_attribution(
                mtime_epoch=file.mtime_epoch,
                started_at_epoch=started_at_epoch,
                relative_path=file.relative_path,
                expected_outputs=expected_outputs,
                baseline_entry=baseline_entry,
                is_expected=is_expected,
                final_sha256=final_sha,
            )
            files.append(
                {
                    "path": file.path,
                    "relative_path": file.relative_path,
                    "size_bytes": file.size_bytes,
                    "mtime_epoch": file.mtime_epoch,
                    "owner": run.owner,
                    "group": run.owner,
                    # sha256 is the attribution-gated hash (null for preexisting
                    # non-expected files), matching the local find-based path.
                    "sha256": final_sha,
                    "attribution": attribution["attribution"],
                    "in_expected_outputs": attribution["in_expected_outputs"],
                    "final_sha256": final_sha,
                    "baseline_sha256": attribution["baseline_sha256"],
                }
            )
        _append_missing_expected(files, expected_outputs, baseline_map)
        files = sorted(files, key=lambda item: str(item.get("relative_path")))
        artifacts = [
            self.store.write_json(
                run_id=run.run_id,
                logical_path="outputs/inventory.json",
                payload={
                    "collector": "outputs_inventory",
                    "transport": transport.probe(identity).transport,
                    "collected_at": utc_now_iso(),
                    "workdir": str(workdir.resolved),
                    "max_depth": policy.max_depth,
                    "excluded_patterns": list(policy.excluded_patterns),
                    "run_started_at": started_at_iso,
                    "expected_outputs": expected_outputs,
                    "attribution_summary": _attribution_summary(files),
                    "files": files,
                    "command": None,
                    "returncode": 0,
                    "stderr": "",
                    "skipped": inventory.skipped,
                },
            )
        ]
        artifacts.append(self._write_manifest(run, extra_artifacts=artifacts, warnings=warnings))
        return EvidenceCollectionResult(
            run_id=run.run_id,
            task_type="outputs_inventory",
            artifacts=artifacts,
            warnings=warnings,
        )

    def _resolve_expected_outputs(self, run: RunRecord) -> list[str]:
        if self.contract_store is None or run.contract_id is None:
            return []
        try:
            contract = self.contract_store.get_contract(run.contract_id)
        except Exception:  # noqa: BLE001 - attribution tagging must never crash collection
            return []
        outputs = contract.payload.get("outputs") or {}
        expected = outputs.get("expected") or []
        if not isinstance(expected, list):
            return []
        # Round-8 P2-2: use the shared parser so typed objects like
        # {"path": "metrics.json", "type": "json"} extract their path instead
        # of becoming a dict-repr garbage string via str(item).
        return [parse_expected_output(item) for item in expected]

    def _resolve_started_at(self, run: RunRecord) -> str | None:
        # RunRecord does not yet carry a dedicated started_at field; fall back to
        # created_at (the run creation timestamp) as the run-start proxy so that
        # attribution still works. If neither is available, attribution becomes
        # "unknown" via compute_file_attribution.
        started_at = getattr(run, "started_at", None)
        if started_at is not None:
            return str(started_at)
        return run.created_at

    def _collect_result_summary(self, run: RunRecord) -> EvidenceCollectionResult:
        required_paths = {
            "slurm/accounting.json",
            "slurm/job_detail.json",
            "logs/stdout.tail.json",
            "logs/stderr.tail.json",
            "environment/summary.json",
            "outputs/inventory.json",
        }
        objects = [] if self.run_store is None else self.run_store.list_evidence_objects(run.run_id)
        object_paths = {obj.logical_path for obj in objects}
        missing = sorted(required_paths - object_paths)
        if missing:
            raise SlurmTransportError(f"result summary prerequisites missing: {missing}")

        run_root = self.store.run_root(run.run_id)
        accounting = _read_json_or_none(run_root / "slurm" / "accounting.json")
        job_detail = _read_json_or_none(run_root / "slurm" / "job_detail.json")
        stdout_tail = _read_json_or_none(run_root / "logs" / "stdout.tail.json")
        stderr_tail = _read_json_or_none(run_root / "logs" / "stderr.tail.json")
        environment = _read_json_or_none(run_root / "environment" / "summary.json")
        outputs = _read_json_or_none(run_root / "outputs" / "inventory.json")
        output_files = outputs.get("files", []) if isinstance(outputs, dict) else []
        payload = {
            "schema": "pilot107.result_summary.v1",
            "collector": "result_summary",
            "created_at": utc_now_iso(),
            "run_id": run.run_id,
            "owner": run.owner,
            "job_id": run.job_id,
            "run_state": run.state.value,
            "terminal_state": run.terminal_state,
            "exit_code": run.exit_code,
            "result_status": run.result_status.value,
            "collection_state": run.collection_state.value,
            "evidence_refs": {
                "accounting": f"evidence://runs/{run.run_id}/slurm/accounting.json",
                "job_detail": f"evidence://runs/{run.run_id}/slurm/job_detail.json",
                "stdout_tail": f"evidence://runs/{run.run_id}/logs/stdout.tail.json",
                "stderr_tail": f"evidence://runs/{run.run_id}/logs/stderr.tail.json",
                "environment": f"evidence://runs/{run.run_id}/environment/summary.json",
                "outputs": f"evidence://runs/{run.run_id}/outputs/inventory.json",
            },
            "slurm": {
                "accounting_returncode": (
                    None if accounting is None else accounting.get("returncode")
                ),
                "accounting_stdout": None if accounting is None else accounting.get("stdout"),
                "job_detail_returncode": (
                    None if job_detail is None else job_detail.get("returncode")
                ),
            },
            "logs": {
                "stdout_status": _metadata_status(stdout_tail),
                "stderr_status": _metadata_status(stderr_tail),
                "stdout_sha256": None if stdout_tail is None else stdout_tail.get("sha256"),
                "stderr_sha256": None if stderr_tail is None else stderr_tail.get("sha256"),
            },
            "environment": {
                "scope": None if environment is None else environment.get("scope"),
            },
            "outputs": {
                "file_count": len(output_files),
                "total_size_bytes": sum(int(item.get("size_bytes") or 0) for item in output_files),
                "attributed_file_count": sum(
                    1
                    for item in output_files
                    if item.get("attribution")
                    in {"created_by_run", "created", "modified"}
                ),
                "attribution_summary": _attribution_summary(output_files),
                "expected_outputs": [
                    {
                        "path": item.get("relative_path"),
                        "attribution": item.get("attribution"),
                        "baseline_sha256": item.get("baseline_sha256"),
                        "final_sha256": item.get("final_sha256"),
                    }
                    for item in output_files
                    if item.get("in_expected_outputs")
                ],
                "files": [
                    {
                        "relative_path": item.get("relative_path"),
                        "size_bytes": item.get("size_bytes"),
                        "sha256": item.get("sha256"),
                        "attribution": item.get("attribution"),
                        "in_expected_outputs": item.get("in_expected_outputs"),
                        "baseline_sha256": item.get("baseline_sha256"),
                        "final_sha256": item.get("final_sha256"),
                    }
                    for item in output_files[:50]
                ],
            },
            "objects": [
                {
                    "category": obj.category,
                    "logical_path": obj.logical_path,
                    "sha256": obj.sha256,
                    "size_bytes": obj.size_bytes,
                }
                for obj in objects
            ],
        }
        artifacts = [
            self.store.write_json(
                run_id=run.run_id,
                logical_path="derived/result_summary.v1.json",
                payload=payload,
            )
        ]
        artifacts.extend(
            write_timeline_artifacts(store=self.store, run_store=self.run_store, run=run)
        )
        artifacts.append(self._write_manifest(run, extra_artifacts=artifacts, warnings=[]))
        return EvidenceCollectionResult(
            run_id=run.run_id,
            task_type="result_summary",
            artifacts=artifacts,
        )

    def _gpu_environment_artifact(
        self,
        run: RunRecord,
    ) -> tuple[EvidenceArtifact | None, str | None]:
        requested_gpus = requested_gpu_count(run.resource_plan)
        if requested_gpus <= 0:
            return None, None
        argv = [
            "nvidia-smi",
            "--query-gpu=name,uuid,driver_version,memory.total",
            "--format=csv,noheader",
        ]
        executed = self._run_user(run, argv, cwd=run.workdir)
        status = "known" if executed.result.returncode == 0 else "unavailable"
        reason = None if executed.result.returncode == 0 else gpu_probe_reason(executed.result)
        artifact = self.store.write_json(
            run_id=run.run_id,
            logical_path="run/environment/gpu.json",
            payload={
                "schema": "pilot107.run.environment.gpu.v1",
                "collector": "environment_finalize",
                "collected_at": utc_now_iso(),
                "run_id": run.run_id,
                "job_id": run.job_id,
                "requested_gpus": requested_gpus,
                "probe": {
                    "command": executed.argv,
                    "returncode": executed.result.returncode,
                    "stdout": redact_gpu_uuids(executed.result.stdout),
                    "stderr": executed.result.stderr,
                    "status": status,
                    "reason": reason,
                },
            },
        )
        warning = None if status == "known" else f"gpu probe unavailable: {reason}"
        return artifact, warning

    def _write_manifest(
        self,
        run: RunRecord,
        *,
        extra_artifacts: list[EvidenceArtifact],
        warnings: list[str],
    ) -> EvidenceArtifact:
        run_root = self.store.run_root(run.run_id)
        artifacts = self._existing_artifacts(run_root)
        collected_at = utc_now_iso()
        payload = {
            "schema": "pilot107.evidence_manifest.v1",
            "run_id": run.run_id,
            "owner": run.owner,
            "job_id": run.job_id,
            "run_state": run.state.value,
            "exit_code": run.exit_code,
            "collected_at": collected_at,
            "artifacts": [
                {
                    "logical_path": artifact.logical_path,
                    "size_bytes": artifact.size_bytes,
                    "sha256": artifact.sha256,
                    "content_type": artifact.content_type,
                    "evidence_ref": f"evidence://runs/{run.run_id}/{artifact.logical_path}",
                }
                for artifact in sorted(artifacts, key=lambda item: item.logical_path)
            ],
            "warnings": warnings,
        }
        if self.run_store is not None:
            self.run_store.upsert_evidence_objects(
                run.run_id,
                [
                    _evidence_object_payload(
                        run_id=run.run_id,
                        run_root=run_root,
                        artifact=artifact,
                        finalized_at=collected_at,
                    )
                    for artifact in artifacts
                ],
            )
        manifest = self.store.write_json(
            run_id=run.run_id,
            logical_path="manifest/manifest.json",
            payload=payload,
        )
        if self.run_store is not None:
            self.run_store.upsert_evidence_objects(
                run.run_id,
                [
                    _evidence_object_payload(
                        run_id=run.run_id,
                        run_root=run_root,
                        artifact=manifest,
                        finalized_at=collected_at,
                    )
                ],
            )
        return manifest

    def _existing_artifacts(self, run_root: Path) -> list[EvidenceArtifact]:
        if not run_root.exists():
            return []
        artifacts: list[EvidenceArtifact] = []
        for path in sorted(item for item in run_root.rglob("*") if item.is_file()):
            logical_path = path.relative_to(run_root).as_posix()
            if logical_path == "manifest/manifest.json":
                continue
            artifacts.append(
                EvidenceArtifact(
                    logical_path=logical_path,
                    path=path,
                    size_bytes=path.stat().st_size,
                    sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                    content_type=_content_type(path),
                )
            )
        return artifacts

    def _authorize_source_path(self, path: str, *, user: str) -> str:
        resolved = self.executor.realpath(path, timeout_seconds=self.timeout_seconds)
        roots = self._resolved_allowed_roots(user)
        for root in roots:
            if resolved == root or resolved.startswith(f"{root}/"):
                return resolved
        raise SlurmTransportError(f"evidence source path outside allowed roots: {path}")

    def _safe_source_path(self, path: str, *, user: str) -> SafePath:
        resolved = self.executor.realpath(path, timeout_seconds=self.timeout_seconds)
        for root in self._resolved_allowed_roots(user):
            if resolved == root or resolved.startswith(f"{root}/"):
                return SafePath(
                    original=path,
                    resolved=Path(resolved),
                    root=Path(root),
                )
        raise SlurmTransportError(f"evidence source path outside allowed roots: {path}")

    def _resolved_allowed_roots(self, user: str) -> list[str]:
        try:
            roots = resolve_owner_roots(self.allowed_roots, user=user)
        except OwnerRootPolicyError as exc:
            raise SlurmTransportError("evidence owner-root policy is invalid") from exc
        return [
            self.executor.realpath(root, timeout_seconds=self.timeout_seconds).rstrip("/")
            for root in roots
        ]

    def _require_evidence_transport(self) -> EvidenceTransport:
        if self.evidence_transport is None:
            raise SlurmTransportError("evidence transport is not configured")
        return self.evidence_transport

    def _transport_log_payload(
        self,
        identity: UserIdentity,
        path: SafePath,
    ) -> tuple[dict[str, Any], str | None, str | None]:
        transport = self._require_evidence_transport()
        try:
            stat_result = transport.stat(identity, path)
        except FileNotFoundError as exc:
            return {"status": "missing", "stderr": str(exc)}, None, None
        except PathPolicyError as exc:
            raise SlurmTransportError(
                f"evidence source path outside allowed roots: {path.original}"
            ) from exc

        if stat_result.kind != "regular file":
            return (
                {
                    "status": "missing",
                    "stderr": f"not a regular file: {stat_result.kind}",
                },
                None,
                None,
            )

        try:
            tail = transport.read_text_tail(
                identity,
                path,
                max_bytes=self.log_tail_bytes,
            )
        except FileNotFoundError as exc:
            return {"status": "missing", "stderr": str(exc)}, None, None
        except PathPolicyError as exc:
            raise SlurmTransportError(
                f"evidence source path outside allowed roots: {path.original}"
            ) from exc

        return (
            {
                "status": "present",
                "type": stat_result.kind,
                "size_bytes": stat_result.size_bytes,
                "mtime_epoch": stat_result.mtime_epoch,
                "owner": identity.username,
                "group": identity.username,
                "owner_readable": stat_result.owner_readable,
                "truncated": tail.truncated,
                "bytes_read": tail.bytes_read,
            },
            tail.tail,
            tail.sha256,
        )

    def _file_metadata(self, run: RunRecord, path: str) -> dict[str, Any]:
        result = self._run_user(
            run,
            ["stat", "-c", "%F|%s|%Y|%U|%G", "--", path],
        ).result
        if result.returncode != 0:
            return {"status": "missing", "stderr": result.stderr}
        kind, size, mtime_epoch, owner, group = _split_stat(result.stdout.strip())
        return {
            "status": "present",
            "type": kind,
            "size_bytes": int(size),
            "mtime_epoch": int(mtime_epoch),
            "owner": owner,
            "group": group,
        }

    def _tail_file(self, run: RunRecord, path: str) -> str | None:
        result = self._run_user(
            run,
            ["tail", "-c", str(self.log_tail_bytes), "--", path],
        ).result
        if result.returncode != 0:
            return None
        return result.stdout

    def _sha256_file(self, run: RunRecord, path: str) -> str | None:
        result = self._run_user(run, ["sha256sum", "--", path]).result
        if result.returncode != 0:
            return None
        return result.stdout.strip().split()[0]

    def _parse_inventory_rows(
        self,
        *,
        run: RunRecord,
        workdir: str,
        stdout: str,
        warnings: list[str],
        expected_outputs: list[str] | None = None,
        started_at_epoch: float | None = None,
        baseline_map: dict[str, dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        expected = expected_outputs or []
        baseline = baseline_map or {}
        files: list[dict[str, Any]] = []
        for line in stdout.splitlines():
            if not line.strip():
                continue
            parts = line.split("|")
            if len(parts) != 5:
                warnings.append(f"inventory row skipped: {line!r}")
                continue
            path, size, mtime_epoch, owner, group = parts
            try:
                authorized = self._authorize_source_path(path, user=run.owner)
            except SlurmTransportError as exc:
                warnings.append(str(exc))
                continue
            relative_path = posixpath.relpath(authorized, workdir)
            if relative_path == "." or relative_path.startswith("../"):
                warnings.append(f"inventory row outside workdir skipped: {path}")
                continue
            if _is_excluded_output(relative_path):
                continue
            mtime = float(mtime_epoch)
            is_expected = relative_path in expected
            baseline_entry = baseline.get(relative_path) if is_expected else None
            # Only hash files this run actually produced or declared; skip
            # preexisting non-expected files to avoid expensive sha256 on large
            # shared workdir leftovers.
            eligible = is_expected or (
                baseline_entry is None
                and (
                    started_at_epoch is None
                    or mtime > started_at_epoch
                )
            )
            sha = self._sha256_file(run, authorized) if eligible else None
            attribution = compute_file_attribution(
                mtime_epoch=mtime,
                started_at_epoch=started_at_epoch,
                relative_path=relative_path,
                expected_outputs=expected,
                baseline_entry=baseline_entry,
                is_expected=is_expected,
                final_sha256=sha,
            )
            files.append(
                {
                    "path": authorized,
                    "relative_path": relative_path,
                    "size_bytes": int(size),
                    "mtime_epoch": mtime,
                    "owner": owner,
                    "group": group,
                    "sha256": sha,
                    "attribution": attribution["attribution"],
                    "in_expected_outputs": attribution["in_expected_outputs"],
                    "final_sha256": sha,
                    "baseline_sha256": attribution["baseline_sha256"],
                }
            )
        return sorted(files, key=lambda item: item["relative_path"])

    def _run_user(
        self,
        run: RunRecord,
        argv: list[str],
        *,
        cwd: str | None = None,
    ) -> _ExecutedCommand:
        result = self.executor.run(
            argv,
            cwd=cwd,
            user=run.owner,
            timeout_seconds=self.timeout_seconds,
        )
        return _ExecutedCommand(argv=argv, result=result)


@dataclass(frozen=True)
class _ExecutedCommand:
    argv: list[str]
    result: CommandResult


def _require_job_id(run: RunRecord) -> str:
    if not run.job_id:
        raise SlurmTransportError(f"run has no job_id: {run.run_id}")
    return run.job_id


def _slurm_log_path(run: RunRecord, job_id: str, extension: str) -> str:
    return f"{run.workdir.rstrip('/')}/slurm-{job_id}.{extension}"


def _split_stat(line: str) -> tuple[str, str, str, str, str]:
    parts = line.split("|")
    if len(parts) != 5:
        raise SlurmTransportError(f"unexpected stat output: {line!r}")
    return parts[0], parts[1], parts[2], parts[3], parts[4]


def _parse_pipe_records(
    stdout: str,
    fields: tuple[str, ...],
) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        values = line.split("|")
        if len(values) != len(fields):
            raise ValueError(
                f"expected {len(fields)} pipe-delimited fields, got {len(values)}"
            )
        records.append(dict(zip(fields, (value.strip() for value in values), strict=True)))
    return records


def _content_type(path: Path) -> str:
    if path.suffix == ".json":
        return "application/json"
    if path.suffix == ".jsonl":
        return "application/jsonl"
    if path.suffix == ".sh":
        return "text/x-shellscript"
    if path.suffix == ".txt":
        return "text/plain"
    return "application/octet-stream"


def _read_json_or_none(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else None


def _load_baseline(run_root: Path) -> dict[str, dict[str, Any]]:
    """Load the pre-run baseline map of expected-output relative path -> entry.

    Returns an empty dict if no baseline was captured (e.g. stores were not
    injected at submit time); callers then fall back to mtime-only attribution.
    """
    baseline = _read_json_or_none(run_root / "baseline" / "baseline.json")
    if baseline is None:
        return {}
    entries = baseline.get("expected_outputs") or []
    if not isinstance(entries, list):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        path = entry.get("path")
        if isinstance(path, str):
            result[path] = entry
    return result


def _append_missing_expected(
    files: list[dict[str, Any]],
    expected_outputs: list[str],
    baseline_map: dict[str, dict[str, Any]],
) -> None:
    """Append ``missing`` placeholder rows for expected outputs not inventoried.

    Expected outputs that did not appear in the run's output directory are
    recorded with ``attribution == "missing"`` so the evaluation step can fail
    strict expected-output verification instead of silently passing.
    """
    present = {item.get("relative_path") for item in files}
    for relative_path in expected_outputs:
        if relative_path in present:
            continue
        baseline_entry = baseline_map.get(relative_path)
        baseline_sha = (
            baseline_entry.get("sha256") if baseline_entry is not None else None
        )
        # Round-8 P1-1: when the pre-run baseline probe failed (timeout /
        # path_invalid / path_too_long / error), the entry carries a ``status``
        # key and is unusable for attribution. Even if the expected output is
        # also absent from the final inventory, we must NOT report a plain
        # ``missing`` (which the verifier could misread as "legitimately not
        # produced"); emit ``baseline_unavailable`` so remediation fails closed.
        attribution = (
            "baseline_unavailable"
            if _baseline_entry_unavailable(baseline_entry)
            else "missing"
        )
        files.append(
            {
                "path": None,
                "relative_path": relative_path,
                "size_bytes": None,
                "mtime_epoch": None,
                "owner": None,
                "group": None,
                "sha256": None,
                "attribution": attribution,
                "in_expected_outputs": True,
                "final_sha256": None,
                "baseline_sha256": baseline_sha,
            }
        )
    files.sort(key=lambda item: str(item.get("relative_path")))


def _metadata_status(payload: dict[str, Any] | None) -> str | None:
    if payload is None:
        return None
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        return None
    return None if metadata.get("status") is None else str(metadata["status"])


def write_official_request_artifacts(
    *,
    store: EvidenceStore,
    run: RunRecord,
    collector: str,
) -> list[EvidenceArtifact]:
    collected_at = utc_now_iso()
    return [
        store.write_json(
            run_id=run.run_id,
            logical_path="run/request/resource-plan.json",
            payload={
                "schema": "pilot107.run.request.resource_plan.v1",
                "collector": collector,
                "collected_at": collected_at,
                "resource_plan": run.resource_plan,
            },
        ),
        store.write_text(
            run_id=run.run_id,
            logical_path="run/request/submitted-script.sbatch",
            content=run.script,
            content_type="text/x-shellscript",
        ),
        store.write_json(
            run_id=run.run_id,
            logical_path="run/request/sbatch-argv.json",
            payload=sbatch_argv_payload(
                collector=collector,
                run=run,
                collected_at=collected_at,
            ),
        ),
        store.write_json(
            run_id=run.run_id,
            logical_path="run/request/capability-profile-ref.json",
            payload=capability_profile_ref_payload(
                collector=collector,
                run=run,
                collected_at=collected_at,
            ),
        ),
    ]


def write_timeline_artifacts(
    *,
    store: EvidenceStore,
    run_store: RunStore | None,
    run: RunRecord,
) -> list[EvidenceArtifact]:
    if run_store is None:
        return []
    events = run_store.list_events(run.run_id)
    content = "".join(
        json.dumps(timeline_event_payload(event), sort_keys=True) + "\n" for event in events
    )
    return [
        store.write_text(
            run_id=run.run_id,
            logical_path="run/timeline/events.jsonl",
            content=content,
            content_type="application/jsonl",
        )
    ]


def sbatch_argv_payload(
    *,
    collector: str,
    run: RunRecord,
    collected_at: str,
) -> dict[str, Any]:
    argv = run.submit_response.get("argv")
    return {
        "schema": "pilot107.run.request.sbatch_argv.v1",
        "collector": collector,
        "collected_at": collected_at,
        "submit_strategy": run.submit_strategy,
        "availability": "known" if isinstance(argv, list) else "unavailable",
        "argv": argv if isinstance(argv, list) else None,
        "warning": None
        if isinstance(argv, list)
        else "submit backend did not expose sbatch argv",
    }


def capability_profile_ref_payload(
    *,
    collector: str,
    run: RunRecord,
    collected_at: str,
) -> dict[str, Any]:
    profile_id = run.resource_plan.get("capability_profile_id")
    return {
        "schema": "pilot107.run.request.capability_profile_ref.v1",
        "collector": collector,
        "collected_at": collected_at,
        "availability": "known" if profile_id else "unavailable",
        "profile_id": profile_id,
        "warning": None
        if profile_id
        else "capability profile id was not persisted with this run request",
    }


def basic_environment_payload(
    *,
    collector: str,
    run: RunRecord,
    collected_at: str,
    scope: str,
    results: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    env_stdout = str(results.get("env", {}).get("stdout") or "")
    return {
        "schema": "pilot107.run.environment.basic.v1",
        "collector": collector,
        "collected_at": collected_at,
        "scope": scope,
        "run_id": run.run_id,
        "owner": run.owner,
        "job_id": run.job_id,
        "workdir": run.workdir,
        "pwd": command_stdout(results, "pwd"),
        "whoami": command_stdout(results, "whoami"),
        "hostname": command_stdout(results, "hostname"),
        "date_iso": command_stdout(results, "date_iso"),
        "python_version": command_stdout(results, "python_version"),
        "python_path": command_stdout(results, "which_python"),
        "conda_default_env": env_value(env_stdout, "CONDA_DEFAULT_ENV"),
        "slurm_env": {
            name: value
            for name, value in env_pairs(env_stdout).items()
            if name.startswith("SLURM_")
        },
        "shared_workdir": {
            "path": run.workdir,
            "status": "not_evaluated",
            "warning": (
                "shared path status is established by WorkDirPreflight, "
                "not this runtime probe"
            ),
        },
    }


def command_stdout(results: dict[str, dict[str, Any]], name: str) -> str | None:
    value = results.get(name, {}).get("stdout")
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def env_pairs(stdout: str) -> dict[str, str]:
    pairs: dict[str, str] = {}
    for line in stdout.splitlines():
        if "=" not in line:
            continue
        name, value = line.split("=", 1)
        pairs[name] = value
    return pairs


def env_value(stdout: str, name: str) -> str | None:
    return env_pairs(stdout).get(name)


def requested_gpu_count(resource_plan: dict[str, Any]) -> int:
    for key in ("gpus_total", "gpus_per_node"):
        value = resource_plan.get(key)
        if value is None:
            continue
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            continue
    return 0


def gpu_probe_reason(result: CommandResult) -> str:
    text = f"{result.stdout}\n{result.stderr}".lower()
    if result.returncode == 127 or "not found" in text or "no such file" in text:
        return "command_not_found"
    if "failed to initialize nvml" in text:
        return "nvml_error"
    if "permission" in text:
        return "permission_denied"
    if "no devices were found" in text or "no device" in text:
        return "no_device"
    return "unavailable"


def redact_gpu_uuids(stdout: str) -> str:
    return re.sub(r"GPU-[0-9A-Fa-f-]{8,}", "GPU-<redacted>", stdout)


def timeline_event_payload(event: Any) -> dict[str, Any]:
    return {
        "schema": "pilot107.run.timeline.event.v1",
        "event_id": event.event_id,
        "run_id": event.run_id,
        "event_type": official_timeline_event_type(event.event_type),
        "raw_event_type": event.event_type,
        "created_at": event.created_at,
        "payload": event.payload,
    }


def official_timeline_event_type(event_type: str) -> str:
    mapping = {
        "run.created": "PREFLIGHT_PASSED",
        "run.submitting": "SUBMISSION_STARTED",
        "run.submitted": "JOB_ACCEPTED",
        "run.snapshot": "SLURM_STATE_OBSERVED",
        "collection.task_succeeded": "EVIDENCE_COLLECTION_PROGRESS",
        "diagnosis.updated": "DIAGNOSIS_COMPLETED",
    }
    return mapping.get(event_type, event_type.upper().replace(".", "_"))


def _filesystem_kind(path: Path) -> str:
    if path.is_file():
        return "regular file"
    if path.is_dir():
        return "directory"
    if path.is_symlink():
        return "symlink"
    return "special"


def _evidence_object_payload(
    *,
    run_id: str,
    run_root: Path,
    artifact: EvidenceArtifact,
    finalized_at: str,
) -> dict[str, Any]:
    return {
        "object_id": _evidence_object_id(run_id, artifact.logical_path),
        "category": artifact.logical_path.split("/", 1)[0],
        "logical_path": artifact.logical_path,
        "store_path": str((run_root / artifact.logical_path).resolve()),
        "source_uri": f"evidence://runs/{run_id}/{artifact.logical_path}",
        "sha256": artifact.sha256,
        "size_bytes": artifact.size_bytes,
        "mime_type": artifact.content_type,
        "collection_status": "collected",
        "collection_note": None,
        "mutable_during_run": _is_mutable_during_run(artifact.logical_path),
        "finalized_at": finalized_at,
    }


def _evidence_object_id(run_id: str, logical_path: str) -> str:
    digest = hashlib.sha256(f"{run_id}:{logical_path}".encode()).hexdigest()[:24]
    slug = re.sub(r"[^A-Za-z0-9]+", "_", logical_path).strip("_").lower()[:32]
    return f"ev_{slug}_{digest}" if slug else f"ev_{digest}"


def _is_mutable_during_run(logical_path: str) -> bool:
    return logical_path.startswith("logs/") or logical_path.startswith("outputs/")


def generated_execution_wrapper(run: RunRecord) -> str:
    return "\n".join(
        [
            "#!/usr/bin/env bash",
            "set +e",
            f'export PILOT107_RUN_ID="{run.run_id}"',
            'pilot107_start_epoch="$(date +%s)"',
            'echo "pilot107.wrapper.start_epoch=${pilot107_start_epoch}" >&2',
            'env | sort > "pilot107-env-${PILOT107_RUN_ID}.txt"',
            '# Phase 0A records this generated wrapper as evidence; later submit paths can set',
            '# PILOT107_USER_SCRIPT to execute the resolved script through this wrapper.',
            'if [ -n "${PILOT107_USER_SCRIPT:-}" ]; then',
            '  bash "${PILOT107_USER_SCRIPT}"',
            'else',
            '  echo "pilot107.wrapper.user_script_not_configured" >&2',
            'fi',
            'pilot107_exit_code="$?"',
            'pilot107_end_epoch="$(date +%s)"',
            'echo "pilot107.wrapper.end_epoch=${pilot107_end_epoch}" >&2',
            'echo "pilot107.wrapper.exit_code=${pilot107_exit_code}" >&2',
            'exit "${pilot107_exit_code}"',
            "",
        ]
    )


def _filter_environment(stdout: str) -> str:
    allowed_prefixes = ("PILOT107_", "SLURM_")
    allowed_names = {"HOME", "USER", "LOGNAME", "SHELL", "PATH", "PWD"}
    lines: list[str] = []
    for line in stdout.splitlines():
        name = line.split("=", 1)[0]
        if name in allowed_names or any(name.startswith(prefix) for prefix in allowed_prefixes):
            lines.append(line)
    return "\n".join(sorted(lines)) + ("\n" if lines else "")


def _is_excluded_output(relative_path: str) -> bool:
    name = posixpath.basename(relative_path)
    return (
        (name.startswith("pilot107-submit-") and name.endswith(".sbatch"))
        or name.startswith("slurm-") and (name.endswith(".out") or name.endswith(".err"))
    )


def _baseline_entry_unavailable(entry: Any) -> bool:
    """Return True if a baseline entry is unusable for attribution.

    A baseline entry written by ``_capture_baseline`` carries a ``status`` key
    (``timeout`` / ``path_invalid`` / ``path_too_long`` / ``error``) ONLY when
    the pre-run probe could not determine the file's actual state. Captured
    entries (the good ones) carry ``exists`` / ``sha256`` / ``mtime_epoch``
    with NO ``status`` key.

    Treating such an entry as ``baseline_missing`` (the historical bug) lets a
    pre-existing file appear as ``created`` / ``modified`` and falsely satisfy
    expected-output verification. Callers must instead emit the stricter
    ``baseline_unavailable`` attribution so remediation fails closed.
    """
    return isinstance(entry, dict) and bool(entry.get("status"))


def compute_file_attribution(
    *,
    mtime_epoch: float,
    started_at_epoch: float | None,
    relative_path: str,
    expected_outputs: list[str],
    baseline_entry: dict[str, Any] | None = None,
    is_expected: bool = False,
    final_sha256: str | None = None,
) -> dict[str, Any]:
    """Attribute a single inventory file to a run.

    Pure helper kept module-level for direct unit testing.

    Two classification regimes:

    * **Expected outputs with a baseline** (``is_expected`` and
      ``baseline_entry`` both provided): strict baseline-vs-final comparison —
      ``created`` (baseline did not exist, now present), ``modified`` (baseline
      sha256 differs from final), ``unchanged`` (baseline sha256 equals final),
      ``missing`` (expected output not present in the current inventory), or
      ``baseline_unavailable`` (baseline entry was written with a probe failure
      ``status`` such as ``timeout`` / ``path_invalid`` / ``path_too_long`` /
      ``error`` — the pre-run state is unknown so we must not pretend the run
      produced the file). ``final_sha256`` is the hash computed by the caller
      for the current file; pass ``None`` when the file is absent (so the
      helper can return ``missing`` for expected outputs that did not appear).
    * **Everything else** (non-expected files, or no baseline): mtime-based
      fallback — ``created_by_run`` (mtime after run start), ``preexisting``,
      or ``unknown`` (no run-start timestamp).

    Returns a dict with ``attribution``, ``in_expected_outputs`` (bool),
    ``baseline_sha256`` (the baseline sha256 or None), and ``final_sha256``
    (echoed back for caller convenience).
    """
    in_expected = relative_path in expected_outputs or is_expected
    # baseline_sha is only meaningful for expected outputs (the baseline tracks
    # expected-output state before the run). For non-expected files, baseline
    # capture doesn't apply, so baseline_sha256 stays None and attribution
    # falls back to mtime-based logic.
    baseline_sha = (
        baseline_entry.get("sha256")
        if (baseline_entry is not None and in_expected)
        else None
    )
    if in_expected and baseline_entry is not None:
        # Round-8 P1-1: a baseline entry carrying a probe-failure ``status``
        # (timeout / path_invalid / path_too_long / error) is NOT a usable
        # baseline. The pre-run state is unknown, so we must not compare
        # exists/sha256 — doing so would let a pre-existing file masquerade as
        # ``created`` / ``modified`` and falsely satisfy expected-output
        # verification. Emit ``baseline_unavailable`` so remediation fails
        # closed to EXECUTION_SUCCESS_UNVERIFIED (BLOCKED) instead of upgrading
        # to VERIFIED_SUCCESS.
        if _baseline_entry_unavailable(baseline_entry):
            attribution = "baseline_unavailable"
        else:
            baseline_exists = bool(baseline_entry.get("exists"))
            current_exists = final_sha256 is not None or mtime_epoch is not None
            if not baseline_exists and current_exists:
                attribution = "created"
            elif not current_exists:
                attribution = "missing"
            elif (
                baseline_sha is not None
                and final_sha256 is not None
                and baseline_sha != final_sha256
            ):
                attribution = "modified"
            else:
                attribution = "unchanged"
    else:
        if started_at_epoch is None:
            attribution = "unknown"
        elif mtime_epoch > started_at_epoch:
            attribution = "created_by_run"
        else:
            attribution = "preexisting"
    return {
        "attribution": attribution,
        "in_expected_outputs": in_expected,
        "baseline_sha256": baseline_sha,
        "final_sha256": final_sha256,
    }


def _iso_to_epoch(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return datetime.fromisoformat(value).timestamp()
    except (ValueError, TypeError):
        return None


def _attribution_summary(files: list[dict[str, Any]]) -> dict[str, int]:
    summary: dict[str, int] = {
        "created_by_run": 0,
        "preexisting": 0,
        "unknown": 0,
        "created": 0,
        "modified": 0,
        "unchanged": 0,
        "missing": 0,
        "baseline_unavailable": 0,
    }
    for item in files:
        attribution = item.get("attribution", "unknown")
        if attribution in summary:
            summary[attribution] += 1
        else:
            summary[item.get("attribution", "unknown")] = 1
    return summary
