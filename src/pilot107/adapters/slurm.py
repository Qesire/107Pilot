"""Slurm backend contracts and local/simulator implementations."""

import base64
import hashlib
import hmac
import json
import os
import posixpath
import re
import shutil
import subprocess
import tarfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from pilot107.core.path_policy import OwnerRootPolicyError, resolve_owner_roots
from pilot107.core.paths import authorize_path
from pilot107.core.resources import PreflightSeverity, ResourcePlan, validate_resource_plan
from pilot107.core.rest_semantics import RestSemanticLevel, check_slurm_rest_semantics
from pilot107.core.states import RunState, normalize_slurm_state

_SAFE_SLURM_VALUE = re.compile(r"^[A-Za-z0-9_.:+/@=-]+$")
_JOB_ID = re.compile(r"^[A-Za-z0-9_.+-]+$")


class SubmissionStrategy(StrEnum):
    REST_NATIVE = "rest_native"
    COMMAND = "command"
    IN_MEMORY = "in_memory"
    DEMO = "demo"


class RestAuthStyle(StrEnum):
    BEARER = "bearer"
    SLURM_HEADERS = "slurm_headers"


class SlurmBackendError(RuntimeError):
    """Base error for backend adapter failures."""


class SlurmAuthError(SlurmBackendError):
    """Raised when a user attempts to access a job they do not own."""


class SlurmSubmissionRejected(SlurmBackendError):
    """Raised when a job cannot be submitted due to validated input."""


class SlurmTransportError(SlurmBackendError):
    """Raised when the backend transport cannot complete a request."""


class SlurmBackendOwnershipError(SlurmTransportError):
    """Raised when a persisted job belongs to a different backend boundary.

    This is not a transient transport failure.  Retrying it forever leaves a
    worker unhealthy after a safe backend migration (for example from the
    in-memory test backend to the persistent demo backend).
    """


@dataclass(frozen=True)
class SubmitIntent:
    user: str
    workdir: Path
    script: str
    resource_plan: ResourcePlan
    idempotency_key: str | None = None
    dependency_job_ids: tuple[str, ...] = ()
    job_name: str | None = None


@dataclass(frozen=True)
class SubmitReceipt:
    job_id: str
    run_state: RunState
    strategy: SubmissionStrategy
    raw_response: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class JobSnapshot:
    job_id: str
    owner: str
    run_state: RunState
    raw_state_flags: list[str]
    exit_code: str | None = None
    reason: str | None = None
    stdout_path: Path | None = None
    stderr_path: Path | None = None
    raw_response: dict[str, Any] = field(default_factory=dict)


class SlurmBackend(Protocol):
    def submit(self, intent: SubmitIntent) -> SubmitReceipt:
        """Submit a job and return a durable Slurm job reference."""

    def get_job(self, *, user: str, job_id: str) -> JobSnapshot:
        """Read a job snapshot for an authorized user."""

    def cancel(self, *, user: str, job_id: str) -> JobSnapshot:
        """Cancel a job for an authorized user and return the resulting snapshot."""


SlurmControlBackend = SlurmBackend


@dataclass(frozen=True)
class HttpResponse:
    status: int
    payload: dict[str, Any]


