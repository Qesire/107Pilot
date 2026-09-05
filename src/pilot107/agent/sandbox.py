"""Fail-closed, argv-only validation inside an isolated Agent Workspace."""

from __future__ import annotations

import hashlib
import json
import math
import os
import resource
import shutil
import signal
import subprocess
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pilot107.agent.workspace import AgentWorkspaceRecord, SandboxResultRecord

if TYPE_CHECKING:
    from pilot107.agent.project_store import ProjectStore


class SandboxPolicyError(ValueError):
    """The validation request cannot be executed under the closed sandbox policy."""


@dataclass(frozen=True)
class SandboxExecutionResult:
    result_id: str
    argv: tuple[str, ...]
    status: Literal["succeeded", "failed", "timed_out", "cancelled"]
    exit_code: int | None
    stdout: str
    stderr: str
    stdout_sha256: str
    stderr_sha256: str
    limit_reason: Literal["time_limit", "output_limit", "resource_limit"] | None

    def persistence_record(self) -> SandboxResultRecord:
        return SandboxResultRecord(
            result_id=self.result_id,
            argv=self.argv,
            status=self.status,
            exit_code=self.exit_code,
            stdout_sha256=self.stdout_sha256,
            stderr_sha256=self.stderr_sha256,
        )


class SandboxExecutor:
    """Run explicitly allowed validation commands without host network or credentials."""

    _EXECUTABLES = {"python": "/usr/bin/python3", "python3": "/usr/bin/python3"}

    def __init__(
        self,
        *,
        store: ProjectStore | None = None,
        bwrap_path: str = "/usr/bin/bwrap",
        max_output_bytes: int = 256 * 1024,
        max_memory_bytes: int = 512 * 1024 * 1024,
        max_processes: int = 64,
        max_timeout_seconds: int = 300,
        dependency_caches: tuple[Path, ...] = (),
    ) -> None:
        for value, label in (
            (max_output_bytes, "max_output_bytes"),
            (max_memory_bytes, "max_memory_bytes"),
            (max_processes, "max_processes"),
            (max_timeout_seconds, "max_timeout_seconds"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{label} must be a positive integer")
        if not Path(bwrap_path).is_absolute():
            raise ValueError("bwrap_path must be absolute")
        self.store = store
        self.bwrap_path = bwrap_path
        self.max_output_bytes = max_output_bytes
        self.max_memory_bytes = max_memory_bytes
        self.max_processes = max_processes
        self.max_timeout_seconds = max_timeout_seconds
        self.dependency_caches = tuple(Path(item) for item in dependency_caches)

    def execute(
        self,
        workspace: AgentWorkspaceRecord,
        *,
        argv: tuple[str, ...],
        timeout: int | float,
        change_set_id: str | None = None,
    ) -> SandboxExecutionResult:
        if not isinstance(workspace, AgentWorkspaceRecord):
            raise TypeError("workspace must be an AgentWorkspaceRecord")
        normalized_argv = self._validate_argv(argv)
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or timeout <= 0
            or timeout > self.max_timeout_seconds
        ):
            raise SandboxPolicyError("timeout exceeds the sandbox policy")
        if not Path(self.bwrap_path).is_file() or shutil.which(self.bwrap_path) is None:
            raise SandboxPolicyError("bubblewrap is required for sandbox execution")

        declared_root = Path(workspace.local_root)
        if declared_root.is_symlink():
            raise SandboxPolicyError("Workspace root cannot be a symlink")
        try:
            root = declared_root.resolve(strict=True)
        except OSError as exc:
            raise SandboxPolicyError("Workspace root does not exist") from exc
        if not root.is_dir():
            raise SandboxPolicyError("Workspace root must be a directory")
        if change_set_id is not None:
            if self.store is None:
                raise SandboxPolicyError("ChangeSet persistence requires a store")
            change_set = self.store.get_change_set(change_set_id, owner=workspace.owner)
            if change_set.workspace_id != workspace.workspace_id:
                raise SandboxPolicyError("ChangeSet belongs to a different Workspace")

        command = self._sandbox_command(root, normalized_argv)
        process_limit = _user_task_count() + self.max_processes
        with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
            try:
                process = subprocess.Popen(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    env={},
                    start_new_session=True,
                    preexec_fn=lambda: self._set_limits(float(timeout), process_limit),
                )
            except OSError as exc:
                raise SandboxPolicyError("sandbox process could not be started") from exc
            timed_out = False
            try:
                process.wait(timeout=float(timeout))
            except subprocess.TimeoutExpired:
                timed_out = True
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait()

            stdout_size = os.fstat(stdout_file.fileno()).st_size
            stderr_size = os.fstat(stderr_file.fileno()).st_size
            stdout_file.seek(0)
            stderr_file.seek(0)
            stdout_bytes = stdout_file.read(self.max_output_bytes)
            stderr_bytes = stderr_file.read(self.max_output_bytes)

        overflow = (
            stdout_size >= self.max_output_bytes or stderr_size >= self.max_output_bytes
        )
        if timed_out:
            status: Literal["succeeded", "failed", "timed_out", "cancelled"] = (
                "timed_out"
            )
            exit_code = None
            limit_reason: Literal[
                "time_limit", "output_limit", "resource_limit"
            ] | None = "time_limit"
        elif overflow:
            status = "failed"
            exit_code = process.returncode
            limit_reason = "output_limit"
        elif process.returncode == 0:
            status = "succeeded"
            exit_code = 0
            limit_reason = None
        else:
            status = "failed"
            exit_code = process.returncode
            limit_reason = "resource_limit" if process.returncode < 0 else None

        result = self._result(
            argv=normalized_argv,
            status=status,
            exit_code=exit_code,
            stdout_bytes=stdout_bytes,
            stderr_bytes=stderr_bytes,
            limit_reason=limit_reason,
        )
        if change_set_id is not None:
            assert self.store is not None
            self.store.append_sandbox_result(
                change_set_id,
                owner=workspace.owner,
                result=result.persistence_record(),
            )
        return result

    def _validate_argv(self, argv: object) -> tuple[str, ...]:
        if not isinstance(argv, tuple):
            raise SandboxPolicyError("argv must be a tuple, never a shell string")
        if not argv or len(argv) > 128:
            raise SandboxPolicyError("argv length exceeds the sandbox policy")
        if any(
            not isinstance(item, str)
            or not item
            or "\0" in item
            or len(item.encode()) > 64 * 1024
            for item in argv
        ):
            raise SandboxPolicyError("argv contains an invalid argument")
        if sum(len(item.encode()) for item in argv) > 256 * 1024:
            raise SandboxPolicyError("argv exceeds the sandbox policy")
        if argv[0] not in self._EXECUTABLES:
            raise SandboxPolicyError("executable is not allowed by sandbox policy")
        return argv

    def _sandbox_command(self, root: Path, argv: tuple[str, ...]) -> list[str]:
        command = [
            self.bwrap_path,
            "--unshare-all",
            "--die-with-parent",
            "--new-session",
            "--clearenv",
            "--ro-bind",
            "/usr",
            "/usr",
        ]
        for system_path in ("/lib", "/lib64"):
            if Path(system_path).exists():
                command.extend(("--ro-bind", system_path, system_path))
        command.extend(
            (
                "--proc",
                "/proc",
                "--dev",
                "/dev",
                "--tmpfs",
                "/tmp",
                "--dir",
                "/workspace",
                "--bind",
                str(root),
                "/workspace",
                "--chdir",
                "/workspace",
                "--setenv",
                "PATH",
                "/usr/bin",
                "--setenv",
                "HOME",
                "/tmp",
                "--setenv",
                "TMPDIR",
                "/tmp",
                "--setenv",
                "PYTHONDONTWRITEBYTECODE",
                "1",
            )
        )
        for index, cache in enumerate(self.dependency_caches):
            if cache.is_symlink():
                raise SandboxPolicyError("Dependency cache cannot be a symlink")
            try:
                resolved = cache.resolve(strict=True)
            except OSError as exc:
                raise SandboxPolicyError("Dependency cache does not exist") from exc
            if not resolved.is_dir():
                raise SandboxPolicyError("Dependency cache must be a directory")
            destination = f"/deps/cache-{index}"
            command.extend(("--dir", destination, "--ro-bind", str(resolved), destination))
        command.extend(("--", self._EXECUTABLES[argv[0]], *argv[1:]))
        return command

    def _set_limits(self, timeout: float, process_limit: int) -> None:
        cpu_seconds = max(1, math.ceil(timeout))
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds + 1, cpu_seconds + 2))
        resource.setrlimit(
            resource.RLIMIT_AS, (self.max_memory_bytes, self.max_memory_bytes)
        )
        resource.setrlimit(
            resource.RLIMIT_NPROC, (process_limit, process_limit)
        )
        resource.setrlimit(
            resource.RLIMIT_FSIZE,
            (self.max_output_bytes, self.max_output_bytes),
        )

    def _result(
        self,
        *,
        argv: tuple[str, ...],
        status: Literal["succeeded", "failed", "timed_out", "cancelled"],
        exit_code: int | None,
        stdout_bytes: bytes,
        stderr_bytes: bytes,
        limit_reason: Literal["time_limit", "output_limit", "resource_limit"] | None,
    ) -> SandboxExecutionResult:
        stdout_sha256 = hashlib.sha256(stdout_bytes).hexdigest()
        stderr_sha256 = hashlib.sha256(stderr_bytes).hexdigest()
        identity = json.dumps(
            {
                "argv": argv,
                "status": status,
                "exit_code": exit_code,
                "stdout_sha256": stdout_sha256,
                "stderr_sha256": stderr_sha256,
                "limit_reason": limit_reason,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        result_id = f"sandbox-{hashlib.sha256(identity.encode()).hexdigest()[:24]}"
        return SandboxExecutionResult(
            result_id=result_id,
            argv=argv,
            status=status,
            exit_code=exit_code,
            stdout=stdout_bytes.decode("utf-8", errors="replace"),
            stderr=stderr_bytes.decode("utf-8", errors="replace"),
            stdout_sha256=stdout_sha256,
            stderr_sha256=stderr_sha256,
            limit_reason=limit_reason,
        )


def _user_task_count() -> int:
    """Return the current kernel task count for this UID for an additive NPROC cap."""

    uid = os.getuid()
    total = 0
    for status_path in Path("/proc").glob("[0-9]*/status"):
        try:
            fields = {
                line.partition(":")[0]: line.partition(":")[2].strip()
                for line in status_path.read_text().splitlines()
                if ":" in line
            }
            if int(fields.get("Uid", "-1").split()[0]) == uid:
                total += int(fields.get("Threads", "1"))
        except (OSError, ValueError, IndexError):
            continue
    return max(total, 1)
