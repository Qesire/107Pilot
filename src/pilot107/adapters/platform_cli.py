"""Allowlisted CLI collector for read-only platform observations."""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from pilot107.adapters.slurm import SimulatorExecutor, SlurmTransportError
from pilot107.core.platform_snapshot import CommandObservation

_USERNAME = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


class PlatformCommand(StrEnum):
    HOSTNAME = "hostname"
    PWD = "pwd"
    WHOAMI = "whoami"
    DATE_ISO = "date_iso"
    PYTHON_VERSION = "python_version"
    WHICH_PYTHON = "which_python"
    CONDA_ENV_LIST_JSON = "conda_env_list_json"
    SCONTROL_SHOW_PART = "scontrol_show_part"
    SCONTROL_SHOW_NODES = "scontrol_show_nodes"
    SINFO_PIPE = "sinfo_pipe"
    SQUEUE_USER_PIPE = "squeue_user_pipe"
    SACCTMGR_QOS = "sacctmgr_qos"
    SACCTMGR_USER_ASSOC_PIPE = "sacctmgr_user_assoc_pipe"
    DF_PUBLIC_HOME = "df_public_home"


@dataclass(frozen=True)
class PlatformCommandSpec:
    name: PlatformCommand
    argv: tuple[str, ...]
    timeout_seconds: float = 10.0

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0 or self.timeout_seconds > 60:
            raise ValueError("platform command timeout must be between 0 and 60 seconds")
        if not _is_allowlisted_argv(self.name, self.argv):
            raise ValueError(f"argv is not allowlisted for platform command {self.name.value}")


class PlatformObservationCollector(Protocol):
    def collect(
        self,
        specs: tuple[PlatformCommandSpec, ...],
    ) -> tuple[CommandObservation, ...]: ...


class PlatformCliCollector:
    def __init__(
        self,
        *,
        max_output_chars: int = 200_000,
        env: dict[str, str] | None = None,
    ) -> None:
        if max_output_chars <= 0:
            raise ValueError("max_output_chars must be positive")
        self.max_output_chars = max_output_chars
        self.env = env or {"PATH": os.environ.get("PATH", ""), "LC_ALL": "C", "LANG": "C"}

    def collect(self, specs: tuple[PlatformCommandSpec, ...]) -> tuple[CommandObservation, ...]:
        return tuple(self.run(spec) for spec in specs)

    def run(self, spec: PlatformCommandSpec) -> CommandObservation:
        if not spec.argv:
            raise ValueError("argv must not be empty")
        try:
            completed = subprocess.run(
                list(spec.argv),
                check=False,
                capture_output=True,
                text=True,
                timeout=spec.timeout_seconds,
                env=self.env,
            )
        except subprocess.TimeoutExpired as exc:
            stdout, stdout_truncated = self._truncate(exc.stdout or "")
            stderr, stderr_truncated = self._truncate(exc.stderr or "")
            return CommandObservation(
                name=spec.name.value,
                argv=spec.argv,
                returncode=124,
                stdout=stdout,
                stderr=stderr,
                timed_out=True,
                truncated=stdout_truncated or stderr_truncated,
            )
        except OSError:
            return CommandObservation(
                name=spec.name.value,
                argv=spec.argv,
                returncode=127,
                stdout="",
                stderr="command unavailable",
            )
        stdout, stdout_truncated = self._truncate(completed.stdout)
        stderr, stderr_truncated = self._truncate(completed.stderr)
        return CommandObservation(
            name=spec.name.value,
            argv=spec.argv,
            returncode=completed.returncode,
            stdout=stdout,
            stderr=stderr,
            truncated=stdout_truncated or stderr_truncated,
        )

    def _truncate(self, value: str | bytes) -> tuple[str, bool]:
        text = value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value
        if len(text) <= self.max_output_chars:
            return text, False
        return text[: self.max_output_chars], True