class HttpTransport(Protocol):
    def request(
        self,
        method: str,
        path: str,
        *,
        token: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> HttpResponse:
        """Send a JSON request to slurmrestd."""


class UrllibHttpTransport:
    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: float = 10.0,
        auth_style: RestAuthStyle = RestAuthStyle.BEARER,
        slurm_username: str | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.auth_style = auth_style
        self.slurm_username = slurm_username

    def request(
        self,
        method: str,
        path: str,
        *,
        token: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> HttpResponse:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if token and self.auth_style == RestAuthStyle.BEARER:
            headers["Authorization"] = f"Bearer {token}"
        elif token and self.auth_style == RestAuthStyle.SLURM_HEADERS:
            if not self.slurm_username:
                raise SlurmTransportError("slurm_headers auth requires slurm_username")
            headers["X-SLURM-USER-NAME"] = self.slurm_username
            headers["X-SLURM-USER-TOKEN"] = token
        request = urllib.request.Request(
            url=f"{self.base_url}{path}",
            data=body,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read()
                parsed = json.loads(raw.decode("utf-8")) if raw else {}
                return HttpResponse(status=response.status, payload=parsed)
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                parsed = json.loads(raw.decode("utf-8")) if raw else {}
            except json.JSONDecodeError:
                parsed = {"errors": [{"description": raw.decode("utf-8", errors="replace")}]}
            return HttpResponse(status=exc.code, payload=parsed)
        except OSError as exc:
            raise SlurmTransportError(str(exc)) from exc


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


class CommandRunner(Protocol):
    def run(
        self,
        argv: list[str],
        *,
        cwd: Path | None = None,
        timeout_seconds: float,
    ) -> CommandResult:
        """Run an allowed command without invoking a shell."""


class SubprocessCommandRunner:
    def run(
        self,
        argv: list[str],
        *,
        cwd: Path | None = None,
        timeout_seconds: float,
    ) -> CommandResult:
        completed = subprocess.run(
            argv,
            cwd=str(cwd) if cwd else None,
            timeout=timeout_seconds,
            check=False,
            text=True,
            capture_output=True,
        )
        return CommandResult(
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )


@dataclass(frozen=True)
class DockerComposeTarget:
    compose_file: Path
    env_file: Path
    workdir: Path
    service: str = "login-node-sim"


class DockerComposeExecutor:
    """Run structured commands inside a Docker Compose service."""

    def __init__(self, target: DockerComposeTarget) -> None:
        self.target = target

    def build_exec_argv(
        self,
        argv: list[str],
        *,
        cwd: str | None = None,
        user: str | None = None,
    ) -> list[str]:
        command = [
            "docker",
            "compose",
            "--env-file",
            str(self.target.env_file),
            "-f",
            str(self.target.compose_file),
            "exec",
            "-T",
        ]
        if user:
            command.extend(["--user", user])
        if cwd:
            command.extend(["--workdir", cwd])
        command.append(self.target.service)
        command.extend(argv)
        return command

    def run(
        self,
        argv: list[str],
        *,
        cwd: str | None = None,
        user: str | None = None,
        stdin: str | None = None,
        timeout_seconds: float = 10.0,
    ) -> CommandResult:
        completed = subprocess.run(
            self.build_exec_argv(argv, cwd=cwd, user=user),
            cwd=str(self.target.workdir),
            input=stdin,
            timeout=timeout_seconds,
            check=False,
            text=True,
            capture_output=True,
        )
        return CommandResult(
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )

    def realpath(self, path: str, *, timeout_seconds: float = 10.0) -> str:
        result = self.run(["realpath", "-m", path], timeout_seconds=timeout_seconds)
        if result.returncode != 0:
            raise SlurmTransportError(result.stderr.strip() or "realpath failed")
        return result.stdout.strip()

    def write_text(
        self,
        *,
        path: str,
        content: str,
        owner: str,
        timeout_seconds: float = 10.0,
    ) -> None:
        write_result = self.run(["tee", path], stdin=content, timeout_seconds=timeout_seconds)
        if write_result.returncode != 0:
            raise SlurmTransportError(write_result.stderr.strip() or "container write failed")
        chown_result = self.run(
            ["chown", f"{owner}:{owner}", path], timeout_seconds=timeout_seconds
        )
        if chown_result.returncode != 0:
            raise SlurmTransportError(chown_result.stderr.strip() or "container chown failed")


class SimulatorExecutor(Protocol):
    def run(
        self,
        argv: list[str],
        *,
        cwd: str | None = None,
        user: str | None = None,
        stdin: str | None = None,
        timeout_seconds: float = 10.0,
    ) -> CommandResult:
        """Run a structured command in the simulator execution boundary."""

    def realpath(self, path: str, *, timeout_seconds: float = 10.0) -> str:
        """Resolve a container path without following host paths."""

    def write_text(
        self,
        *,
        path: str,
        content: str,
        owner: str,
        timeout_seconds: float = 10.0,
    ) -> None:
        """Write a text file in the simulator and assign ownership."""


@dataclass(frozen=True)
class FileEntry:
    name: str
    type: str
    size: int
    mtime: int


@dataclass(frozen=True)
class FileSearchRequest:
    owner: str
    root: str
    q: str
    kind: str
    size_min: int | None
    size_max: int | None
    mtime_from: int | None
    mtime_to: int | None
    limit: int
    cursor: str | None
    scan_limit: int
    time_limit_ms: int


@dataclass(frozen=True)
class FileSearchEntry:
    path: str
    relative_path: str
    type: str
    size: int
    mtime: int


@dataclass(frozen=True)
class FileSearchPage:
    items: tuple[FileSearchEntry, ...]
    incomplete: bool
    next_cursor: str | None
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class FileStat:
    path: str
    type: str
    size: int
    mtime: int


@dataclass(frozen=True)
class DiskUsage:
    """Recursive apparent size of a path plus the filesystem total.

    ``total_bytes`` is ``None`` when the backend cannot determine the
    containing filesystem size (e.g. some remote gateways).
    """

    path: str
    used_bytes: int
    total_bytes: int | None


class FileOpsExecutor(Protocol):
    """Binary file primitives for transferring user files to the cluster.

    These complement :class:`SimulatorExecutor` (which only writes small text
    scripts).  All paths are authorized against the owner's allowed roots by
    the concrete backend before any byte is moved.
    """

    def write_bytes_chunk(
        self,
        *,
        path: str,
        data_b64: str,
        offset: int,
        owner: str,
        timeout_seconds: float = 30.0,
    ) -> int:
        """Write a base64 chunk at ``offset`` (0 truncates, <0 appends)."""

    def read_bytes_chunk(
        self,
        *,
        path: str,
        offset: int,
        length: int,
        owner: str,
        timeout_seconds: float = 30.0,
    ) -> tuple[str, int]:
        """Read up to ``length`` bytes; return ``(data_b64, total_size)``."""

    def file_sha256(
        self, *, path: str, owner: str, timeout_seconds: float = 30.0
    ) -> str:
        """Return the hex sha256 of a remote file."""

    def list_dir(
        self, *, path: str, owner: str, timeout_seconds: float = 30.0
    ) -> list[FileEntry]:
        """List a directory's entries."""

    def search_files(
        self,
        *,
        root: str,
        q: str,
        kind: str,
        size_min: int | None,
        size_max: int | None,
        mtime_from: int | None,
        mtime_to: int | None,
        limit: int,
        cursor: str | None,
        scan_limit: int,
        time_limit_ms: int,
        owner: str,
        timeout_seconds: float = 30.0,
    ) -> FileSearchPage:
        """Search names and relative paths within an authorized bounded root."""

    def make_dir(
        self, *, path: str, owner: str, timeout_seconds: float = 30.0
    ) -> None:
        """Create a directory (and parents)."""

    def remove_path(
        self, *, path: str, owner: str, timeout_seconds: float = 30.0
    ) -> None:
        """Remove a file or directory tree."""

    def rename_path(
        self,
        *,
        path: str,
        new_path: str,
        owner: str,
        overwrite: bool = False,
        timeout_seconds: float = 30.0,
    ) -> None:
        """Rename or move ``path`` to ``new_path``.

        Both endpoints are authorized against the owner's roots. When
        ``overwrite`` is false an existing target is rejected.
        """

    def copy_entries(
        self,
        *,
        paths: list[str],
        dest_dir: str,
        owner: str,
        timeout_seconds: float = 120.0,
    ) -> list[str]:
        """Copy entries into ``dest_dir`` (overwrite); return copied paths."""

    def create_file(
        self,
        *,
        dir_path: str,
        name: str,
        owner: str,
        timeout_seconds: float = 30.0,
    ) -> str:
        """Create an empty file; reject when the name is already taken."""

    def stat_path(
        self, *, path: str, owner: str, timeout_seconds: float = 30.0
    ) -> FileStat:
        """Return metadata for a path."""

    def disk_usage(
        self, *, path: str, owner: str, timeout_seconds: float = 30.0
    ) -> DiskUsage:
        """Return recursive used bytes for ``path`` plus filesystem total."""

    def extract_archive(
        self,
        *,
        archive_path: str,
        dest_dir: str,
        owner: str,
        timeout_seconds: float = 120.0,
    ) -> int:
        """Safely extract a tar archive; return the member count."""

    def create_archive(
        self,
        *,
        paths: list[str],
        dest_dir: str,
        archive_name: str,
        owner: str,
        timeout_seconds: float = 120.0,
    ) -> tuple[str, int]:
        """Pack paths into a tar.gz under dest_dir; return (archive_path, size)."""


class HttpCommandGatewayExecutor:
    """Run simulator commands through a narrow HTTP command gateway."""

    def __init__(
        self,
        *,
        base_url: str,
        token: str | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout_seconds = timeout_seconds

    def run(
        self,
        argv: list[str],
        *,
        cwd: str | None = None,
        user: str | None = None,
        stdin: str | None = None,
        timeout_seconds: float = 10.0,
    ) -> CommandResult:
        payload = self._request(
            "/run",
            {
                "argv": argv,
                "cwd": cwd,
                "user": user,
                "stdin": stdin,
                "timeout_seconds": timeout_seconds,
            },
            timeout_seconds=timeout_seconds,
        )
        return CommandResult(
            returncode=int(payload.get("returncode", 1)),
            stdout=str(payload.get("stdout", "")),
            stderr=str(payload.get("stderr", "")),
        )

    def realpath(self, path: str, *, timeout_seconds: float = 10.0) -> str:
        payload = self._request(
            "/realpath",
            {"path": path, "timeout_seconds": timeout_seconds},
            timeout_seconds=timeout_seconds,
        )
        value = payload.get("path")
        if not isinstance(value, str) or not value:
            raise SlurmTransportError("gateway realpath response missing path")
        return value

    def write_text(
        self,
        *,
        path: str,
        content: str,
        owner: str,
        timeout_seconds: float = 10.0,
    ) -> None:
        self._request(
            "/write_text",
            {
                "path": path,
                "content": content,
                "owner": owner,
                "timeout_seconds": timeout_seconds,
            },
            timeout_seconds=timeout_seconds,
        )

    def write_bytes_chunk(
        self,
        *,
        path: str,
        data_b64: str,
        offset: int,
        owner: str,
        timeout_seconds: float = 30.0,
    ) -> int:
        payload = self._request(
            "/write_bytes",
            {
                "path": path,
                "data_b64": data_b64,
                "offset": offset,
                "owner": owner,
                "timeout_seconds": timeout_seconds,
            },
            timeout_seconds=timeout_seconds,
        )
        return int(payload.get("size", 0))

    def read_bytes_chunk(
        self,
        *,
        path: str,
        offset: int,
        length: int,
        owner: str,
        timeout_seconds: float = 30.0,
    ) -> tuple[str, int]:
        payload = self._request(
            "/read_bytes",
            {
                "path": path,
                "offset": offset,
                "length": length,
                "owner": owner,
                "timeout_seconds": timeout_seconds,
            },
            timeout_seconds=timeout_seconds,
        )
        return str(payload.get("data_b64", "")), int(payload.get("size", 0))

    def file_sha256(
        self, *, path: str, owner: str, timeout_seconds: float = 30.0
    ) -> str:
        payload = self._request(
            "/sha256",
            {"path": path, "owner": owner, "timeout_seconds": timeout_seconds},
            timeout_seconds=timeout_seconds,
        )
        value = payload.get("sha256")
        if not isinstance(value, str) or not value:
            raise SlurmTransportError("gateway sha256 response missing digest")
        return value

    def list_dir(
        self, *, path: str, owner: str, timeout_seconds: float = 30.0
    ) -> list[FileEntry]:
        payload = self._request(
            "/list_dir",
            {"path": path, "owner": owner, "timeout_seconds": timeout_seconds},
            timeout_seconds=timeout_seconds,
        )
        entries = payload.get("entries")
        if not isinstance(entries, list):
            raise SlurmTransportError("gateway list_dir response missing entries")
        return [
            FileEntry(
                name=str(item.get("name", "")),
                type=str(item.get("type", "other")),
                size=int(item.get("size", 0)),
                mtime=int(item.get("mtime", 0)),
            )
            for item in entries
            if isinstance(item, dict)
        ]

    def search_files(
        self,
        *,
        root: str,
        q: str,
        kind: str,
        size_min: int | None,
        size_max: int | None,
        mtime_from: int | None,
        mtime_to: int | None,
        limit: int,
        cursor: str | None,
        scan_limit: int,
        time_limit_ms: int,
        owner: str,
        timeout_seconds: float = 30.0,
    ) -> FileSearchPage:
        payload = self._request(
            "/search_files",
            {
                "root": root,
                "q": q,
                "kind": kind,
                "size_min": size_min,
                "size_max": size_max,
                "mtime_from": mtime_from,
                "mtime_to": mtime_to,
                "limit": limit,
                "cursor": cursor,
                "scan_limit": scan_limit,
                "time_limit_ms": time_limit_ms,
                "owner": owner,
                "timeout_seconds": timeout_seconds,
            },
            timeout_seconds=timeout_seconds,
        )
        raw_items = payload.get("items")
        if not isinstance(raw_items, list):
            raise SlurmTransportError("gateway search_files response missing items")
        raw_warnings = payload.get("warnings", [])
        if not isinstance(raw_warnings, list):
            raise SlurmTransportError(
                "gateway search_files response has invalid warnings"
            )
        raw_cursor = payload.get("next_cursor")
        if raw_cursor is not None and not isinstance(raw_cursor, str):
            raise SlurmTransportError(
                "gateway search_files response has invalid cursor"
            )
        return FileSearchPage(
            items=tuple(
                FileSearchEntry(
                    path=str(item.get("path", "")),
                    relative_path=str(item.get("relative_path", "")),
                    type=str(item.get("type", "other")),
                    size=int(item.get("size", 0)),
                    mtime=int(item.get("mtime", 0)),
                )
                for item in raw_items
                if isinstance(item, dict)
            ),
            incomplete=bool(payload.get("incomplete", False)),
            next_cursor=raw_cursor,
            warnings=tuple(str(item) for item in raw_warnings),
        )

    def make_dir(
        self, *, path: str, owner: str, timeout_seconds: float = 30.0
    ) -> None:
        self._request(
            "/mkdir",
            {"path": path, "owner": owner, "timeout_seconds": timeout_seconds},
            timeout_seconds=timeout_seconds,
        )

    def remove_path(
        self, *, path: str, owner: str, timeout_seconds: float = 30.0
    ) -> None:
        self._request(
            "/remove",
            {"path": path, "owner": owner, "timeout_seconds": timeout_seconds},
            timeout_seconds=timeout_seconds,
        )

    def rename_path(
        self,
        *,
        path: str,
        new_path: str,
        owner: str,
        overwrite: bool = False,
        timeout_seconds: float = 30.0,
    ) -> None:
        self._request(
            "/rename",
            {
                "path": path,
                "new_path": new_path,
                "owner": owner,
                "overwrite": overwrite,
                "timeout_seconds": timeout_seconds,
            },
            timeout_seconds=timeout_seconds,
        )

    def copy_entries(
        self,
        *,
        paths: list[str],
        dest_dir: str,
        owner: str,
        timeout_seconds: float = 120.0,
    ) -> list[str]:
        payload = self._request(
            "/copy",
            {
                "paths": paths,
                "dest_dir": dest_dir,
                "owner": owner,
                "timeout_seconds": timeout_seconds,
            },
            timeout_seconds=timeout_seconds,
        )
        copied = payload.get("copied", [])
        return [str(item) for item in copied] if isinstance(copied, list) else []

    def create_file(
        self,
        *,
        dir_path: str,
        name: str,
        owner: str,
        timeout_seconds: float = 30.0,
    ) -> str:
        payload = self._request(
            "/create-file",
            {
                "dir": dir_path,
                "name": name,
                "owner": owner,
                "timeout_seconds": timeout_seconds,
            },
            timeout_seconds=timeout_seconds,
        )
        return str(payload.get("path", ""))

    def stat_path(
        self, *, path: str, owner: str, timeout_seconds: float = 30.0
    ) -> FileStat:
        payload = self._request(
            "/stat",
            {"path": path, "owner": owner, "timeout_seconds": timeout_seconds},
            timeout_seconds=timeout_seconds,
        )
        return FileStat(
            path=str(payload.get("path", path)),
            type=str(payload.get("type", "other")),
            size=int(payload.get("size", 0)),
            mtime=int(payload.get("mtime", 0)),
        )

    def disk_usage(
        self, *, path: str, owner: str, timeout_seconds: float = 30.0
    ) -> DiskUsage:
        payload = self._request(
            "/disk_usage",
            {"path": path, "owner": owner, "timeout_seconds": timeout_seconds},
            timeout_seconds=timeout_seconds,
        )
        total = payload.get("total_bytes")
        return DiskUsage(
            path=str(payload.get("path", path)),
            used_bytes=int(payload.get("used_bytes", 0)),
            total_bytes=None if total is None else int(total),
        )

    def extract_archive(
        self,
        *,
        archive_path: str,
        dest_dir: str,
        owner: str,
        timeout_seconds: float = 120.0,
    ) -> int:
        payload = self._request(
            "/extract",
            {
                "path": archive_path,
                "dest_dir": dest_dir,
                "owner": owner,
                "timeout_seconds": timeout_seconds,
            },
            timeout_seconds=timeout_seconds,
        )
        return int(payload.get("members", 0))

    def create_archive(
        self,
        *,
        paths: list[str],
        dest_dir: str,
        archive_name: str,
        owner: str,
        timeout_seconds: float = 120.0,
    ) -> tuple[str, int]:
        payload = self._request(
            "/archive",
            {
                "paths": paths,
                "dest_dir": dest_dir,
                "archive_name": archive_name,
                "owner": owner,
                "timeout_seconds": timeout_seconds,
            },
            timeout_seconds=timeout_seconds,
        )
        return str(payload.get("path", "")), int(payload.get("size", 0))

    def _request(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        body = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(
            url=f"{self.base_url}{path}",
            data=body,
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                request,
                timeout=max(timeout_seconds, self.timeout_seconds),
            ) as response:
                raw = response.read()
                parsed = json.loads(raw.decode("utf-8")) if raw else {}
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                parsed = json.loads(raw.decode("utf-8")) if raw else {}
            except json.JSONDecodeError:
                parsed = {"error": raw.decode("utf-8", errors="replace")}
            raise SlurmTransportError(f"gateway {path} failed: {parsed!r}") from exc
        except OSError as exc:
            raise SlurmTransportError(f"gateway {path} failed: {exc}") from exc
        if not isinstance(parsed, dict):
            raise SlurmTransportError(f"gateway {path} returned non-object JSON")
        return parsed


def _directory_apparent_size(root: Path) -> int:
    """Sum regular-file apparent sizes under ``root`` without following links.

    Close enough to ``du -sb`` for a storage-usage card: symlinks are counted
    by their own size and never traversed, and unreadable entries are skipped
    rather than aborting the whole walk.
    """

    total = 0
    stack: list[Path] = [root]
    while stack:
        current = stack.pop()
        try:
            handle = os.scandir(current)
        except OSError:
            continue
        with handle:
            for entry in handle:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(Path(entry.path))
                        continue
                    total += entry.stat(follow_symlinks=False).st_size
                except OSError:
                    continue
    return total


def _encode_local_search_cursor(payload: dict[str, Any], key: bytes) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    body = base64.urlsafe_b64encode(raw).rstrip(b"=")
    signature = hmac.new(key, body, hashlib.sha256).digest()
    return (
        body.decode("ascii")
        + "."
        + base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")
    )


def _decode_local_search_cursor(cursor: str, key: bytes) -> dict[str, Any]:
    try:
        body_text, signature_text = cursor.split(".", 1)
        body = body_text.encode("ascii")
        signature = base64.urlsafe_b64decode(
            signature_text + "=" * (-len(signature_text) % 4)
        )
        expected = hmac.new(key, body, hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError("signature mismatch")
        raw = base64.urlsafe_b64decode(body + b"=" * (-len(body) % 4))
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise SlurmSubmissionRejected("invalid search cursor") from exc
    if not isinstance(payload, dict):
        raise SlurmSubmissionRejected("invalid search cursor")
    return payload


def _validate_local_search_stack(raw_stack: list[Any]) -> list[dict[str, Any]]:
    stack: list[dict[str, Any]] = []
    for frame in raw_stack:
        if not isinstance(frame, dict):
            raise SlurmSubmissionRejected("invalid search cursor")
        relative_dir = frame.get("relative_dir")
        index = frame.get("index")
        if (
            not isinstance(relative_dir, str)
            or relative_dir.startswith("/")
            or ".." in Path(relative_dir).parts
            or not isinstance(index, int)
            or index < 0
        ):
            raise SlurmSubmissionRejected("invalid search cursor")
        stack.append({"relative_dir": relative_dir, "index": index})
    return stack


class LocalFileOpsExecutor:
    """File primitives against the local filesystem (tests / local backend).

    Mirrors the command-gateway semantics: every path is authorized against
    ``allowed_roots`` before any byte is moved, and archive extraction rejects
    members that would escape the destination.
    """

    def __init__(self, *, allowed_roots: list[str]) -> None:
        self.allowed_roots = [root.rstrip("/") or "/" for root in allowed_roots]
        self._search_cursor_key = os.urandom(32)

    def _authorize(self, path: str) -> Path:
        resolved = Path(path).resolve()
        for root in self.allowed_roots:
            resolved_root = Path(root).resolve()
            if resolved == resolved_root or str(resolved).startswith(
                f"{resolved_root}/"
            ):
                return resolved
        raise SlurmSubmissionRejected(f"path outside allowed roots: {path}")

    def write_bytes_chunk(
        self,
        *,
        path: str,
        data_b64: str,
        offset: int,
        owner: str,
        timeout_seconds: float = 30.0,
    ) -> int:
        target = self._authorize(path)
        data = base64.b64decode(data_b64, validate=True)
        if not target.parent.exists():
            raise SlurmTransportError(
                f"parent directory does not exist: {target.parent}"
            )
        if offset < 0:
            mode = "ab"
        elif offset == 0:
            mode = "wb"
        else:
            if not target.exists():
                raise SlurmTransportError("cannot write at offset before file exists")
            current = target.stat().st_size
            if offset != current:
                raise SlurmTransportError(
                    f"write offset {offset} does not match file size {current}"
                )
            mode = "r+b"
        with open(target, mode) as handle:
            if offset > 0:
                handle.seek(offset)
            handle.write(data)
        return target.stat().st_size

    def read_bytes_chunk(
        self,
        *,
        path: str,
        offset: int,
        length: int,
        owner: str,
        timeout_seconds: float = 30.0,
    ) -> tuple[str, int]:
        target = self._authorize(path)
        if not target.is_file():
            raise SlurmTransportError(f"not a regular file: {path}")
        if offset < 0 or length <= 0:
            raise SlurmTransportError("offset must be >= 0 and length positive")
        size = target.stat().st_size
        with open(target, "rb") as handle:
            handle.seek(offset)
            data = handle.read(length)
        return base64.b64encode(data).decode("ascii"), size

    def file_sha256(
        self, *, path: str, owner: str, timeout_seconds: float = 30.0
    ) -> str:
        target = self._authorize(path)
        if not target.is_file():
            raise SlurmTransportError(f"not a regular file: {path}")
        digest = hashlib.sha256()
        with open(target, "rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    def list_dir(
        self, *, path: str, owner: str, timeout_seconds: float = 30.0
    ) -> list[FileEntry]:
        target = self._authorize(path)
        if not target.is_dir():
            raise SlurmTransportError(f"not a directory: {path}")
        entries: list[FileEntry] = []
        for entry in sorted(target.iterdir(), key=lambda item: item.name):
            info = entry.lstat()
            if entry.is_symlink():
                kind = "symlink"
            elif entry.is_dir():
                kind = "dir"
            elif entry.is_file():
                kind = "file"
            else:
                kind = "other"
            entries.append(
                FileEntry(
                    name=entry.name,
                    type=kind,
                    size=info.st_size,
                    mtime=int(info.st_mtime),
                )
            )
        return entries

    def search_files(
        self,
        *,
        root: str,
        q: str,
        kind: str,
        size_min: int | None,
        size_max: int | None,
        mtime_from: int | None,
        mtime_to: int | None,
        limit: int,
        cursor: str | None,
        scan_limit: int,
        time_limit_ms: int,
        owner: str,
        timeout_seconds: float = 30.0,
    ) -> FileSearchPage:
        del timeout_seconds
        target = self._authorize(root)
        if not target.is_dir():
            raise SlurmTransportError(f"not a directory: {root}")
        normalized_query = q.strip().casefold()
        if kind not in {"file", "directory", "all"}:
            raise SlurmSubmissionRejected("kind must be file, directory, or all")
        if not 1 <= limit <= 100:
            raise SlurmSubmissionRejected("limit must be between 1 and 100")
        if not 1 <= scan_limit <= 100_000:
            raise SlurmSubmissionRejected("scan_limit must be between 1 and 100000")
        if not 1 <= time_limit_ms <= 10_000:
            raise SlurmSubmissionRejected("time_limit_ms must be between 1 and 10000")
        for name, value in (
            ("size_min", size_min),
            ("size_max", size_max),
            ("mtime_from", mtime_from),
            ("mtime_to", mtime_to),
        ):
            if value is not None and value < 0:
                raise SlurmSubmissionRejected(f"{name} must be non-negative")
        if size_min is not None and size_max is not None and size_min > size_max:
            raise SlurmSubmissionRejected("size_min cannot exceed size_max")
        if mtime_from is not None and mtime_to is not None and mtime_from > mtime_to:
            raise SlurmSubmissionRejected("mtime_from cannot exceed mtime_to")
        binding = {
            "owner": owner,
            "root": str(target),
            "q": normalized_query,
            "kind": kind,
            "size_min": size_min,
            "size_max": size_max,
            "mtime_from": mtime_from,
            "mtime_to": mtime_to,
        }
        if cursor:
            state = _decode_local_search_cursor(cursor, self._search_cursor_key)
            if state.get("binding") != binding:
                raise SlurmSubmissionRejected("search cursor does not match request")
            raw_stack = state.get("stack")
            if not isinstance(raw_stack, list):
                raise SlurmSubmissionRejected("invalid search cursor")
            stack = _validate_local_search_stack(raw_stack)
        else:
            stack = [{"relative_dir": "", "index": 0}]

        started = time.monotonic()
        scanned = 0
        items: list[FileSearchEntry] = []
        warnings: list[str] = []
        while stack and len(items) < limit:
            if (
                scanned >= scan_limit
                or (time.monotonic() - started) * 1000 >= time_limit_ms
            ):
                break
            frame = stack[-1]
            relative_dir = str(frame["relative_dir"])
            directory = target / relative_dir if relative_dir else target
            try:
                with os.scandir(directory) as handle:
                    entries = sorted(handle, key=lambda entry: entry.name)
            except OSError:
                warnings.append(
                    "unreadable directory: " + (relative_dir if relative_dir else ".")
                )
                stack.pop()
                continue
            index = int(frame["index"])
            if index >= len(entries):
                stack.pop()
                continue
            entry = entries[index]
            frame["index"] = index + 1
            scanned += 1
            try:
                info = entry.stat(follow_symlinks=False)
                is_directory = entry.is_dir(follow_symlinks=False)
                is_file = entry.is_file(follow_symlinks=False)
            except OSError:
                continue
            if not is_directory and not is_file:
                continue
            relative_path = (
                posixpath.join(relative_dir, entry.name) if relative_dir else entry.name
            )
            entry_kind = "directory" if is_directory else "file"
            if is_directory:
                stack.append({"relative_dir": relative_path, "index": 0})
            if (
                normalized_query not in entry.name.casefold()
                and normalized_query not in relative_path.casefold()
            ):
                continue
            if kind != "all" and kind != entry_kind:
                continue
            if size_min is not None and info.st_size < size_min:
                continue
            if size_max is not None and info.st_size > size_max:
                continue
            mtime = int(info.st_mtime)
            if mtime_from is not None and mtime < mtime_from:
                continue
            if mtime_to is not None and mtime > mtime_to:
                continue
            items.append(
                FileSearchEntry(
                    path=str(target / relative_path),
                    relative_path=relative_path,
                    type=entry_kind,
                    size=info.st_size,
                    mtime=mtime,
                )
            )
        incomplete = bool(stack)
        next_cursor = (
            _encode_local_search_cursor(
                {"binding": binding, "stack": stack}, self._search_cursor_key
            )
            if incomplete
            else None
        )
        return FileSearchPage(
            items=tuple(items),
            incomplete=incomplete,
            next_cursor=next_cursor,
            warnings=tuple(warnings),
        )

    def make_dir(
        self, *, path: str, owner: str, timeout_seconds: float = 30.0
    ) -> None:
        self._authorize(path).mkdir(parents=True, exist_ok=True)

    def remove_path(
        self, *, path: str, owner: str, timeout_seconds: float = 30.0
    ) -> None:
        target = self._authorize(path)
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        elif target.exists() or target.is_symlink():
            target.unlink()
        else:
            raise SlurmTransportError(f"path does not exist: {path}")

    def rename_path(
        self,
        *,
        path: str,
        new_path: str,
        owner: str,
        overwrite: bool = False,
        timeout_seconds: float = 30.0,
    ) -> None:
        source = self._authorize(path)
        destination = self._authorize(new_path)
        if not source.exists() and not source.is_symlink():
            raise SlurmTransportError(f"path does not exist: {path}")
        if destination.exists() and not overwrite:
            raise SlurmSubmissionRejected(f"target already exists: {new_path}")
        if destination.exists():
            if destination.is_dir() and not destination.is_symlink():
                shutil.rmtree(destination)
            else:
                destination.unlink()
        destination.parent.mkdir(parents=True, exist_ok=True)
        source.rename(destination)

    def copy_entries(
        self,
        *,
        paths: list[str],
        dest_dir: str,
        owner: str,
        timeout_seconds: float = 120.0,
    ) -> list[str]:
        destination = self._authorize(dest_dir)
        if not destination.is_dir():
            raise SlurmTransportError(f"destination is not a directory: {dest_dir}")
        copied: list[str] = []
        for item in paths:
            source = self._authorize(item)
            if not source.exists() and not source.is_symlink():
                raise SlurmTransportError(f"path does not exist: {item}")
            if source.is_dir() and not source.is_symlink():
                source_resolved = source.resolve()
                dest_resolved = destination.resolve()
                if dest_resolved == source_resolved or str(dest_resolved).startswith(
                    f"{source_resolved}/"
                ):
                    raise SlurmSubmissionRejected(
                        f"cannot copy a directory into itself: {item}"
                    )
            target = destination / source.name
            if target.exists() or target.is_symlink():
                if target.is_dir() and not target.is_symlink():
                    shutil.rmtree(target)
                else:
                    target.unlink()
            if source.is_dir() and not source.is_symlink():
                shutil.copytree(source, target, symlinks=True)
            else:
                shutil.copy2(source, target, follow_symlinks=False)
            copied.append(str(target))
        return copied

    def create_file(
        self,
        *,
        dir_path: str,
        name: str,
        owner: str,
        timeout_seconds: float = 30.0,
    ) -> str:
        if not name or name in {".", ".."} or "/" in name or "\\" in name:
            raise SlurmSubmissionRejected(f"unsafe file name: {name}")
        parent = self._authorize(dir_path)
        if not parent.is_dir():
            raise SlurmTransportError(f"directory does not exist: {dir_path}")
        target = parent / name
        try:
            with target.open("xb"):
                pass
        except FileExistsError as exc:
            raise SlurmSubmissionRejected(f"file already exists: {target}") from exc
        return str(target)

    def stat_path(
        self, *, path: str, owner: str, timeout_seconds: float = 30.0
    ) -> FileStat:
        target = self._authorize(path)
        if not target.exists():
            raise SlurmTransportError(f"path does not exist: {path}")
        info = target.lstat()
        if target.is_symlink():
            kind = "symlink"
        elif target.is_dir():
            kind = "dir"
        elif target.is_file():
            kind = "file"
        else:
            kind = "other"
        return FileStat(
            path=str(target), type=kind, size=info.st_size, mtime=int(info.st_mtime)
        )

    def disk_usage(
        self, *, path: str, owner: str, timeout_seconds: float = 30.0
    ) -> DiskUsage:
        target = self._authorize(path)
        if not target.exists():
            raise SlurmTransportError(f"path does not exist: {path}")
        if target.is_dir() and not target.is_symlink():
            used = _directory_apparent_size(target)
        else:
            used = target.lstat().st_size
        try:
            total: int | None = shutil.disk_usage(target).total
        except OSError:
            total = None
        return DiskUsage(path=str(target), used_bytes=used, total_bytes=total)

    def extract_archive(
        self,
        *,
        archive_path: str,
        dest_dir: str,
        owner: str,
        timeout_seconds: float = 120.0,
    ) -> int:
        archive = self._authorize(archive_path)
        destination = self._authorize(dest_dir)
        if not archive.is_file():
            raise SlurmTransportError(f"archive not found: {archive_path}")
        destination.mkdir(parents=True, exist_ok=True)
        dest_resolved = destination.resolve()
        count = 0
        with tarfile.open(archive, "r:*") as tar:
            for member in tar.getmembers():
                member_dest = (destination / member.name).resolve()
                if member_dest != dest_resolved and not str(member_dest).startswith(
                    f"{dest_resolved}/"
                ):
                    raise SlurmSubmissionRejected(
                        f"archive member escapes destination: {member.name}"
                    )
                if member.issym() or member.islnk():
                    raise SlurmSubmissionRejected(
                        f"archive link members are not permitted: {member.name}"
                    )
                count += 1
            tar.extractall(destination)
        return count

    def create_archive(
        self,
        *,
        paths: list[str],
        dest_dir: str,
        archive_name: str,
        owner: str,
        timeout_seconds: float = 120.0,
    ) -> tuple[str, int]:
        destination = self._authorize(dest_dir)
        if "/" in archive_name or "\\" in archive_name or ".." in archive_name:
            raise SlurmSubmissionRejected(f"unsafe archive name: {archive_name}")
        if not paths:
            raise SlurmTransportError("paths must be a non-empty list")
        sources = [self._authorize(item) for item in paths]
        destination.mkdir(parents=True, exist_ok=True)
        archive_path = destination / archive_name
        count = 0
        with tarfile.open(archive_path, "w:gz") as tar:
            for source in sources:
                if not source.exists():
                    raise SlurmTransportError(f"path does not exist: {source}")
                tar.add(source, arcname=source.name)
                count += 1
        return str(archive_path), archive_path.stat().st_size


class SimulatorPathChecker:
    """Probe path permissions inside the simulator as the owning Slurm user."""

    def __init__(
        self,
        *,
        executor: SimulatorExecutor,
        user: str,
        timeout_seconds: float = 10.0,
    ) -> None:
        self.executor = executor
        self.user = user
        self.timeout_seconds = timeout_seconds

    def exists(self, path: str | Path) -> bool:
        return self._test("-e", path)

    def is_dir(self, path: str | Path) -> bool:
        return self._test("-d", path)

    def readable(self, path: str | Path) -> bool:
        return self._test("-r", path)

    def executable(self, path: str | Path) -> bool:
        return self._test("-x", path)

    def writable(self, path: str | Path) -> bool:
        return self._test("-w", path)

    def _test(self, flag: str, path: str | Path) -> bool:
        try:
            result = self.executor.run(
                ["test", flag, str(path)],
                user=self.user,
                timeout_seconds=self.timeout_seconds,
            )
        except SlurmTransportError:
            return False
        return result.returncode == 0


@dataclass
class _MemoryRecord:
    owner: str
    workdir: Path
    script: str
    resource_plan: ResourcePlan
    state_flags: list[str]
    exit_code: str | None = None
    reason: str | None = None


def _validate_submit_intent(intent: SubmitIntent) -> None:
    if not intent.user.strip():
        raise SlurmSubmissionRejected("user must not be empty")
    if not intent.script.strip():
        raise SlurmSubmissionRejected("script must not be empty")
    for job_id in intent.dependency_job_ids:
        _require_job_id(job_id)
    _require_safe_slurm_value("job_name", intent.job_name)
    if intent.job_name is not None and len(intent.job_name) > 128:
        raise SlurmSubmissionRejected("Slurm job_name exceeds 128 characters")
    blockers = [
        finding
        for finding in validate_resource_plan(intent.resource_plan)
        if finding.severity == PreflightSeverity.BLOCK
    ]
    if blockers:
        codes = ", ".join(finding.code for finding in blockers)
        raise SlurmSubmissionRejected(f"resource plan failed preflight: {codes}")


def _require_safe_slurm_value(name: str, value: str | None) -> None:
    if value is None:
        return
    if not value or not _SAFE_SLURM_VALUE.fullmatch(value):
        raise SlurmSubmissionRejected(f"unsafe Slurm {name}: {value!r}")


def _require_job_id(job_id: str) -> None:
    if not _JOB_ID.fullmatch(job_id):
        raise SlurmSubmissionRejected(f"unsafe job_id: {job_id!r}")


def _sbatch_options(plan: ResourcePlan) -> list[str]:
    _require_safe_slurm_value("partition", plan.partition)
    _require_safe_slurm_value("qos", plan.qos)
    _require_safe_slurm_value("gpu_type", plan.gpu_type)
    options = [
        "--partition",
        plan.partition,
        "--nodes",
        str(plan.nodes),
        "--ntasks",
        str(plan.ntasks),
        "--cpus-per-task",
        str(plan.cpus_per_task),
        "--time",
        str(plan.time_limit),
        # Separate stdout/stderr into distinct files so the evidence
        # collector can read slurm-{job_id}.err for traceback analysis.
        "--output",
        "slurm-%j.out",
        "--error",
        "slurm-%j.err",
    ]
    if plan.qos:
        options.extend(["--qos", plan.qos])
    if plan.memory_value is not None and plan.memory_unit:
        _require_safe_slurm_value("memory_unit", plan.memory_unit)
        options.extend(["--mem", f"{plan.memory_value}{plan.memory_unit}"])
    if plan.gpus_total is not None and plan.gpus_total > 0:
        if plan.gpu_type:
            options.extend(["--gres", f"gpu:{plan.gpu_type}:{plan.gpus_total}"])
        else:
            options.extend(["--gpus", str(plan.gpus_total)])
    elif plan.gpus_per_node is not None and plan.gpus_per_node > 0:
        if plan.gpu_type:
            options.extend(["--gres", f"gpu:{plan.gpu_type}:{plan.gpus_per_node}"])
        else:
            options.extend(["--gpus-per-node", str(plan.gpus_per_node)])
    if plan.array:
        _require_safe_slurm_value("array", plan.array.expression)
        array_value = plan.array.expression
        if plan.array.max_concurrency:
            array_value = f"{array_value}%{plan.array.max_concurrency}"
        options.extend(["--array", array_value])
    return options


def _dependency_options(intent: SubmitIntent) -> list[str]:
    if not intent.dependency_job_ids:
        return []
    return ["--dependency", "afterok:" + ":".join(intent.dependency_job_ids)]


def _authorize_container_path(
    *,
    executor: SimulatorExecutor,
    path: str,
    allowed_roots: list[str],
    timeout_seconds: float,
) -> str:
    if "\x00" in path:
        raise SlurmSubmissionRejected("path contains NUL byte")
    if not path.startswith("/"):
        raise SlurmSubmissionRejected("container path must be absolute")

    resolved = executor.realpath(path, timeout_seconds=timeout_seconds)
    resolved_roots = [
        executor.realpath(root, timeout_seconds=timeout_seconds).rstrip("/")
        for root in allowed_roots
    ]
    for root in resolved_roots:
        if resolved == root or resolved.startswith(f"{root}/"):
            return resolved
    raise SlurmSubmissionRejected("container path is outside allowed roots")


class InMemorySlurmBackend:
    """Deterministic backend for API/worker development before Docker is wired."""

    def __init__(self) -> None:
        self._next_job_id = 1000
        self._jobs: dict[str, _MemoryRecord] = {}
        self._idempotency: dict[str, str] = {}

    def submit(self, intent: SubmitIntent) -> SubmitReceipt:
        _validate_submit_intent(intent)

        if intent.idempotency_key and intent.idempotency_key in self._idempotency:
            job_id = self._idempotency[intent.idempotency_key]
            state, _ = normalize_slurm_state(self._jobs[job_id].state_flags)
            return SubmitReceipt(
                job_id=job_id,
                run_state=state,
                strategy=SubmissionStrategy.IN_MEMORY,
                raw_response={"idempotent_replay": True},
            )

        job_id = str(self._next_job_id)
        self._next_job_id += 1
        self._jobs[job_id] = _MemoryRecord(
            owner=intent.user,
            workdir=intent.workdir,
            script=intent.script,
            resource_plan=intent.resource_plan,
            state_flags=["PENDING"],
        )
        if intent.idempotency_key:
            self._idempotency[intent.idempotency_key] = job_id
        return SubmitReceipt(
            job_id=job_id,
            run_state=RunState.PENDING,
            strategy=SubmissionStrategy.IN_MEMORY,
            raw_response={
                "job_id": job_id,
                "dependency_job_ids": list(intent.dependency_job_ids),
            },
        )

    def get_job(self, *, user: str, job_id: str) -> JobSnapshot:
        record = self._require_owned_job(user=user, job_id=job_id)
        return self._snapshot(job_id, record)

    def cancel(self, *, user: str, job_id: str) -> JobSnapshot:
        record = self._require_owned_job(user=user, job_id=job_id)
        record.state_flags = ["CANCELLED"]
        record.reason = "cancelled by user"
        return self._snapshot(job_id, record)

    def advance_job(
        self,
        *,
        job_id: str,
        raw_state: str | list[str],
        exit_code: str | None = None,
        reason: str | None = None,
    ) -> JobSnapshot:
        record = self._jobs[job_id]
        record.state_flags = [raw_state] if isinstance(raw_state, str) else list(raw_state)
        record.exit_code = exit_code
        record.reason = reason
        return self._snapshot(job_id, record)

    def _require_owned_job(self, *, user: str, job_id: str) -> _MemoryRecord:
        try:
            record = self._jobs[job_id]
        except KeyError as exc:
            raise SlurmBackendError(f"unknown job_id: {job_id}") from exc
        if record.owner != user:
            raise SlurmAuthError("job is not owned by user")
        return record

    def _snapshot(self, job_id: str, record: _MemoryRecord) -> JobSnapshot:
        state, flags = normalize_slurm_state(record.state_flags)
        return JobSnapshot(
            job_id=job_id,
            owner=record.owner,
            run_state=state,
            raw_state_flags=flags,
            exit_code=record.exit_code,
            reason=record.reason,
            stdout_path=record.workdir / f"slurm-{job_id}.out",
            stderr_path=record.workdir / f"slurm-{job_id}.err",
            raw_response={"state": flags},
        )


class DemoSlurmBackend:
    """Cross-process deterministic backend for the Web demonstration mode."""

    def submit(self, intent: SubmitIntent) -> SubmitReceipt:
        _validate_submit_intent(intent)
        seed = intent.idempotency_key or f"{intent.user}:{intent.workdir}:{intent.script}"
        digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:12]
        job_id = f"demo-{digest}"
        return SubmitReceipt(
            job_id=job_id,
            run_state=RunState.SUBMITTED,
            strategy=SubmissionStrategy.DEMO,
            raw_response={
                "job_id": job_id,
                "backend": "demo",
                "note": "deterministic demo backend; no Slurm resources consumed",
            },
        )

    def get_job(self, *, user: str, job_id: str) -> JobSnapshot:
        _require_job_id(job_id)
        if not job_id.startswith("demo-"):
            raise SlurmBackendOwnershipError(f"demo backend does not own job_id: {job_id}")
        return JobSnapshot(
            job_id=job_id,
            owner=user,
            run_state=RunState.SUCCEEDED,
            raw_state_flags=["COMPLETED"],
            exit_code="0:0",
            reason="demo job completed",
            raw_response={"backend": "demo", "job_id": job_id},
        )

    def cancel(self, *, user: str, job_id: str) -> JobSnapshot:
        _require_job_id(job_id)
        if not job_id.startswith("demo-"):
            raise SlurmBackendOwnershipError(f"demo backend does not own job_id: {job_id}")
        return JobSnapshot(
            job_id=job_id,
            owner=user,
            run_state=RunState.CANCELLED,
            raw_state_flags=["CANCELLED"],
            exit_code="0:0",
            reason="demo job cancelled",
            raw_response={"backend": "demo", "job_id": job_id},
        )


class RestNativeSlurmBackend:
    """Slurm REST backend for simulator and future real-platform probes."""

    def __init__(
        self,
        *,
        transport: HttpTransport,
        api_version: str = "v0.0.41",
        token: str | None = None,
    ) -> None:
        self.transport = transport
        self.api_version = api_version
        self.token = token

    def submit(self, intent: SubmitIntent) -> SubmitReceipt:
        _validate_submit_intent(intent)
        payload = {
            "script": intent.script,
            "job": self._job_payload(intent),
        }
        response = self.transport.request(
            "POST",
            f"/slurm/{self.api_version}/job/submit",
            token=self.token,
            payload=payload,
        )
        semantic = check_slurm_rest_semantics(response.payload, required_fields=[])
        if response.status >= 400 or semantic.level == RestSemanticLevel.ERROR:
            raise SlurmSubmissionRejected(f"REST submit rejected: {response.payload!r}")
        job_id = _extract_job_id(response.payload)
        if not job_id:
            raise SlurmTransportError("REST submit response did not include job_id")
        return SubmitReceipt(
            job_id=job_id,
            run_state=RunState.SUBMITTED,
            strategy=SubmissionStrategy.REST_NATIVE,
            raw_response=response.payload,
        )

    def get_job(self, *, user: str, job_id: str) -> JobSnapshot:
        _require_job_id(job_id)
        response = self.transport.request(
            "GET",
            f"/slurm/{self.api_version}/job/{job_id}",
            token=self.token,
        )
        semantic = check_slurm_rest_semantics(response.payload)
        if response.status >= 400 or semantic.level == RestSemanticLevel.ERROR:
            raise SlurmTransportError(f"REST get job failed: {response.payload!r}")
        return _snapshot_from_rest(user=user, job_id=job_id, payload=response.payload)

    def cancel(self, *, user: str, job_id: str) -> JobSnapshot:
        _require_job_id(job_id)
        response = self.transport.request(
            "DELETE",
            f"/slurm/{self.api_version}/job/{job_id}",
            token=self.token,
        )
        semantic = check_slurm_rest_semantics(response.payload)
        if response.status >= 400 or semantic.level == RestSemanticLevel.ERROR:
            raise SlurmTransportError(f"REST cancel failed: {response.payload!r}")
        return JobSnapshot(
            job_id=job_id,
            owner=user,
            run_state=RunState.CANCELLED,
            raw_state_flags=["CANCELLED"],
            reason="cancel requested",
            raw_response=response.payload,
        )

    def _job_payload(self, intent: SubmitIntent) -> dict[str, Any]:
        options = _sbatch_options(intent.resource_plan)
        job: dict[str, Any] = {
            "name": intent.job_name or "pilot107-run",
            "current_working_directory": str(intent.workdir),
            "environment": ["PILOT107_RUN=1"],
        }
        option_pairs = zip(options[0::2], options[1::2], strict=True)
        for key, value in option_pairs:
            job[key.removeprefix("--").replace("-", "_")] = value
        if intent.dependency_job_ids:
            job["dependency"] = "afterok:" + ":".join(intent.dependency_job_ids)
        return job


class CommandSubmitBackend:
    """Controlled command backend for the Docker simulator."""

    def __init__(
        self,
        *,
        allowed_roots: list[str | Path],
        runner: CommandRunner | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        self.allowed_roots = allowed_roots
        self.runner = runner or SubprocessCommandRunner()
        self.timeout_seconds = timeout_seconds

    def submit(self, intent: SubmitIntent) -> SubmitReceipt:
        _validate_submit_intent(intent)
        try:
            allowed_roots = resolve_owner_roots(self.allowed_roots, user=intent.user)
        except OwnerRootPolicyError as exc:
            raise SlurmSubmissionRejected(str(exc)) from exc
        safe_workdir = authorize_path(str(intent.workdir), list(allowed_roots))
        script_path = safe_workdir.resolved / _submission_script_name(intent)
        script_path.write_text(intent.script, encoding="utf-8")
        argv = [
            "sbatch",
            "--parsable",
            "--job-name",
            intent.job_name or "pilot107-run",
            "--chdir",
            str(safe_workdir.resolved),
            *_sbatch_options(intent.resource_plan),
            *_dependency_options(intent),
            str(script_path),
        ]
        result = self.runner.run(
            argv, cwd=safe_workdir.resolved, timeout_seconds=self.timeout_seconds
        )
        if result.returncode != 0:
            raise SlurmSubmissionRejected(result.stderr.strip() or "sbatch failed")
        job_id = result.stdout.strip().splitlines()[0].split(";")[0].strip()
        _require_job_id(job_id)
        return SubmitReceipt(
            job_id=job_id,
            run_state=RunState.SUBMITTED,
            strategy=SubmissionStrategy.COMMAND,
            raw_response={"stdout": result.stdout, "stderr": result.stderr, "argv": argv},
        )

    def get_job(self, *, user: str, job_id: str) -> JobSnapshot:
        _require_job_id(job_id)
        result = self.runner.run(
            ["squeue", "-h", "-j", job_id, "-o", "%i|%u|%T|%R"],
            timeout_seconds=self.timeout_seconds,
        )
        if result.returncode != 0:
            raise SlurmTransportError(result.stderr.strip() or "squeue failed")
        if not result.stdout.strip():
            return self._get_finished_job(user=user, job_id=job_id)
        owner, run_state, flags, _exit_code, reason = _aggregate_command_job_rows(
            result.stdout,
            job_id=job_id,
            user=user,
            source="squeue",
        )
        return JobSnapshot(
            job_id=job_id,
            owner=owner,
            run_state=run_state,
            raw_state_flags=flags,
            reason=reason,
            raw_response={"stdout": result.stdout},
        )

    def cancel(self, *, user: str, job_id: str) -> JobSnapshot:
        _require_job_id(job_id)
        self.get_job(user=user, job_id=job_id)
        result = self.runner.run(["scancel", job_id], timeout_seconds=self.timeout_seconds)
        if result.returncode != 0:
            raise SlurmTransportError(result.stderr.strip() or "scancel failed")
        return JobSnapshot(
            job_id=job_id,
            owner=user,
            run_state=RunState.CANCELLED,
            raw_state_flags=["CANCELLED"],
            reason="cancel requested",
            raw_response={"stdout": result.stdout, "stderr": result.stderr},
        )

    def _get_finished_job(self, *, user: str, job_id: str) -> JobSnapshot:
        result = self.runner.run(
            ["sacct", "-n", "-j", job_id, "-X", "-o", "JobID,User,State,ExitCode"],
            timeout_seconds=self.timeout_seconds,
        )
        if result.returncode != 0:
            raise SlurmTransportError(result.stderr.strip() or "sacct failed")
        if not result.stdout.strip():
            raise SlurmTransportError(f"job not found: {job_id}")
        owner, run_state, flags, exit_code, _reason = _aggregate_command_job_rows(
            result.stdout,
            job_id=job_id,
            user=user,
            source="sacct",
        )
        return JobSnapshot(
            job_id=job_id,
            owner=owner,
            run_state=run_state,
            raw_state_flags=flags,
            exit_code=exit_code,
            raw_response={"stdout": result.stdout},
        )


class DockerSimulatorCommandBackend:
    """Command backend that executes against the local Docker Slurm simulator."""

    def __init__(
        self,
        *,
        executor: SimulatorExecutor,
        allowed_roots: list[str],
        timeout_seconds: float = 10.0,
    ) -> None:
        self.executor = executor
        self.allowed_roots = allowed_roots
        self.timeout_seconds = timeout_seconds

    def submit(self, intent: SubmitIntent) -> SubmitReceipt:
        _validate_submit_intent(intent)
        try:
            allowed_roots = resolve_owner_roots(self.allowed_roots, user=intent.user)
        except OwnerRootPolicyError as exc:
            raise SlurmSubmissionRejected(str(exc)) from exc
        workdir = _authorize_container_path(
            executor=self.executor,
            path=str(intent.workdir),
            allowed_roots=list(allowed_roots),
            timeout_seconds=self.timeout_seconds,
        )
        script_path = posixpath.join(workdir, _submission_script_name(intent))
        self.executor.write_text(
            path=script_path,
            content=intent.script,
            owner=intent.user,
            timeout_seconds=self.timeout_seconds,
        )
        argv = [
            "sbatch",
            "--parsable",
            "--job-name",
            intent.job_name or "pilot107-run",
            "--chdir",
            workdir,
            *_sbatch_options(intent.resource_plan),
            *_dependency_options(intent),
            script_path,
        ]
        result = self.executor.run(
            argv,
            cwd=workdir,
            user=intent.user,
            timeout_seconds=self.timeout_seconds,
        )
        if result.returncode != 0:
            raise SlurmSubmissionRejected(result.stderr.strip() or "sbatch failed")
        job_id = result.stdout.strip().splitlines()[0].split(";")[0].strip()
        _require_job_id(job_id)
        return SubmitReceipt(
            job_id=job_id,
            run_state=RunState.SUBMITTED,
            strategy=SubmissionStrategy.COMMAND,
            raw_response={"stdout": result.stdout, "stderr": result.stderr, "argv": argv},
        )

    def get_job(self, *, user: str, job_id: str) -> JobSnapshot:
        _require_job_id(job_id)
        result = self.executor.run(
            ["squeue", "-h", "-j", job_id, "-o", "%i|%u|%T|%R"],
            user=user,
            timeout_seconds=self.timeout_seconds,
        )
        if result.returncode != 0:
            raise SlurmTransportError(result.stderr.strip() or "squeue failed")
        if not result.stdout.strip():
            return self._get_finished_job(user=user, job_id=job_id)
        owner, run_state, flags, _exit_code, reason = _aggregate_command_job_rows(
            result.stdout,
            job_id=job_id,
            user=user,
            source="squeue",
        )
        return JobSnapshot(
            job_id=job_id,
            owner=owner,
            run_state=run_state,
            raw_state_flags=flags,
            reason=reason,
            raw_response={"stdout": result.stdout},
        )

    def cancel(self, *, user: str, job_id: str) -> JobSnapshot:
        _require_job_id(job_id)
        self.get_job(user=user, job_id=job_id)
        result = self.executor.run(
            ["scancel", job_id],
            user=user,
            timeout_seconds=self.timeout_seconds,
        )
        if result.returncode != 0:
            raise SlurmTransportError(result.stderr.strip() or "scancel failed")
        return JobSnapshot(
            job_id=job_id,
            owner=user,
            run_state=RunState.CANCELLED,
            raw_state_flags=["CANCELLED"],
            reason="cancel requested",
            raw_response={"stdout": result.stdout, "stderr": result.stderr},
        )

    def _get_finished_job(self, *, user: str, job_id: str) -> JobSnapshot:
        result = self.executor.run(
            ["sacct", "-nP", "-j", job_id, "-X", "-o", "JobID,User,State,ExitCode"],
            user=user,
            timeout_seconds=self.timeout_seconds,
        )
        if result.returncode != 0:
            raise SlurmTransportError(result.stderr.strip() or "sacct failed")
        if not result.stdout.strip():
            raise SlurmTransportError(f"job not found: {job_id}")
        owner, run_state, flags, exit_code, _reason = _aggregate_command_job_rows(
            result.stdout,
            job_id=job_id,
            user=user,
            source="sacct",
        )
        return JobSnapshot(
            job_id=job_id,
            owner=owner,
            run_state=run_state,
            raw_state_flags=flags,
            exit_code=exit_code,
            stdout_path=Path(f"/public/home/{user}/slurm-{job_id}.out"),
            stderr_path=Path(f"/public/home/{user}/slurm-{job_id}.out"),
            raw_response={"stdout": result.stdout},
        )


class SshSlurmBackend(DockerSimulatorCommandBackend):
    """Slurm command adapter carried by the typed, owner-bound SSH relay.

    This backend intentionally reuses the simulator command backend's state
    normalization and ownership checks, while changing materialization and
    reconciliation to match the real-platform contract.
    """

    submission_strategy = SubmissionStrategy.COMMAND
    backend_kind = "real107-ssh"

    def __init__(
        self,
        *,
        executor: SimulatorExecutor,
        allowed_roots: list[str],
        timeout_seconds: float = 10.0,
        target_id: str | None = None,
    ) -> None:
        super().__init__(
            executor=executor,
            allowed_roots=allowed_roots,
            timeout_seconds=timeout_seconds,
        )
        self.target_id = target_id

    def submit(self, intent: SubmitIntent) -> SubmitReceipt:
        _validate_submit_intent(intent)
        try:
            allowed_roots = resolve_owner_roots(self.allowed_roots, user=intent.user)
        except OwnerRootPolicyError as exc:
            raise SlurmSubmissionRejected(str(exc)) from exc
        workdir = _authorize_container_path(
            executor=self.executor,
            path=str(intent.workdir),
            allowed_roots=list(allowed_roots),
            timeout_seconds=self.timeout_seconds,
        )
        run_directory = posixpath.join(
            workdir,
            ".107pilot",
            "runs",
            _ssh_run_directory_name(intent),
        )
        mkdir_result = self.executor.run(
            ["mkdir", "-p", "--", run_directory],
            user=intent.user,
            timeout_seconds=self.timeout_seconds,
        )
        if mkdir_result.returncode != 0:
            raise SlurmTransportError(mkdir_result.stderr.strip() or "prepare run directory failed")
        script_path = posixpath.join(run_directory, "submission.sbatch")
        marker_path = posixpath.join(run_directory, "intent.json")
        self.executor.write_text(
            path=script_path,
            content=intent.script,
            owner=intent.user,
            timeout_seconds=self.timeout_seconds,
        )
        self.executor.write_text(
            path=marker_path,
            content=json.dumps(
                {
                    "schema": "pilot107.ssh_submission_intent.v1",
                    "owner": intent.user,
                    "job_name": intent.job_name,
                    "idempotency_key": intent.idempotency_key,
                    "script_sha256": hashlib.sha256(intent.script.encode("utf-8")).hexdigest(),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n",
            owner=intent.user,
            timeout_seconds=self.timeout_seconds,
        )
        argv = [
            "sbatch",
            "--parsable",
            "--job-name",
            intent.job_name or "pilot107-run",
            "--chdir",
            workdir,
            *_sbatch_options(intent.resource_plan),
            *_dependency_options(intent),
            script_path,
        ]
        result = self.executor.run(
            argv,
            cwd=workdir,
            user=intent.user,
            timeout_seconds=self.timeout_seconds,
        )
        if result.returncode != 0:
            raise SlurmSubmissionRejected(result.stderr.strip() or "sbatch failed")
        job_id = result.stdout.strip().splitlines()[0].split(";")[0].strip()
        _require_job_id(job_id)
        return SubmitReceipt(
            job_id=job_id,
            run_state=RunState.SUBMITTED,
            strategy=SubmissionStrategy.COMMAND,
            raw_response={
                "backend_kind": "real107-ssh",
                "target_id": self.target_id,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "argv": argv,
                "run_directory": run_directory,
                "marker_path": marker_path,
            },
        )

    def find_jobs_by_marker(
        self,
        *,
        user: str,
        job_name_marker: str,
        since_timestamp: float,
    ) -> list[str]:
        """Find one logical job per matching marker for timeout recovery."""

        if not _SAFE_SLURM_VALUE.fullmatch(job_name_marker):
            raise SlurmSubmissionRejected("unsafe reconciliation marker")
        since = datetime.fromtimestamp(since_timestamp, tz=UTC).strftime("%Y-%m-%dT%H:%M:%S")
        result = self.executor.run(
            [
                "sacct",
                "-nP",
                "-X",
                "-u",
                user,
                "-S",
                since,
                "-o",
                "JobIDRaw,User,JobName",
            ],
            user=user,
            timeout_seconds=self.timeout_seconds,
        )
        if result.returncode != 0:
            raise SlurmTransportError(result.stderr.strip() or "sacct reconciliation failed")
        matches: list[str] = []
        for line in result.stdout.splitlines():
            if not line.strip():
                continue
            job_id, owner, job_name = _split_command_row(line, expected_fields=3)
            if owner != user or job_name != job_name_marker:
                continue
            _require_job_id(job_id)
            if job_id not in matches:
                matches.append(job_id)
        return matches

    def _get_finished_job(self, *, user: str, job_id: str) -> JobSnapshot:
        result = self.executor.run(
            ["sacct", "-nP", "-j", job_id, "-X", "-o", "JobID,User,State,ExitCode"],
            user=user,
            timeout_seconds=self.timeout_seconds,
        )
        if result.returncode != 0:
            raise SlurmTransportError(result.stderr.strip() or "sacct failed")
        line = result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""
        if not line:
            raise SlurmTransportError(f"job not found: {job_id}")
        found_job_id, owner, raw_state, exit_code = _split_command_row(line, expected_fields=4)
        if found_job_id != job_id:
            raise SlurmTransportError("sacct returned mismatched job_id")
        _require_accounting_owner(owner=owner, user=user)
        run_state, flags = normalize_slurm_state(raw_state)
        return JobSnapshot(
            job_id=job_id,
            owner=owner,
            run_state=run_state,
            raw_state_flags=flags,
            exit_code=exit_code,
            raw_response={"stdout": result.stdout},
        )


def _extract_job_id(payload: dict[str, Any]) -> str | None:
    result = payload.get("result")
    if isinstance(result, dict):
        for key in ("job_id", "jobId", "id"):
            value = result.get(key)
            if value is not None:
                return str(value)
    for key in ("job_id", "jobId", "id"):
        value = payload.get(key)
        if value is not None:
            return str(value)
    return None


def _snapshot_from_rest(*, user: str, job_id: str, payload: dict[str, Any]) -> JobSnapshot:
    job = payload
    jobs = payload.get("jobs")
    if isinstance(jobs, list) and jobs:
        job = jobs[0]
    raw_state = job.get("job_state") or job.get("state") or "UNKNOWN"
    run_state, flags = normalize_slurm_state(raw_state)
    owner = str(job.get("user_name") or job.get("user") or user)
    if owner != user:
        raise SlurmAuthError("job is not owned by user")
    return JobSnapshot(
        job_id=job_id,
        owner=owner,
        run_state=run_state,
        raw_state_flags=flags,
        exit_code=_string_or_none(job.get("exit_code")),
        reason=_string_or_none(job.get("state_reason") or job.get("reason")),
        raw_response=payload,
    )


def _require_accounting_owner(*, owner: str, user: str) -> None:
    if not owner:
        raise SlurmTransportError("sacct did not populate job owner yet")
    if owner != user:
        raise SlurmAuthError("job is not owned by user")


def _aggregate_command_job_rows(
    output: str,
    *,
    job_id: str,
    user: str,
    source: str,
) -> tuple[str, RunState, list[str], str | None, str | None]:
    """Aggregate a parent job and its Slurm array element rows."""

    if source not in {"squeue", "sacct"}:
        raise ValueError("command job row source is invalid")
    rows: list[tuple[str, RunState, list[str], str]] = []
    for line in output.strip().splitlines():
        found_job_id, owner, raw_state, detail = _split_command_row(
            line, expected_fields=4
        )
        if not _is_job_or_array_element(found_job_id, parent_job_id=job_id):
            raise SlurmTransportError(f"{source} returned mismatched job_id")
        _require_accounting_owner(owner=owner, user=user)
        run_state, flags = normalize_slurm_state(raw_state)
        rows.append((owner, run_state, flags, detail))
    if not rows:
        raise SlurmTransportError(f"job not found: {job_id}")

    states = {row[1] for row in rows}
    priority = (
        RunState.RUNNING,
        RunState.COMPLETING,
        RunState.PENDING,
        RunState.SUBMITTED,
        RunState.FAILED,
        RunState.CANCELLED,
        RunState.SUCCEEDED,
        RunState.UNKNOWN,
    )
    run_state = next(state for state in priority if state in states)
    raw_flags = list(
        dict.fromkeys(flag for _owner, _state, flags, _detail in rows for flag in flags)
    )
    details = list(dict.fromkeys(row[3] for row in rows if row[3]))
    if source == "sacct":
        nonzero = [value for value in details if not value.startswith("0:")]
        exit_code = nonzero[0] if nonzero else details[0] if details else None
        reason = None
    else:
        exit_code = None
        reason = ", ".join(details) or None
    return rows[0][0], run_state, raw_flags, exit_code, reason


def _is_job_or_array_element(found_job_id: str, *, parent_job_id: str) -> bool:
    if found_job_id == parent_job_id:
        return True
    prefix = f"{parent_job_id}_"
    if not found_job_id.startswith(prefix):
        return False
    suffix = found_job_id[len(prefix) :]
    return bool(re.fullmatch(r"(?:\d+|\[[0-9,_%+\-]+\])", suffix))


def _split_command_row(line: str, *, expected_fields: int) -> list[str]:
    fields = (
        [field.strip() for field in line.split("|")] if "|" in line else line.split()
    )
    if len(fields) < expected_fields:
        raise SlurmTransportError(f"unexpected command output row: {line!r}")
    return fields[:expected_fields]


def _string_or_none(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _submission_script_name(intent: SubmitIntent) -> str:
    raw = intent.idempotency_key or hashlib.sha256(intent.script.encode("utf-8")).hexdigest()[:16]
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", raw).strip("._-")
    if not safe:
        safe = hashlib.sha256(intent.script.encode("utf-8")).hexdigest()[:16]
    return f"pilot107-submit-{safe[:80]}.sbatch"


def _ssh_run_directory_name(intent: SubmitIntent) -> str:
    raw = (intent.idempotency_key or "").removesuffix(":submit")
    safe = re.sub(r"[^A-Za-z0-9_.:-]+", "_", raw).strip("._:-")
    if not safe:
        safe = hashlib.sha256(intent.script.encode("utf-8")).hexdigest()[:32]
    return safe[:96]
