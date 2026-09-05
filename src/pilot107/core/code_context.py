"""Bounded, read-only source context for evidence-driven agent diagnosis.

The execution worktree, rather than a possibly stale remote Git URL, is the
source of truth for a Run.  This module captures a small immutable description
of that worktree and selects source windows only when a diagnostic log points
at a file and line.  It deliberately has no write, checkout, build, or submit
operation.
"""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol

from pilot107.core.evidence_binding import redact_evidence_text
from pilot107.core.run_store import RunRecord

_TRACEBACK_LOCATION = re.compile(r"""File ["'](?P<path>[^"'\n]+)["'], line (?P<line>[1-9][0-9]*)""")
_COMPILER_LOCATION = re.compile(
    r"(?P<path>(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+\.[A-Za-z0-9_+.-]+)"
    r":(?P<line>[1-9][0-9]*)(?::[1-9][0-9]*)?"
)
_EXCLUDED_PARTS = frozenset({".git", ".hg", ".svn", "node_modules", "__pycache__"})
_EXCLUDED_SUFFIXES = frozenset({".pem", ".key", ".p12", ".pfx", ".crt", ".der"})

# This program is fixed application code, passed as one argument to ``python3
# -c`` by SshWorkspaceReader.  The user-controlled values are positional
# arguments, and the program verifies containment after resolving symlinks.
_REMOTE_READ_PROGRAM = """
from pathlib import Path
import sys

root = Path(sys.argv[1]).resolve(strict=True)
relative = sys.argv[2]
limit = int(sys.argv[3])
candidate = root / relative
if candidate.is_symlink():
    raise SystemExit("refusing symlink source file")
resolved = candidate.resolve(strict=True)
if not resolved.is_relative_to(root) or not resolved.is_file():
    raise SystemExit("source path outside workspace or not a regular file")
sys.stdout.buffer.write(resolved.read_bytes()[:limit + 1])
""".strip()


class CodeContextError(RuntimeError):
    """A bounded code-context read could not be completed."""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(message or code)


@dataclass(frozen=True)
class SourceLocation:
    path: str
    line: int
    origin: str


@dataclass(frozen=True)
class CodeContextChunk:
    chunk_id: str
    source_ref: str
    path: str
    start_line: int
    end_line: int
    content: str
    sha256: str
    redactions: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, object]:
        return {
            "chunk_id": self.chunk_id,
            "source_ref": self.source_ref,
            "path": self.path,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "content": self.content,
            "sha256": self.sha256,
            "redactions": list(self.redactions),
        }


@dataclass(frozen=True)
class CodeContextBundle:
    run_id: str
    snapshot_id: str
    workspace: str
    revision: str
    dirty: bool
    worktree_fingerprint: str
    chunks: tuple[CodeContextChunk, ...]
    evidence_snippets: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "snapshot_id": self.snapshot_id,
            "workspace": self.workspace,
            "revision": self.revision,
            "dirty": self.dirty,
            "worktree_fingerprint": self.worktree_fingerprint,
            "chunks": [chunk.to_payload() for chunk in self.chunks],
            "evidence_snippets": list(self.evidence_snippets),
            "warnings": list(self.warnings),
        }


class WorkspaceReader(Protocol):
    """Read exactly the files selected by the CodeContextService."""

    def resolve_workspace(self, workspace: str) -> str:
        """Return a canonical absolute workspace path."""

    def git(self, workspace: str, args: tuple[str, ...]) -> str:
        """Run one of the service's fixed read-only Git projections."""

    def read_text(self, workspace: str, relative_path: str, *, max_bytes: int) -> str:
        """Read one regular source file under the canonical workspace."""