class ExecutorPlatformCliCollector:
    """Collect allowlisted facts through a simulator execution boundary."""

    def __init__(
        self,
        *,
        executor: SimulatorExecutor,
        user: str,
        cwd: str,
        max_output_chars: int = 200_000,
    ) -> None:
        if not _USERNAME.fullmatch(user):
            raise ValueError("collector user is invalid")
        if not cwd.startswith("/") or "\x00" in cwd or len(cwd) > 4096:
            raise ValueError("collector cwd must be an absolute path")
        if max_output_chars <= 0:
            raise ValueError("max_output_chars must be positive")
        self.executor = executor
        self.user = user
        self.cwd = cwd
        self.max_output_chars = max_output_chars

    def collect(
        self,
        specs: tuple[PlatformCommandSpec, ...],
    ) -> tuple[CommandObservation, ...]:
        return tuple(self.run(spec) for spec in specs)

    def run(self, spec: PlatformCommandSpec) -> CommandObservation:
        try:
            completed = self.executor.run(
                list(spec.argv),
                cwd=self.cwd,
                user=self.user,
                timeout_seconds=spec.timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            return CommandObservation(
                name=spec.name.value,
                argv=spec.argv,
                returncode=124,
                stdout="",
                stderr="command timed out",
                timed_out=True,
            )
        except (OSError, SlurmTransportError):
            return CommandObservation(
                name=spec.name.value,
                argv=spec.argv,
                returncode=125,
                stdout="",
                stderr="collector transport unavailable",
            )
        stdout, stdout_truncated = _truncate(completed.stdout, self.max_output_chars)
        stderr, stderr_truncated = _truncate(completed.stderr, self.max_output_chars)
        return CommandObservation(
            name=spec.name.value,
            argv=spec.argv,
            returncode=completed.returncode,
            stdout=stdout,
            stderr=stderr,
            truncated=stdout_truncated or stderr_truncated,
        )


def default_login_snapshot_specs(
    *,
    username: str,
    home: str | None = None,
) -> tuple[PlatformCommandSpec, ...]:
    specs = [
        PlatformCommandSpec(PlatformCommand.HOSTNAME, ("hostname",)),
        PlatformCommandSpec(PlatformCommand.PWD, ("pwd",)),
        PlatformCommandSpec(PlatformCommand.WHOAMI, ("whoami",)),
        PlatformCommandSpec(PlatformCommand.DATE_ISO, ("date", "-Is")),
        PlatformCommandSpec(PlatformCommand.PYTHON_VERSION, ("python", "-V")),
        PlatformCommandSpec(PlatformCommand.WHICH_PYTHON, ("which", "python")),
        PlatformCommandSpec(
            PlatformCommand.CONDA_ENV_LIST_JSON,
            ("conda", "env", "list", "--json"),
        ),
        PlatformCommandSpec(PlatformCommand.SCONTROL_SHOW_PART, ("scontrol", "show", "part")),
        PlatformCommandSpec(PlatformCommand.SCONTROL_SHOW_NODES, ("scontrol", "show", "nodes")),
        PlatformCommandSpec(
            PlatformCommand.SINFO_PIPE,
            ("sinfo", "-h", "-o", "%N|%P|%t|%c|%m|%G|%E"),
        ),
        PlatformCommandSpec(
            PlatformCommand.SQUEUE_USER_PIPE,
            ("squeue", "-h", "-u", username, "-o", "%i|%T|%R|%P|%j"),
        ),
    ]
    if home:
        specs.append(
            PlatformCommandSpec(
                PlatformCommand.DF_PUBLIC_HOME,
                ("df", "-P", "-h", "/public", home),
            )
        )
    return tuple(specs)


def _is_allowlisted_argv(name: PlatformCommand, argv: tuple[str, ...]) -> bool:
    exact = {
        PlatformCommand.HOSTNAME: ("hostname",),
        PlatformCommand.PWD: ("pwd",),
        PlatformCommand.WHOAMI: ("whoami",),
        PlatformCommand.DATE_ISO: ("date", "-Is"),
        PlatformCommand.PYTHON_VERSION: ("python", "-V"),
        PlatformCommand.WHICH_PYTHON: ("which", "python"),
        PlatformCommand.CONDA_ENV_LIST_JSON: ("conda", "env", "list", "--json"),
        PlatformCommand.SCONTROL_SHOW_PART: ("scontrol", "show", "part"),
        PlatformCommand.SCONTROL_SHOW_NODES: ("scontrol", "show", "nodes"),
        PlatformCommand.SINFO_PIPE: ("sinfo", "-h", "-o", "%N|%P|%t|%c|%m|%G|%E"),
        PlatformCommand.SACCTMGR_QOS: ("sacctmgr", "show", "qos"),
    }
    if name in exact:
        return argv == exact[name]
    if name == PlatformCommand.SQUEUE_USER_PIPE:
        return (
            len(argv) == 6
            and argv[:3] == ("squeue", "-h", "-u")
            and _USERNAME.fullmatch(argv[3]) is not None
            and argv[4:] == ("-o", "%i|%T|%R|%P|%j")
        )
    if name == PlatformCommand.SACCTMGR_USER_ASSOC_PIPE:
        return (
            len(argv) == 7
            and argv[:4] == ("sacctmgr", "-nP", "show", "user")
            and argv[4].startswith("name=")
            and _USERNAME.fullmatch(argv[4][len("name=") :]) is not None
            and argv[5] == "WithAssoc"
            and argv[6] == "format=User,DefaultAccount,Account,Partition,QOS,DefaultQOS"
        )
    if name == PlatformCommand.DF_PUBLIC_HOME:
        return (
            len(argv) == 5
            and argv[:4] == ("df", "-P", "-h", "/public")
            and _safe_absolute_path(argv[4])
        )
    return False


def _safe_absolute_path(value: str) -> bool:
    return value.startswith("/") and "\x00" not in value and len(value) <= 4096


def _truncate(value: str | bytes, limit: int) -> tuple[str, bool]:
    text = value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value
    if len(text) <= limit:
        return text, False
    return text[:limit], True


def user_entitlement_snapshot_specs(username: str) -> tuple[PlatformCommandSpec, ...]:
    return (
        PlatformCommandSpec(
            PlatformCommand.SACCTMGR_USER_ASSOC_PIPE,
            (
                "sacctmgr",
                "-nP",
                "show",
                "user",
                f"name={username}",
                "WithAssoc",
                "format=User,DefaultAccount,Account,Partition,QOS,DefaultQOS",
            ),
        ),
    )
