"""Audited, fixed-command terminal diagnostics for a simulator boundary.

This is intentionally not a browser-to-shell bridge. A browser may request
only a small catalog of readonly Slurm diagnostics; the command gateway owns
the execution boundary, identity switch and audit log.
"""

from __future__ import annotations

from dataclasses import dataclass

from pilot107.adapters.slurm import SimulatorExecutor
from pilot107.core.run_store import RunRecord


class TerminalCommandError(ValueError):
    """Raised when a caller requests an unavailable terminal diagnostic."""


@dataclass(frozen=True)
class TerminalCommandResult:
    command: str
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str

    def to_payload(self) -> dict[str, object]:
        return {
            "command": self.command,
            "argv": list(self.argv),
            "returncode": self.returncode,
            "stdout": self.stdout,
            "stderr": self.stderr,
        }


class TerminalCommandService:
    """Execute a finite readonly command catalog through a trusted gateway."""

    _MAX_OUTPUT_CHARS = 24_000
    _RUN_REQUIRED = frozenset({"run_status"})
    _COMMANDS = frozenset({"identity", "cluster", "my_jobs", "run_status"})

    def __init__(self, *, executor: SimulatorExecutor, timeout_seconds: float = 10.0) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.executor = executor
        self.timeout_seconds = timeout_seconds

    def execute(
        self,
        *,
        command: str,
        user: str,
        run: RunRecord | None = None,
    ) -> TerminalCommandResult:
        if command not in self._COMMANDS:
            raise TerminalCommandError("unsupported terminal command")
        if command in self._RUN_REQUIRED and (run is None or run.job_id is None):
            raise TerminalCommandError("a submitted Run is required for this command")
        argv = self._argv(command=command, user=user, run=run)
        completed = self.executor.run(
            argv,
            user=user,
            timeout_seconds=self.timeout_seconds,
        )
        return TerminalCommandResult(
            command=command,
            argv=tuple(argv),
            returncode=completed.returncode,
            stdout=completed.stdout[: self._MAX_OUTPUT_CHARS],
            stderr=completed.stderr[: self._MAX_OUTPUT_CHARS],
        )

    @staticmethod
    def _argv(*, command: str, user: str, run: RunRecord | None) -> list[str]:
        match command:
            case "identity":
                return ["id"]
            case "cluster":
                return ["sinfo", "-h", "-o", "%P|%c|%m|%G|%T"]
            case "my_jobs":
                return ["squeue", "-h", "-u", user, "-o", "%i|%j|%T|%R"]
            case "run_status":
                assert run is not None and run.job_id is not None
                return [
                    "sacct",
                    "-nP",
                    "-X",
                    "-j",
                    run.job_id,
                    "-o",
                    "JobIDRaw,State,ExitCode,Elapsed,NodeList",
                ]
        raise TerminalCommandError("unsupported terminal command")