class LocalWorkspaceReader:
    """Read a local worktree beneath explicitly configured roots."""

    def __init__(
        self,
        *,
        allowed_roots: tuple[str | Path, ...],
        timeout_seconds: float = 10.0,
    ) -> None:
        self.allowed_roots = tuple(Path(root).expanduser().resolve() for root in allowed_roots)
        self.timeout_seconds = timeout_seconds

    def resolve_workspace(self, workspace: str) -> str:
        candidate = Path(workspace).expanduser()
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as exc:
            raise CodeContextError("workspace_missing", str(exc)) from exc
        if not resolved.is_dir():
            raise CodeContextError("workspace_not_directory")
        within_allowed_root = any(
            resolved == root or resolved.is_relative_to(root) for root in self.allowed_roots
        )
        if not within_allowed_root:
            raise CodeContextError("workspace_outside_allowed_roots")
        return str(resolved)

    def git(self, workspace: str, args: tuple[str, ...]) -> str:
        self._validate_git_args(args)
        completed = subprocess.run(
            ["git", "-C", workspace, *args],
            check=False,
            text=True,
            capture_output=True,
            timeout=self.timeout_seconds,
        )
        if completed.returncode != 0:
            raise CodeContextError("git_unavailable", completed.stderr.strip() or "git failed")
        return completed.stdout

    def read_text(self, workspace: str, relative_path: str, *, max_bytes: int) -> str:
        root = Path(workspace).resolve(strict=True)
        relative = _safe_relative_path(relative_path)
        candidate = root / relative
        if candidate.is_symlink():
            raise CodeContextError("source_symlink")
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as exc:
            raise CodeContextError("source_missing", str(exc)) from exc
        if not resolved.is_relative_to(root) or not resolved.is_file():
            raise CodeContextError("source_outside_workspace")
        try:
            data = resolved.read_bytes()[: max_bytes + 1]
        except OSError as exc:
            raise CodeContextError("source_unreadable", str(exc)) from exc
        return data[:max_bytes].decode("utf-8", errors="replace")

    @staticmethod
    def _validate_git_args(args: tuple[str, ...]) -> None:
        allowed = {
            ("rev-parse", "--verify", "HEAD"),
            ("status", "--porcelain=v1", "-z"),
            ("ls-files", "-z"),
            ("diff", "--no-ext-diff", "--no-textconv", "--binary", "--no-renames"),
        }
        if args not in allowed:
            raise CodeContextError("unsupported_git_projection")


@dataclass(frozen=True)
class SshWorkspaceConfig:
    """A pre-authenticated SSH control-master boundary.

    ``target`` is an SSH host/host-alias supplied by deployment configuration,
    never by an API request.  The reader uses ``-O check`` first so a missing
    master reports AUTH_REQUIRED instead of trying an unexpected interactive
    authentication flow from the API container.
    """

    target: str
    control_path: Path
    port: int | None = None
    timeout_seconds: float = 10.0


