"""Typed, owner-bound SSH relay for a pre-authenticated ControlMaster.

The relay is deliberately narrower than a generic SSH shell.  Callers submit
structured argv which are validated against the operations required by the
Slurm and Evidence adapters.  Authentication material is owned by OpenSSH;
107Pilot stores only a configured control-socket reference and never reads a
password, OTP, private key, or agent socket.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Protocol

from pilot107.adapters.slurm import (
    CommandResult,
    FileEntry,
    FileStat,
    SlurmTransportError,
)
from pilot107.core.identity import is_safe_username
from pilot107.core.path_policy import OwnerRootPolicyError, resolve_owner_roots

_SAFE_TARGET = re.compile(r"^[A-Za-z0-9_.:@%+-]+$")
_SAFE_JOB_ID = re.compile(r"^[A-Za-z0-9_.+-]+$")
_SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9_.:-]+$")
_SAFE_SLURM_ATOM = re.compile(r"^[A-Za-z0-9_.:+/@=%,-]+$")
_SAFE_FORMAT = re.compile(r"^[A-Za-z0-9%|,._:+@=-]+$")
_BASE64_ALPHABET = re.compile(r"^[A-Za-z0-9+/]*={0,2}$")


class SshSessionState(StrEnum):
    ACTIVE = "active"
    AUTH_REQUIRED = "auth_required"
    REVOKED = "revoked"
    EXPIRED = "expired"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class SshRelayCheck:
    state: SshSessionState
    checked_at: str
    status_code: str
    message: str


@dataclass(frozen=True)
class SshRelayConfig:
    """Deployment-owned reference to one SSH identity and Slurm target."""

    connection_id: str
    target_id: str
    target: str
    control_path: Path
    portal_owner: str
    slurm_user: str
    owner_roots: tuple[str, ...]
    known_hosts_file: Path | None = None
    port: int | None = None
    timeout_seconds: float = 20.0

    def __post_init__(self) -> None:
        for label, value in (
            ("connection_id", self.connection_id),
            ("target_id", self.target_id),
        ):
            if not _SAFE_RUN_ID.fullmatch(value):
                raise ValueError(f"unsafe SSH {label}: {value!r}")
        if not _SAFE_TARGET.fullmatch(self.target):
            raise ValueError("SSH target must be a deployment-owned host or alias")
        if not self.control_path.is_absolute():
            raise ValueError("SSH control path must be absolute")
        if self.known_hosts_file is not None and not self.known_hosts_file.is_absolute():
            raise ValueError("SSH known-hosts file must be absolute")
        if not is_safe_username(self.portal_owner) or not is_safe_username(self.slurm_user):
            raise ValueError("SSH portal and Slurm owners must be safe usernames")
        if not self.owner_roots:
            raise ValueError("SSH owner roots must be explicit and non-empty")
        if self.port is not None and not 1 <= self.port <= 65535:
            raise ValueError("SSH port must be between 1 and 65535")
        if self.timeout_seconds <= 0:
            raise ValueError("SSH timeout must be positive")
        resolve_owner_roots(self.owner_roots, user=self.slurm_user)

    def expanded_owner_roots(self) -> tuple[str, ...]:
        try:
            roots = resolve_owner_roots(self.owner_roots, user=self.slurm_user)
        except OwnerRootPolicyError as exc:  # pragma: no cover - guarded by __post_init__
            raise ValueError(str(exc)) from exc
        return tuple(_absolute_remote_path(root) for root in roots)


class FixedRemoteProgram(StrEnum):
    """Application-owned remote programs; API input cannot select source text."""

    EVIDENCE_FS = "evidence_fs"


class SshRelayClient(Protocol):
    config: SshRelayConfig

    def check(self) -> SshRelayCheck:
        """Check the already-authenticated session without prompting."""

    def execute(
        self,
        argv: tuple[str, ...],
        *,
        cwd: str | None = None,
        portal_owner: str,
        stdin: str | None = None,
        timeout_seconds: float | None = None,
    ) -> CommandResult:
        """Execute one allowlisted remote argv for the bound owner."""

    def execute_fixed_program(
        self,
        program: FixedRemoteProgram,
        args: tuple[str, ...],
        *,
        portal_owner: str,
        timeout_seconds: float | None = None,
    ) -> CommandResult:
        """Execute immutable application code with validated positional args."""


class SshRelayAuthRequired(SlurmTransportError):
    """The configured ControlMaster is missing or no longer authenticated."""


class SshRelayPolicyError(SlurmTransportError):
    """A caller requested an operation outside the relay contract."""


class SubprocessSshRelayClient:
    """OpenSSH implementation for the single-user ``control_plane_master`` mode."""

    def __init__(
        self,
        config: SshRelayConfig,
        *,
        fixed_programs: dict[FixedRemoteProgram, str] | None = None,
    ) -> None:
        self.config = config
        self._fixed_programs = dict(fixed_programs or {})

    def check(self) -> SshRelayCheck:
        checked_at = datetime.now(UTC).isoformat()
        command = [*self._base_ssh_options(), "-O", "check", self.config.target]
        try:
            completed = subprocess.run(
                command,
                check=False,
                text=True,
                capture_output=True,
                timeout=self.config.timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            return SshRelayCheck(
                state=SshSessionState.UNAVAILABLE,
                checked_at=checked_at,
                status_code="SSH.CHECK_TIMEOUT",
                message="SSH 连接检查超时",
            )
        except OSError:
            return SshRelayCheck(
                state=SshSessionState.UNAVAILABLE,
                checked_at=checked_at,
                status_code="SSH.CLIENT_UNAVAILABLE",
                message="SSH 客户端不可用",
            )
        if completed.returncode == 0:
            return SshRelayCheck(
                state=SshSessionState.ACTIVE,
                checked_at=checked_at,
                status_code="SSH.ACTIVE",
                message="真实算力平台连接可用",
            )
        return SshRelayCheck(
            state=SshSessionState.AUTH_REQUIRED,
            checked_at=checked_at,
            status_code="SSH.AUTH_REQUIRED",
            message="需要重新进行 MFA 验证",
        )

    def execute(
        self,
        argv: tuple[str, ...],
        *,
        cwd: str | None = None,
        portal_owner: str,
        stdin: str | None = None,
        timeout_seconds: float | None = None,
    ) -> CommandResult:
        self._require_owner(portal_owner)
        _validate_remote_argv(argv, roots=self.config.expanded_owner_roots())
        canonical_cwd = (
            None
            if cwd is None
            else _validate_remote_path(cwd, roots=self.config.expanded_owner_roots())
        )
        return self._execute_validated(
            argv,
            cwd=canonical_cwd,
            stdin=stdin,
            timeout_seconds=timeout_seconds,
        )

    def execute_fixed_program(
        self,
        program: FixedRemoteProgram,
        args: tuple[str, ...],
        *,
        portal_owner: str,
        timeout_seconds: float | None = None,
    ) -> CommandResult:
        self._require_owner(portal_owner)
        source = self._fixed_programs.get(program)
        if source is None:
            raise SshRelayPolicyError(f"fixed remote program is unavailable: {program}")
        if len(args) > 32 or any("\x00" in arg for arg in args):
            raise SshRelayPolicyError("fixed remote program arguments are invalid")
        return self._execute_validated(
            ("python3", "-c", source, *args),
            cwd=None,
            stdin=None,
            timeout_seconds=timeout_seconds,
        )

    def _execute_validated(
        self,
        argv: tuple[str, ...],
        *,
        cwd: str | None,
        stdin: str | None,
        timeout_seconds: float | None,
    ) -> CommandResult:
        check = self.check()
        if check.state != SshSessionState.ACTIVE:
            raise SshRelayAuthRequired(check.status_code)
        remote_argv = argv
        if cwd is not None:
            remote_argv = ("cd", "--", cwd, "&&", *argv)
        remote_command = _quote_remote_command(remote_argv, with_cd=cwd is not None)
        command = [
            *self._base_ssh_options(),
            "-o",
            "ControlMaster=no",
            "-T",
            self.config.target,
            "--",
            remote_command,
        ]
        timeout = timeout_seconds or self.config.timeout_seconds
        try:
            completed = subprocess.run(
                command,
                check=False,
                text=True,
                input=stdin,
                capture_output=True,
                timeout=timeout + 2,
            )
        except subprocess.TimeoutExpired as exc:
            raise SlurmTransportError("SSH.REMOTE_TIMEOUT") from exc
        except OSError as exc:
            raise SlurmTransportError("SSH.CLIENT_UNAVAILABLE") from exc
        if completed.returncode == 255:
            follow_up = self.check()
            if follow_up.state != SshSessionState.ACTIVE:
                raise SshRelayAuthRequired(follow_up.status_code)
            raise SlurmTransportError("SSH.TRANSPORT_FAILED")
        return CommandResult(
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )

    def run_file_shell(
        self,
        shell_command: str,
        *,
        stdin: str | None = None,
        timeout_seconds: float | None = None,
    ) -> CommandResult:
        """Execute an application-built file-operation shell snippet.

        Unlike :meth:`execute` (structured argv), file transfer requires pipes
        and redirection (``base64 -d >> file``).  The snippet is always built
        by 107Pilot code from validated paths and base64-checked data; remote
        tokens are shell-quoted by the caller.
        """
        check = self.check()
        if check.state != SshSessionState.ACTIVE:
            raise SshRelayAuthRequired(check.status_code)
        command = [
            *self._base_ssh_options(),
            "-o",
            "ControlMaster=no",
            "-T",
            self.config.target,
            "--",
            shell_command,
        ]
        timeout = timeout_seconds or self.config.timeout_seconds
        try:
            completed = subprocess.run(
                command,
                check=False,
                text=True,
                input=stdin,
                capture_output=True,
                timeout=timeout + 2,
            )
        except subprocess.TimeoutExpired as exc:
            raise SlurmTransportError("SSH.REMOTE_TIMEOUT") from exc
        except OSError as exc:
            raise SlurmTransportError("SSH.CLIENT_UNAVAILABLE") from exc
        if completed.returncode == 255:
            follow_up = self.check()
            if follow_up.state != SshSessionState.ACTIVE:
                raise SshRelayAuthRequired(follow_up.status_code)
            raise SlurmTransportError("SSH.TRANSPORT_FAILED")
        return CommandResult(
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )

    def _require_owner(self, portal_owner: str) -> None:
        if portal_owner != self.config.portal_owner:
            raise SshRelayPolicyError("SSH relay owner mismatch")

    def _base_ssh_options(self) -> list[str]:
        command = [
            "ssh",
            "-S",
            str(self.config.control_path),
            "-o",
            "BatchMode=yes",
            "-o",
            f"ConnectTimeout={max(1, int(self.config.timeout_seconds))}",
        ]
        if self.config.known_hosts_file is not None:
            command.extend(
                [
                    "-o",
                    "StrictHostKeyChecking=yes",
                    "-o",
                    f"UserKnownHostsFile={self.config.known_hosts_file}",
                ]
            )
        if self.config.port is not None:
            command.extend(["-p", str(self.config.port)])
        return command


class SshRelayExecutor:
    """``SimulatorExecutor``-compatible adapter over the typed SSH relay."""

    def __init__(self, client: SshRelayClient) -> None:
        self.client = client
        self.config = client.config

    def run(
        self,
        argv: list[str],
        *,
        cwd: str | None = None,
        user: str | None = None,
        stdin: str | None = None,
        timeout_seconds: float = 10.0,
    ) -> CommandResult:
        portal_owner = user or self.config.portal_owner
        return self.client.execute(
            tuple(argv),
            cwd=cwd,
            portal_owner=portal_owner,
            stdin=stdin,
            timeout_seconds=timeout_seconds,
        )

    def realpath(self, path: str, *, timeout_seconds: float = 10.0) -> str:
        requested = _absolute_remote_path(path)
        result = self.client.execute(
            ("realpath", "-m", "--", requested),
            portal_owner=self.config.portal_owner,
            timeout_seconds=timeout_seconds,
        )
        if result.returncode != 0:
            raise SlurmTransportError("SSH.REALPATH_FAILED")
        resolved = result.stdout.strip()
        if not resolved:
            raise SlurmTransportError("SSH.REALPATH_EMPTY")
        return _absolute_remote_path(resolved)

    def write_text(
        self,
        *,
        path: str,
        content: str,
        owner: str,
        timeout_seconds: float = 10.0,
    ) -> None:
        if owner != self.config.slurm_user and owner != self.config.portal_owner:
            raise SshRelayPolicyError("SSH write owner mismatch")
        safe_path = _validate_remote_path(path, roots=self.config.expanded_owner_roots())
        write_result = self.client.execute(
            ("tee", "--", safe_path),
            portal_owner=self.config.portal_owner,
            stdin=content,
            timeout_seconds=timeout_seconds,
        )
        if write_result.returncode != 0:
            raise SlurmTransportError("SSH.WRITE_FAILED")
        chmod_result = self.client.execute(
            ("chmod", "600", "--", safe_path),
            portal_owner=self.config.portal_owner,
            timeout_seconds=timeout_seconds,
        )
        if chmod_result.returncode != 0:
            raise SlurmTransportError("SSH.CHMOD_FAILED")

    def _file_shell(
        self,
        shell_command: str,
        *,
        stdin: str | None = None,
        timeout_seconds: float,
    ) -> CommandResult:
        runner = getattr(self.client, "run_file_shell", None)
        if runner is None:
            raise SshRelayPolicyError("SSH client lacks file transfer support")
        result: CommandResult = runner(
            shell_command, stdin=stdin, timeout_seconds=timeout_seconds
        )
        return result

    def _require_file_owner(self, owner: str) -> None:
        if owner != self.config.slurm_user and owner != self.config.portal_owner:
            raise SshRelayPolicyError("SSH file owner mismatch")

    def write_bytes_chunk(
        self,
        *,
        path: str,
        data_b64: str,
        offset: int,
        owner: str,
        timeout_seconds: float = 30.0,
    ) -> int:
        self._require_file_owner(owner)
        safe_path = _validate_remote_path(path, roots=self.config.expanded_owner_roots())
        if not _BASE64_ALPHABET.fullmatch(data_b64):
            raise SshRelayPolicyError("write payload is not valid base64")
        quoted = shlex.quote(safe_path)
        if offset < 0:
            redirect = ">>"
        elif offset == 0:
            redirect = ">"
        else:
            raise SshRelayPolicyError(
                "SSH relay writes sequentially; offset must be 0 or append(-1)"
            )
        shell_command = f"base64 -d {redirect} {quoted} && stat -c %s -- {quoted}"
        result = self._file_shell(
            shell_command, stdin=data_b64, timeout_seconds=timeout_seconds
        )
        if result.returncode != 0:
            raise SlurmTransportError("SSH.WRITE_BYTES_FAILED")
        size_lines = [line for line in result.stdout.splitlines() if line.strip().isdigit()]
        return int(size_lines[-1]) if size_lines else 0

    def read_bytes_chunk(
        self,
        *,
        path: str,
        offset: int,
        length: int,
        owner: str,
        timeout_seconds: float = 30.0,
    ) -> tuple[str, int]:
        self._require_file_owner(owner)
        safe_path = _validate_remote_path(path, roots=self.config.expanded_owner_roots())
        if offset < 0 or length <= 0:
            raise SshRelayPolicyError("offset must be >= 0 and length positive")
        quoted = shlex.quote(safe_path)
        shell_command = (
            f"head -c {int(length)} < {quoted} | base64 -w0; "
            f"printf '\\n'; stat -c %s -- {quoted}"
        )
        if offset > 0:
            shell_command = (
                f"tail -c +{int(offset) + 1} < {quoted} | "
                f"head -c {int(length)} | base64 -w0; "
                f"printf '\\n'; stat -c %s -- {quoted}"
            )
        result = self._file_shell(shell_command, timeout_seconds=timeout_seconds)
        if result.returncode != 0:
            raise SlurmTransportError("SSH.READ_BYTES_FAILED")
        lines = result.stdout.splitlines()
        data_b64 = lines[0].strip() if lines else ""
        size_lines = [line for line in lines[1:] if line.strip().isdigit()]
        size = int(size_lines[-1]) if size_lines else 0
        return data_b64, size

    def file_sha256(
        self, *, path: str, owner: str, timeout_seconds: float = 30.0
    ) -> str:
        self._require_file_owner(owner)
        safe_path = _validate_remote_path(path, roots=self.config.expanded_owner_roots())
        result = self.client.execute(
            ("sha256sum", "--", safe_path),
            portal_owner=self.config.portal_owner,
            timeout_seconds=timeout_seconds,
        )
        if result.returncode != 0:
            raise SlurmTransportError("SSH.SHA256_FAILED")
        digest = result.stdout.split()[0] if result.stdout.split() else ""
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise SlurmTransportError("SSH.SHA256_INVALID")
        return digest

    def list_dir(
        self, *, path: str, owner: str, timeout_seconds: float = 30.0
    ) -> list[FileEntry]:
        self._require_file_owner(owner)
        safe_path = _validate_remote_path(path, roots=self.config.expanded_owner_roots())
        script = (
            'python3 -c "import json,os,sys;p=sys.argv[1];'
            "print(json.dumps([{'name':n,"
            "'type':'symlink' if os.path.islink(os.path.join(p,n)) "
            "else ('dir' if os.path.isdir(os.path.join(p,n)) else 'file'),"
            "'size':os.lstat(os.path.join(p,n)).st_size,"
            "'mtime':int(os.lstat(os.path.join(p,n)).st_mtime)}"
            'for n in sorted(os.listdir(p))]))" '
        ) + shlex.quote(safe_path)
        result = self._file_shell(script, timeout_seconds=timeout_seconds)
        if result.returncode != 0:
            raise SlurmTransportError("SSH.LIST_DIR_FAILED")
        payload = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else "[]"
        try:
            decoded = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise SlurmTransportError("SSH.LIST_DIR_INVALID") from exc
        return [
            FileEntry(
                name=str(item.get("name", "")),
                type=str(item.get("type", "other")),
                size=int(item.get("size", 0)),
                mtime=int(item.get("mtime", 0)),
            )
            for item in decoded
            if isinstance(item, dict)
        ]

    def make_dir(
        self, *, path: str, owner: str, timeout_seconds: float = 30.0
    ) -> None:
        self._require_file_owner(owner)
        safe_path = _validate_remote_path(path, roots=self.config.expanded_owner_roots())
        result = self.client.execute(
            ("mkdir", "-p", "--", safe_path),
            portal_owner=self.config.portal_owner,
            timeout_seconds=timeout_seconds,
        )
        if result.returncode != 0:
            raise SlurmTransportError("SSH.MKDIR_FAILED")

    def remove_path(
        self, *, path: str, owner: str, timeout_seconds: float = 30.0
    ) -> None:
        self._require_file_owner(owner)
        safe_path = _validate_remote_path(path, roots=self.config.expanded_owner_roots())
        result = self._file_shell(
            f"rm -rf -- {shlex.quote(safe_path)}", timeout_seconds=timeout_seconds
        )
        if result.returncode != 0:
            raise SlurmTransportError("SSH.REMOVE_FAILED")

    def stat_path(
        self, *, path: str, owner: str, timeout_seconds: float = 30.0
    ) -> FileStat:
        self._require_file_owner(owner)
        safe_path = _validate_remote_path(path, roots=self.config.expanded_owner_roots())
        result = self.client.execute(
            ("stat", "-c", "%F|%s|%Y", "--", safe_path),
            portal_owner=self.config.portal_owner,
            timeout_seconds=timeout_seconds,
        )
        if result.returncode != 0:
            raise SlurmTransportError("SSH.STAT_FAILED")
        parts = result.stdout.strip().split("|")
        if len(parts) != 3:
            raise SlurmTransportError("SSH.STAT_INVALID")
        raw_type, size, mtime = parts
        kind = {
            "regular file": "file",
            "directory": "dir",
            "symbolic link": "symlink",
        }.get(raw_type, "other")
        return FileStat(path=safe_path, type=kind, size=int(size), mtime=int(mtime))

    def extract_archive(
        self,
        *,
        archive_path: str,
        dest_dir: str,
        owner: str,
        timeout_seconds: float = 120.0,
    ) -> int:
        self._require_file_owner(owner)
        safe_archive = _validate_remote_path(
            archive_path, roots=self.config.expanded_owner_roots()
        )
        safe_dest = _validate_remote_path(
            dest_dir, roots=self.config.expanded_owner_roots()
        )
        script = (
            'python3 -c "import sys,tarfile;a,d=sys.argv[1],sys.argv[2];t=tarfile.open(a);'
            "ms=t.getmembers();"
            "bad=any(m.name.startswith('/') or '..' in m.name.split('/') "
            "or m.issym() or m.islnk() for m in ms);"
            "sys.exit(2) if bad else t.extractall(d);"
            'print(len(ms))" '
        ) + shlex.quote(safe_archive) + " " + shlex.quote(safe_dest)
        result = self._file_shell(
            f"mkdir -p {shlex.quote(safe_dest)} && {script}",
            timeout_seconds=timeout_seconds,
        )
        if result.returncode != 0:
            raise SlurmTransportError("SSH.EXTRACT_FAILED")
        count_lines = [line for line in result.stdout.splitlines() if line.strip().isdigit()]
        return int(count_lines[-1]) if count_lines else 0

    def create_archive(
        self,
        *,
        paths: list[str],
        dest_dir: str,
        archive_name: str,
        owner: str,
        timeout_seconds: float = 120.0,
    ) -> tuple[str, int]:
        self._require_file_owner(owner)
        if "/" in archive_name or "\\" in archive_name or ".." in archive_name:
            raise SshRelayPolicyError(f"unsafe archive name: {archive_name}")
        if not paths:
            raise SshRelayPolicyError("paths must be a non-empty list")
        safe_dest = _validate_remote_path(
            dest_dir, roots=self.config.expanded_owner_roots()
        )
        safe_sources = [
            _validate_remote_path(item, roots=self.config.expanded_owner_roots())
            for item in paths
        ]
        script = (
            'python3 -c "import sys,tarfile,os;d=sys.argv[1];n=sys.argv[2];'
            "srcs=sys.argv[3:];p=os.path.join(d,n);"
            "t=tarfile.open(p,'w:gz');"
            "[t.add(s,arcname=os.path.basename(s)) for s in srcs];t.close();"
            'print(p+chr(124)+str(os.path.getsize(p)))" '
        )
        quoted = " ".join(
            shlex.quote(token) for token in [safe_dest, archive_name, *safe_sources]
        )
        result = self._file_shell(
            f"mkdir -p {shlex.quote(safe_dest)} && {script}{quoted}",
            timeout_seconds=timeout_seconds,
        )
        if result.returncode != 0:
            raise SlurmTransportError("SSH.ARCHIVE_FAILED")
        last = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
        if "|" not in last:
            raise SlurmTransportError("SSH.ARCHIVE_INVALID")
        archive_path, _, size_text = last.rpartition("|")
        return archive_path, int(size_text) if size_text.isdigit() else 0


def _quote_remote_command(argv: tuple[str, ...], *, with_cd: bool) -> str:
    if not with_cd:
        return shlex.join(argv)
    # ``cd`` is a shell builtin.  Only the two fixed control operators created
    # above are emitted unquoted; every caller-controlled token is shell-quoted.
    if len(argv) < 5 or argv[0:2] != ("cd", "--") or argv[3] != "&&":
        raise SshRelayPolicyError("invalid remote cwd wrapper")
    return f"cd -- {shlex.quote(argv[2])} && exec {shlex.join(argv[4:])}"


def _validate_remote_argv(argv: tuple[str, ...], *, roots: tuple[str, ...]) -> None:
    if not argv or len(argv) > 64:
        raise SshRelayPolicyError("remote argv is empty or too large")
    if any(not isinstance(arg, str) or "\x00" in arg or "\n" in arg for arg in argv):
        raise SshRelayPolicyError("remote argv contains an invalid token")
    command = argv[0]
    if command == "realpath":
        if len(argv) != 4 or argv[1:3] != ("-m", "--"):
            raise SshRelayPolicyError("unsupported realpath operation")
        _absolute_remote_path(argv[3])
        return
    if command in {"tee", "chmod", "mkdir"}:
        _validate_file_mutation_argv(argv, roots=roots)
        return
    if command == "sbatch":
        _validate_sbatch_argv(argv, roots=roots)
        return
    if command in {"squeue", "sacct", "scontrol", "scancel"}:
        _validate_slurm_query_argv(argv)
        return
    if command in {"pwd", "whoami", "hostname", "id", "env"} and len(argv) == 1:
        return
    if command == "date" and argv == ("date", "-Is"):
        return
    if command == "python" and argv == ("python", "-V"):
        return
    if command == "which" and argv == ("which", "python"):
        return
    if command == "nvidia-smi" and argv == (
        "nvidia-smi",
        "--query-gpu=name,uuid,driver_version,memory.total",
        "--format=csv,noheader",
    ):
        return
    if command == "stat":
        if len(argv) != 5 or argv[1] != "-c" or argv[3] != "--":
            raise SshRelayPolicyError("unsupported stat operation")
        if argv[2] not in {"%s|%Y", "%F|%s|%Y|%U|%G"}:
            raise SshRelayPolicyError("unsupported stat format")
        _validate_remote_path(argv[4], roots=roots)
        return
    if command == "test":
        if len(argv) != 3 or argv[1] not in {"-e", "-d", "-r", "-x", "-w"}:
            raise SshRelayPolicyError("unsupported path test")
        _validate_remote_path(argv[2], roots=roots)
        return
    if command == "tail":
        if len(argv) != 5 or argv[1] != "-c" or argv[3] != "--":
            raise SshRelayPolicyError("unsupported tail operation")
        if not argv[2].isdigit() or int(argv[2]) <= 0:
            raise SshRelayPolicyError("invalid tail limit")
        _validate_remote_path(argv[4], roots=roots)
        return
    if command == "sha256sum":
        if len(argv) != 3 or argv[1] != "--":
            raise SshRelayPolicyError("unsupported sha256 operation")
        _validate_remote_path(argv[2], roots=roots)
        return
    raise SshRelayPolicyError(f"unsupported SSH relay command: {command}")


def _validate_file_mutation_argv(argv: tuple[str, ...], *, roots: tuple[str, ...]) -> None:
    if argv[0] == "tee":
        if len(argv) != 3 or argv[1] != "--":
            raise SshRelayPolicyError("unsupported write operation")
        _validate_remote_path(argv[2], roots=roots)
        return
    if argv[0] == "chmod":
        if len(argv) != 4 or argv[1:3] != ("600", "--"):
            raise SshRelayPolicyError("unsupported chmod operation")
        _validate_remote_path(argv[3], roots=roots)
        return
    if len(argv) != 4 or argv[1:3] != ("-p", "--"):
        raise SshRelayPolicyError("unsupported mkdir operation")
    _validate_remote_path(argv[3], roots=roots)


def _validate_sbatch_argv(argv: tuple[str, ...], *, roots: tuple[str, ...]) -> None:
    if len(argv) < 4 or argv[1] != "--parsable":
        raise SshRelayPolicyError("unsupported sbatch operation")
    _validate_remote_path(argv[-1], roots=roots)
    index = 2
    flag_only = {"--exclusive"}
    valued = {
        "--job-name",
        "--chdir",
        "--partition",
        "--qos",
        "--time",
        "--nodes",
        "--ntasks",
        "--ntasks-per-node",
        "--cpus-per-task",
        "--mem",
        "--mem-per-cpu",
        "--gres",
        "--gpus",
        "--gpus-per-node",
        "--constraint",
        "--dependency",
        "--array",
    }
    while index < len(argv) - 1:
        option = argv[index]
        if option in flag_only:
            index += 1
            continue
        if option not in valued or index + 1 >= len(argv) - 1:
            raise SshRelayPolicyError(f"unsupported sbatch option: {option}")
        value = argv[index + 1]
        if option == "--chdir":
            _validate_remote_path(value, roots=roots)
        elif not _SAFE_SLURM_ATOM.fullmatch(value):
            raise SshRelayPolicyError(f"unsafe sbatch value for {option}")
        index += 2


def _validate_slurm_query_argv(argv: tuple[str, ...]) -> None:
    command = argv[0]
    if command == "scancel":
        if len(argv) != 2 or not _SAFE_JOB_ID.fullmatch(argv[1]):
            raise SshRelayPolicyError("invalid scancel operation")
        return
    if command == "scontrol":
        if (
            len(argv) != 5
            or argv[1:4] != ("-o", "show", "job")
            or not _SAFE_JOB_ID.fullmatch(argv[4])
        ):
            raise SshRelayPolicyError("invalid scontrol operation")
        return
    if command == "squeue":
        allowed_flags = {"-h", "-j", "-u", "-o"}
    else:
        allowed_flags = {"-nP", "-nPX", "-j", "-u", "-S", "-o", "-X"}
    index = 1
    while index < len(argv):
        flag = argv[index]
        if flag not in allowed_flags:
            raise SshRelayPolicyError(f"unsupported {command} flag: {flag}")
        if flag in {"-h", "-nP", "-nPX", "-X"}:
            index += 1
            continue
        if index + 1 >= len(argv):
            raise SshRelayPolicyError(f"missing value for {command} flag: {flag}")
        value = argv[index + 1]
        if flag == "-j" and not _SAFE_JOB_ID.fullmatch(value):
            raise SshRelayPolicyError("invalid Slurm job id")
        elif flag == "-u" and not is_safe_username(value):
            raise SshRelayPolicyError("invalid Slurm user")
        elif flag == "-o" and not _SAFE_FORMAT.fullmatch(value):
            raise SshRelayPolicyError("invalid Slurm output format")
        elif flag == "-S" and (
            len(value) > 40 or not re.fullmatch(r"[0-9T:+-]+", value)
        ):
            raise SshRelayPolicyError("invalid Slurm time filter")
        index += 2


def _absolute_remote_path(path: str) -> str:
    if "\x00" in path:
        raise SshRelayPolicyError("remote path contains NUL")
    candidate = PurePosixPath(path)
    if not candidate.is_absolute() or ".." in candidate.parts:
        raise SshRelayPolicyError("remote path must be absolute without parent traversal")
    normalized = str(candidate)
    if normalized == "/":
        raise SshRelayPolicyError("remote filesystem root is not an allowed target")
    return normalized


def _validate_remote_path(path: str, *, roots: tuple[str, ...]) -> str:
    candidate = _absolute_remote_path(path)
    for root in roots:
        if candidate == root or candidate.startswith(f"{root.rstrip('/')}/"):
            return candidate
    raise SshRelayPolicyError("remote path is outside the owner roots")