class SshWorkspaceReader:
    """Read remote worktrees through an existing authenticated SSH master."""

    def __init__(self, *, config: SshWorkspaceConfig, allowed_roots: tuple[str, ...]) -> None:
        if not config.target or any(char.isspace() for char in config.target):
            raise ValueError("SSH target must be a non-empty host or host alias")
        if config.port is not None and not (1 <= config.port <= 65535):
            raise ValueError("SSH port must be between 1 and 65535")
        self.config = config
        self.allowed_roots = tuple(_remote_absolute_path(root) for root in allowed_roots)

    def resolve_workspace(self, workspace: str) -> str:
        requested = _remote_absolute_path(workspace)
        output = self._run_remote(
            (
                "python3",
                "-c",
                (
                    "from pathlib import Path; import sys; "
                    "print(Path(sys.argv[1]).resolve(strict=True))"
                ),
                requested,
            )
        ).strip()
        resolved = _remote_absolute_path(output)
        if not _is_under_any_remote_root(resolved, self.allowed_roots):
            raise CodeContextError("workspace_outside_allowed_roots")
        return resolved

    def git(self, workspace: str, args: tuple[str, ...]) -> str:
        LocalWorkspaceReader._validate_git_args(args)
        return self._run_remote(("git", "-C", workspace, *args))

    def read_text(self, workspace: str, relative_path: str, *, max_bytes: int) -> str:
        relative = _safe_relative_path(relative_path)
        output = self._run_remote(
            ("python3", "-c", _REMOTE_READ_PROGRAM, workspace, relative, str(max_bytes))
        )
        return output[:max_bytes]

    def _run_remote(self, remote_argv: tuple[str, ...]) -> str:
        self._assert_control_master()
        command = [
            "ssh",
            "-S",
            str(self.config.control_path),
            "-o",
            "BatchMode=yes",
            "-o",
            "ControlMaster=no",
            "-o",
            f"ConnectTimeout={max(1, int(self.config.timeout_seconds))}",
        ]
        if self.config.port is not None:
            command.extend(["-p", str(self.config.port)])
        command.extend([self.config.target, "--", shlex.join(remote_argv)])
        try:
            completed = subprocess.run(
                command,
                check=False,
                text=True,
                capture_output=True,
                timeout=self.config.timeout_seconds + 2,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise CodeContextError("ssh_transport_unavailable", str(exc)) from exc
        if completed.returncode != 0:
            raise CodeContextError(
                "ssh_remote_read_failed", completed.stderr.strip() or "remote command failed"
            )
        return completed.stdout

    def _assert_control_master(self) -> None:
        command = ["ssh", "-S", str(self.config.control_path), "-O", "check"]
        if self.config.port is not None:
            command.extend(["-p", str(self.config.port)])
        command.append(self.config.target)
        try:
            completed = subprocess.run(
                command,
                check=False,
                text=True,
                capture_output=True,
                timeout=self.config.timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise CodeContextError("ssh_control_master_unavailable", str(exc)) from exc
        if completed.returncode != 0:
            raise CodeContextError(
                "ssh_auth_required",
                completed.stderr.strip() or "SSH control master is not available",
            )


@dataclass(frozen=True)
class CodeContextPolicy:
    max_chunks: int = 3
    context_before_lines: int = 60
    context_after_lines: int = 60
    max_file_bytes: int = 64 * 1024
    max_worktree_diff_bytes: int = 1024 * 1024

    def __post_init__(self) -> None:
        if self.max_chunks <= 0 or self.context_before_lines < 0 or self.context_after_lines < 0:
            raise ValueError("invalid code context limits")
        if self.max_file_bytes <= 0 or self.max_worktree_diff_bytes <= 0:
            raise ValueError("code context byte limits must be positive")


class CodeContextService:
    """Capture a run-scoped snapshot and traceback-selected source windows."""

    def __init__(self, *, reader: WorkspaceReader, policy: CodeContextPolicy | None = None) -> None:
        self.reader = reader
        self.policy = policy or CodeContextPolicy()

    def capture(self, run: RunRecord, *, evidence_texts: tuple[str, ...]) -> CodeContextBundle:
        warnings: list[str] = []
        try:
            workspace = self.reader.resolve_workspace(run.workdir)
            revision = self.reader.git(workspace, ("rev-parse", "--verify", "HEAD")).strip()
            status = self.reader.git(workspace, ("status", "--porcelain=v1", "-z"))
            tracked = self.reader.git(workspace, ("ls-files", "-z"))
            dirty_diff = self.reader.git(
                workspace,
                ("diff", "--no-ext-diff", "--no-textconv", "--binary", "--no-renames"),
            )
        except CodeContextError as exc:
            return _unavailable_bundle(run, warning=exc.code)

        if len(dirty_diff.encode("utf-8")) > self.policy.max_worktree_diff_bytes:
            return _unavailable_bundle(run, warning="dirty_worktree_diff_too_large")

        tracked_files = tuple(item for item in tracked.split("\0") if item)
        snapshot_seed = _sha256_json(
            {
                "workspace": workspace,
                "revision": revision,
                "status": status,
                "dirty_diff_sha256": hashlib.sha256(dirty_diff.encode("utf-8")).hexdigest(),
                "tracked_files": tracked_files,
            }
        )
        locations = locate_error_locations(
            evidence_texts=_prepare_evidence_for_location(evidence_texts, owner=run.owner),
            workspace=workspace,
            max_locations=self.policy.max_chunks,
        )
        selected_sources: list[tuple[SourceLocation, str]] = []
        for location in locations:
            if _excluded_source_path(location.path):
                warnings.append(f"source_path_excluded:{location.path}")
                continue
            try:
                text = self.reader.read_text(
                    workspace,
                    location.path,
                    max_bytes=self.policy.max_file_bytes,
                )
            except CodeContextError as exc:
                warnings.append(f"source_read_failed:{location.path}:{exc.code}")
                continue
            selected_sources.append((location, text))

        if not locations:
            warnings.append("no_traceback_location")
        fingerprint = _sha256_json(
            {
                "snapshot_seed": snapshot_seed,
                "selected_source_sha256": [
                    {
                        "path": location.path,
                        "line": location.line,
                        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    }
                    for location, text in selected_sources
                ],
            }
        )
        snapshot_id = f"codesnap_{fingerprint[:32]}"
        chunks = tuple(
            chunk
            for location, text in selected_sources
            if (
                chunk := _source_window(
                    snapshot_id=snapshot_id,
                    path=location.path,
                    line=location.line,
                    text=text,
                    before=self.policy.context_before_lines,
                    after=self.policy.context_after_lines,
                    owner=run.owner,
                )
            )
            is not None
        )
        return CodeContextBundle(
            run_id=run.run_id,
            snapshot_id=snapshot_id,
            workspace=workspace,
            revision=revision,
            dirty=bool(status),
            worktree_fingerprint=fingerprint,
            chunks=chunks,
            evidence_snippets=tuple(text for text in evidence_texts if text.strip()),
            warnings=tuple(dict.fromkeys(warnings)),
        )


def _prepare_evidence_for_location(
    evidence_texts: tuple[str, ...],
    *,
    owner: str,
) -> tuple[str, ...]:
    """Extract log tails and reverse home-redaction for path location.

    The evidence binder delivers whole JSON file content (with escaped
    quotes) and redacts ``/public/home/{owner}`` to ``<home>``.  The
    traceback location regex needs unescaped text with real paths.
    """
    home_placeholder = "<home>"
    real_home = f"/public/home/{owner}"
    prepared: list[str] = []
    for text in evidence_texts:
        fragments: list[str] = [text]
        # If the text is a JSON payload with a "tail" field, extract it.
        stripped = text.lstrip()
        if stripped.startswith("{"):
            try:
                payload = json.loads(text)
            except (json.JSONDecodeError, ValueError):
                payload = None
            if isinstance(payload, dict):
                tail = payload.get("tail")
                if isinstance(tail, str) and tail.strip():
                    fragments.append(tail)
        # Reverse the home-directory redaction so paths resolve.
        expanded = [
            fragment.replace(home_placeholder, real_home)
            if home_placeholder in fragment
            else fragment
            for fragment in fragments
        ]
        prepared.extend(expanded)
    return tuple(prepared)


def locate_error_locations(
    *,
    evidence_texts: tuple[str, ...],
    workspace: str,
    max_locations: int,
) -> tuple[SourceLocation, ...]:
    """Return distinct source locations below ``workspace`` in log order."""

    results: list[SourceLocation] = []
    seen: set[tuple[str, int]] = set()
    workspace_path = PurePosixPath(_remote_absolute_path(workspace))
    for text in evidence_texts:
        for match in _TRACEBACK_LOCATION.finditer(text):
            location = _normalize_location(
                raw_path=match.group("path"),
                line=int(match.group("line")),
                workspace=workspace_path,
                origin="traceback",
            )
            if location is not None and (location.path, location.line) not in seen:
                results.append(location)
                seen.add((location.path, location.line))
                if len(results) >= max_locations:
                    return tuple(results)
        for match in _COMPILER_LOCATION.finditer(text):
            location = _normalize_location(
                raw_path=match.group("path"),
                line=int(match.group("line")),
                workspace=workspace_path,
                origin="compiler",
            )
            if location is not None and (location.path, location.line) not in seen:
                results.append(location)
                seen.add((location.path, location.line))
                if len(results) >= max_locations:
                    return tuple(results)
    return tuple(results)


def _normalize_location(
    *,
    raw_path: str,
    line: int,
    workspace: PurePosixPath,
    origin: str,
) -> SourceLocation | None:
    candidate = PurePosixPath(raw_path)
    if candidate.is_absolute():
        try:
            relative = candidate.relative_to(workspace)
        except ValueError:
            return None
    else:
        relative = candidate
    try:
        safe = _safe_relative_path(relative.as_posix())
    except CodeContextError:
        return None
    return SourceLocation(path=safe, line=line, origin=origin)


def _source_window(
    *,
    snapshot_id: str,
    path: str,
    line: int,
    text: str,
    before: int,
    after: int,
    owner: str,
) -> CodeContextChunk | None:
    lines = text.splitlines()
    if line <= 0 or line > len(lines):
        return None
    start = max(1, line - before)
    end = min(len(lines), line + after)
    content = "\n".join(lines[start - 1 : end])
    redacted, redactions = redact_evidence_text(content, owner=owner)
    digest = hashlib.sha256(redacted.encode("utf-8")).hexdigest()
    chunk_id = f"code_{digest[:24]}"
    source_ref = f"code://snapshots/{snapshot_id}/{path}#L{start}-L{end}"
    return CodeContextChunk(
        chunk_id=chunk_id,
        source_ref=source_ref,
        path=path,
        start_line=start,
        end_line=end,
        content=redacted,
        sha256=digest,
        redactions=redactions,
    )


def _unavailable_bundle(run: RunRecord, *, warning: str) -> CodeContextBundle:
    fingerprint = _sha256_json({"run_id": run.run_id, "workdir": run.workdir, "warning": warning})
    return CodeContextBundle(
        run_id=run.run_id,
        snapshot_id=f"codesnap_unavailable_{fingerprint[:24]}",
        workspace=run.workdir,
        revision="unavailable",
        dirty=False,
        worktree_fingerprint=fingerprint,
        chunks=(),
        warnings=(warning,),
    )


def _safe_relative_path(value: str) -> str:
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or not value
        or "\x00" in value
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise CodeContextError("invalid_source_path")
    return path.as_posix()


def _remote_absolute_path(value: str) -> str:
    if not value or "\x00" in value:
        raise CodeContextError("invalid_workspace_path")
    path = PurePosixPath(value)
    if not path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise CodeContextError("invalid_workspace_path")
    return path.as_posix()


def _is_under_any_remote_root(path: str, roots: tuple[str, ...]) -> bool:
    candidate = PurePosixPath(path)
    for root in roots:
        root_path = PurePosixPath(root)
        try:
            candidate.relative_to(root_path)
        except ValueError:
            continue
        return True
    return False


def _excluded_source_path(path: str) -> bool:
    candidate = PurePosixPath(path)
    if any(part in _EXCLUDED_PARTS for part in candidate.parts):
        return True
    name = candidate.name.lower()
    return (
        name == ".env" or name.startswith(".env.") or candidate.suffix.lower() in _EXCLUDED_SUFFIXES
    )


def _sha256_json(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
