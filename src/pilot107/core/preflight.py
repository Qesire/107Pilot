"""WorkDirPreflight — submission-time workdir safety checks.

Implements the WorkDirPreflight contract from
``docs/phase-1/submission_strategy.md`` §4. The service layer (Lane 4b-ii)
calls :func:`preflight_workdir` (pure path checks) or
:func:`preflight_workdir_fs` (filesystem-backed checks via an injectable
:class:`PathChecker`) before forwarding a submit intent to a Slurm backend.

Design notes
------------
* Findings reuse :class:`pilot107.core.resources.PreflightFinding` so the
  service layer can aggregate workdir findings with ``validate_resource_plan``
  findings uniformly (same shape, same severities).
* The pure-path variant (:func:`preflight_workdir_paths` /
  :func:`preflight_workdir`) performs ONLY lexical path membership checks —
  no filesystem access. It is safe to call from the API process or any
  boundary that cannot stat the target filesystem.
* The FS variant (:func:`preflight_workdir_fs`) accepts an injectable
  :class:`PathChecker` so the same logic works against the local filesystem
  OR a remote command gateway (e.g. ``SimulatorExecutor``-style HTTP proxy).
  It never calls ``os.access`` directly.

Choice for Matrix #4 ("REST + 本地 /tmp 输出 → 警告或拒绝"): a workdir or
output_parent under ``/tmp`` is a hard BLOCK (``WORKDIR_LOCAL_TMP``). BLOCK
was chosen over WARN for clarity — a job whose workdir lives on login-node
``/tmp`` is invisible to compute nodes and would silently fail.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from pilot107.core.resources import PreflightFinding, PreflightSeverity

_BLOCK = PreflightSeverity.BLOCK
_WARN = PreflightSeverity.WARN
_PREFLIGHT_AUTHORITY = "submission_strategy.md#4"

# Local-ephemeral prefixes that are always rejected as a workdir/output root
# even if a misconfigured profile lists them under allowed_roots. ``/tmp`` is
# the canonical login-node-local path (Matrix #4); ``/dev/shm`` and ``/run``
# are treated identically because they share the "compute node cannot see
# this path" semantic.
_LOCAL_EPHEMERAL_PREFIXES: tuple[str, ...] = ("/tmp", "/dev/shm", "/run", "/var/tmp")

# Windows drive letter (e.g. ``C:\``) — a dead giveaway that the user pasted
# a local computer path into the submit request.
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")


class PathChecker(Protocol):
    """Injectable filesystem probe for :func:`preflight_workdir_fs`.

    Lane 4b-ii wires this to either:

    * a local-filesystem implementation (``LocalPathChecker`` below) when the
      service runs on the login node, or
    * a command-gateway implementation that issues ``stat``/``test`` calls
      through ``SimulatorExecutor`` (HTTP command gateway) so the same
      preflight runs against the container the Slurm backend will actually
      use.

    Every method must accept ``str | Path`` and return a plain bool. They
    must NOT raise on missing paths — return ``False`` instead — so the
    preflight can produce a finding rather than aborting.
    """

    def exists(self, path: str | Path) -> bool: ...
    def is_dir(self, path: str | Path) -> bool: ...
    def readable(self, path: str | Path) -> bool: ...
    def executable(self, path: str | Path) -> bool: ...
    def writable(self, path: str | Path) -> bool: ...


class LocalPathChecker:
    """Concrete :class:`PathChecker` backed by the local filesystem.

    Uses :mod:`os` access checks. Wrapped here (rather than calling
    ``os.access`` inline in preflight) so the preflight logic stays pure and
    testable with a fake checker.
    """

    def __init__(self) -> None:
        import os

        self._os = os

    def exists(self, path: str | Path) -> bool:
        return Path(path).exists()

    def is_dir(self, path: str | Path) -> bool:
        return Path(path).is_dir()

    def readable(self, path: str | Path) -> bool:
        return self._os.access(str(path), self._os.R_OK)

    def executable(self, path: str | Path) -> bool:
        return self._os.access(str(path), self._os.X_OK)

    def writable(self, path: str | Path) -> bool:
        return self._os.access(str(path), self._os.W_OK)


# --------------------------------------------------------------------------- #
# Internal lexical helpers (pure, no FS)
# --------------------------------------------------------------------------- #


def _normalize_roots(roots: Sequence[str | Path]) -> list[Path]:
    """Return absolute :class:`Path` objects for a root sequence.

    No ``resolve()`` is performed — the pure variant is lexical only. The
    FS variant is responsible for realpath semantics.
    """

    return [Path(root) for root in roots]


def _is_lexically_under(path: Path, root: Path) -> bool:
    """True if ``path`` is ``root`` or lexically below ``root``.

    Both arguments should be absolute. ``relative_to`` raises ``ValueError``
    when ``path`` is not under ``root``; we treat that as "not under".
    """

    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _under_any(path: Path, roots: Sequence[Path]) -> bool:
    return any(_is_lexically_under(path, root) for root in roots)


def _detect_local_computer_path(
    path: Path, shared_roots: Sequence[Path]
) -> PreflightFinding | None:
    """Detect paths that look like they came from the user's laptop."""

    text = str(path)
    if _WINDOWS_DRIVE_RE.match(text):
        return PreflightFinding(
            severity=_BLOCK,
            code="WORKDIR_LOCAL_PATH",
            message=(
                f"workdir {text!r} looks like a local computer path "
                "(Windows drive letter); refusing to write it into sbatch"
            ),
            source_authority=_PREFLIGHT_AUTHORITY,
        )
    if text == "/Users" or text.startswith("/Users/"):
        return PreflightFinding(
            severity=_BLOCK,
            code="WORKDIR_LOCAL_PATH",
            message=(
                f"workdir {text!r} looks like a macOS local home path; "
                "refusing to write it into sbatch"
            ),
            source_authority=_PREFLIGHT_AUTHORITY,
        )
    # ``/home`` is a legitimate shared root on some clusters, so only
    # block it when no shared root covers it.
    if (text == "/home" or text.startswith("/home/")) and not _under_any(
        path, shared_roots
    ):
        return PreflightFinding(
            severity=_BLOCK,
            code="WORKDIR_LOCAL_PATH",
            message=(
                f"workdir {text!r} is under /home but not under any "
                "shared root; refusing to write a login-node-only path "
                "into sbatch"
            ),
            source_authority=_PREFLIGHT_AUTHORITY,
        )
    return None


def _detect_local_tmp(path: Path) -> PreflightFinding | None:
    text = str(path)
    for prefix in _LOCAL_EPHEMERAL_PREFIXES:
        if text == prefix or text.startswith(f"{prefix}/"):
            return PreflightFinding(
                severity=_BLOCK,
                code="WORKDIR_LOCAL_TMP",
                message=(
                    f"workdir {text!r} is under the local-ephemeral path "
                    f"{prefix!r}; compute nodes will not see this path "
                    "(Matrix #4: REST + local /tmp output → reject)"
                ),
                source_authority=_PREFLIGHT_AUTHORITY,
            )
    return None


def _classify_shared(
    path: Path,
    shared_roots: Sequence[Path],
    local_roots: Sequence[Path],
) -> PreflightFinding | None:
    under_shared = _under_any(path, shared_roots)
    under_local = _under_any(path, local_roots)
    if under_shared:
        # Shared filesystem semantic — compute node sees the same path.
        return None
    if under_local:
        return PreflightFinding(
            severity=_BLOCK,
            code="WORKDIR_NOT_SHARED",
            message=(
                f"workdir {path!s} is under a node-local root "
                "(local_roots) but not under any shared root; "
                "compute nodes will not see this path"
            ),
            source_authority=_PREFLIGHT_AUTHORITY,
        )
    # Allowed by `allowed_roots` but neither declared shared nor local.
    # This is a profile gap — warn rather than block so a partially-mapped
    # cluster does not hard-stop submissions, but surface the risk.
    return PreflightFinding(
        severity=_WARN,
        code="WORKDIR_SHARED_UNKNOWN",
        message=(
            f"workdir {path!s} is not classified as shared or local in the "
            "cluster profile; compute-node visibility cannot be verified"
        ),
        source_authority=_PREFLIGHT_AUTHORITY,
    )


# --------------------------------------------------------------------------- #
# Public preflight entry points
# --------------------------------------------------------------------------- #


def preflight_workdir_paths(
    *,
    workdir: str | Path,
    allowed_roots: Sequence[str | Path],
    shared_roots: Sequence[str | Path],
    local_roots: Sequence[str | Path],
    output_parent: str | Path | None = None,
    user: str | None = None,  # noqa: ARG001 - reserved for future per-user checks
) -> list[PreflightFinding]:
    """Pure-path WorkDirPreflight (no filesystem access).

    Performs the lexical subset of the §4 checks: absolute, allowed-root
    membership, local-computer-path rejection, ``/tmp`` rejection, and
    shared-vs-local classification. The "exists / readable / executable /
    writable" checks require FS access and are intentionally NOT performed
    here — use :func:`preflight_workdir_fs` for those.

    ``user`` is accepted for API symmetry with :func:`preflight_workdir_fs`
    and is reserved for future per-user home-path policy. It is currently
    unused.
    """

    findings: list[PreflightFinding] = []
    work_path = Path(workdir)
    shared = _normalize_roots(shared_roots)
    local = _normalize_roots(local_roots)
    allowed = _normalize_roots(allowed_roots)

    if not work_path.is_absolute():
        findings.append(
            PreflightFinding(
                severity=_BLOCK,
                code="WORKDIR_NOT_ABSOLUTE",
                message=(
                    f"workdir {work_path!s} is not an absolute path; "
                    "sbatch --chdir requires an absolute path"
                ),
                source_authority=_PREFLIGHT_AUTHORITY,
            )
        )
        return findings

    local_path_finding = _detect_local_computer_path(work_path, shared)
    if local_path_finding is not None:
        findings.append(local_path_finding)
        return findings

    tmp_finding = _detect_local_tmp(work_path)
    if tmp_finding is not None:
        findings.append(tmp_finding)
        return findings

    if not _under_any(work_path, allowed):
        findings.append(
            PreflightFinding(
                severity=_BLOCK,
                code="WORKDIR_NOT_ALLOWED",
                message=(
                    f"workdir {work_path!s} is outside the user's "
                    "allowed_roots; refusing to write it into sbatch"
                ),
                source_authority=_PREFLIGHT_AUTHORITY,
            )
        )
        return findings

    shared_finding = _classify_shared(work_path, shared, local)
    if shared_finding is not None:
        findings.append(shared_finding)

    if output_parent is not None:
        findings.extend(
            _preflight_output_parent_paths(
                output_parent=output_parent,
                allowed=allowed,
                shared=shared,
                local=local,
            )
        )

    return findings


def _preflight_output_parent_paths(
    *,
    output_parent: str | Path,
    allowed: Sequence[Path],
    shared: Sequence[Path],
    local: Sequence[Path],
) -> list[PreflightFinding]:
    """Lexical checks for the output parent directory."""

    findings: list[PreflightFinding] = []
    out_path = Path(output_parent)

    if not out_path.is_absolute():
        findings.append(
            PreflightFinding(
                severity=_BLOCK,
                code="WORKDIR_OUTPUT_NOT_ABSOLUTE",
                message=(
                    f"output_parent {out_path!s} is not an absolute path"
                ),
                source_authority=_PREFLIGHT_AUTHORITY,
            )
        )
        return findings

    tmp_finding = _detect_local_tmp(out_path)
    if tmp_finding is not None:
        # Re-badge with an output-specific code so the operator can tell
        # which field tripped the rule.
        findings.append(
            PreflightFinding(
                severity=_BLOCK,
                code="WORKDIR_OUTPUT_LOCAL_TMP",
                message=tmp_finding.message.replace("workdir", "output_parent"),
                source_authority=_PREFLIGHT_AUTHORITY,
            )
        )
        return findings

    if not _under_any(out_path, allowed):
        findings.append(
            PreflightFinding(
                severity=_BLOCK,
                code="WORKDIR_OUTPUT_NOT_ALLOWED",
                message=(
                    f"output_parent {out_path!s} is outside the user's "
                    "allowed_roots"
                ),
                source_authority=_PREFLIGHT_AUTHORITY,
            )
        )
        return findings

    shared_finding = _classify_shared(out_path, shared, local)
    if shared_finding is not None:
        findings.append(
            PreflightFinding(
                severity=shared_finding.severity,
                code=shared_finding.code.replace("WORKDIR_", "WORKDIR_OUTPUT_"),
                message=shared_finding.message.replace("workdir", "output_parent"),
                source_authority=_PREFLIGHT_AUTHORITY,
            )
        )

    return findings


def preflight_workdir(
    *,
    workdir: str | Path,
    allowed_roots: Sequence[str | Path],
    shared_roots: Sequence[str | Path],
    local_roots: Sequence[str | Path],
    output_parent: str | Path | None = None,
    user: str | None = None,
) -> list[PreflightFinding]:
    """Default WorkDirPreflight entry point (pure-path variant).

    Convenience alias for :func:`preflight_workdir_paths`. The service layer
    should call this first; if it returns no BLOCK findings and FS-backed
    writability/existence checks are required, follow up with
    :func:`preflight_workdir_fs`.
    """

    return preflight_workdir_paths(
        workdir=workdir,
        allowed_roots=allowed_roots,
        shared_roots=shared_roots,
        local_roots=local_roots,
        output_parent=output_parent,
        user=user,
    )


def preflight_workdir_fs(
    *,
    workdir: str | Path,
    allowed_roots: Sequence[str | Path],
    shared_roots: Sequence[str | Path],
    local_roots: Sequence[str | Path],
    path_checker: PathChecker,
    output_parent: str | Path | None = None,
    user: str | None = None,
) -> list[PreflightFinding]:
    """Filesystem-backed WorkDirPreflight.

    Runs the pure-path checks first (cheap, no FS), then adds the §4 checks
    that require filesystem access via ``path_checker``:

    * workdir exists or can be created (parent writable);
    * user can read the workdir;
    * user can execute (enter) the workdir;
    * output_parent is writable (or can be created).

    ``path_checker`` is injected so this works against the local FS
    (``LocalPathChecker``) or a command gateway that probes the container the
    Slurm backend will actually use.
    """

    findings = preflight_workdir_paths(
        workdir=workdir,
        allowed_roots=allowed_roots,
        shared_roots=shared_roots,
        local_roots=local_roots,
        output_parent=output_parent,
        user=user,
    )

    # If the pure variant already produced a BLOCK for the workdir itself,
    # FS checks on the same path are moot — skip them. We still run output
    # FS checks if the path-level output checks did not BLOCK.
    workdir_blocked = any(
        f.severity == _BLOCK and f.code.startswith("WORKDIR_") and "OUTPUT" not in f.code
        for f in findings
    )

    if not workdir_blocked:
        findings.extend(
            _preflight_workdir_fs_checks(
                workdir=workdir,
                path_checker=path_checker,
            )
        )

    output_path_blocked = any(
        f.severity == _BLOCK and "OUTPUT" in f.code for f in findings
    )
    if output_parent is not None and not output_path_blocked:
        findings.extend(
            _preflight_output_parent_fs_checks(
                output_parent=output_parent,
                path_checker=path_checker,
            )
        )

    return findings


def _preflight_workdir_fs_checks(
    *,
    workdir: str | Path,
    path_checker: PathChecker,
) -> list[PreflightFinding]:
    """FS checks for the workdir itself (exists / read / exec / creatable)."""

    findings: list[PreflightFinding] = []
    work_path = Path(workdir)

    if path_checker.exists(work_path):
        if not path_checker.is_dir(work_path):
            findings.append(
                PreflightFinding(
                    severity=_BLOCK,
                    code="WORKDIR_NOT_DIRECTORY",
                    message=f"workdir {work_path!s} exists but is not a directory",
                    source_authority=_PREFLIGHT_AUTHORITY,
                )
            )
            return findings
        if not path_checker.readable(work_path):
            findings.append(
                PreflightFinding(
                    severity=_BLOCK,
                    code="WORKDIR_NOT_READABLE",
                    message=(
                        f"user cannot read workdir {work_path!s}; "
                        "sbatch script write and job launch will fail"
                    ),
                    source_authority=_PREFLIGHT_AUTHORITY,
                )
            )
        if not path_checker.executable(work_path):
            findings.append(
                PreflightFinding(
                    severity=_BLOCK,
                    code="WORKDIR_NOT_EXECUTABLE",
                    message=(
                        f"user cannot enter (execute) workdir {work_path!s}; "
                        "the job cannot chdir into it"
                    ),
                    source_authority=_PREFLIGHT_AUTHORITY,
                )
            )
        return findings

    # workdir does not exist — §4 allows "exists or can be created".
    parent = work_path.parent
    if not path_checker.exists(parent):
        findings.append(
            PreflightFinding(
                severity=_BLOCK,
                code="WORKDIR_PARENT_NOT_FOUND",
                message=(
                    f"workdir {work_path!s} does not exist and its parent "
                    f"{parent!s} does not exist either; cannot create it"
                ),
                source_authority=_PREFLIGHT_AUTHORITY,
            )
        )
        return findings
    if not path_checker.writable(parent):
        findings.append(
            PreflightFinding(
                severity=_BLOCK,
                code="WORKDIR_PARENT_NOT_WRITABLE",
                message=(
                    f"workdir {work_path!s} does not exist and its parent "
                    f"{parent!s} is not writable; cannot create it"
                ),
                source_authority=_PREFLIGHT_AUTHORITY,
            )
        )
        return findings
    findings.append(
        PreflightFinding(
            severity=_WARN,
            code="WORKDIR_WILL_BE_CREATED",
            message=(
                f"workdir {work_path!s} does not exist but its parent is "
                "writable; it will be created at submit time"
            ),
            source_authority=_PREFLIGHT_AUTHORITY,
        )
    )
    return findings


def _preflight_output_parent_fs_checks(
    *,
    output_parent: str | Path,
    path_checker: PathChecker,
) -> list[PreflightFinding]:
    """FS checks for the output parent directory (writable / creatable)."""

    findings: list[PreflightFinding] = []
    out_path = Path(output_parent)

    if path_checker.exists(out_path):
        if not path_checker.is_dir(out_path):
            findings.append(
                PreflightFinding(
                    severity=_BLOCK,
                    code="WORKDIR_OUTPUT_NOT_DIRECTORY",
                    message=(
                        f"output_parent {out_path!s} exists but is not a directory"
                    ),
                    source_authority=_PREFLIGHT_AUTHORITY,
                )
            )
            return findings
        if not path_checker.writable(out_path):
            findings.append(
                PreflightFinding(
                    severity=_BLOCK,
                    code="WORKDIR_OUTPUT_NOT_WRITABLE",
                    message=(
                        f"output_parent {out_path!s} exists but is not "
                        "writable; the job cannot write stdout/stderr or "
                        "artifacts there"
                    ),
                    source_authority=_PREFLIGHT_AUTHORITY,
                )
            )
        return findings

    parent = out_path.parent
    if not path_checker.exists(parent) or not path_checker.writable(parent):
        findings.append(
            PreflightFinding(
                severity=_BLOCK,
                code="WORKDIR_OUTPUT_NOT_CREATABLE",
                message=(
                    f"output_parent {out_path!s} does not exist and its "
                    f"parent {parent!s} is not writable; cannot create it"
                ),
                source_authority=_PREFLIGHT_AUTHORITY,
            )
        )
        return findings
    findings.append(
        PreflightFinding(
            severity=_WARN,
            code="WORKDIR_OUTPUT_WILL_BE_CREATED",
            message=(
                f"output_parent {out_path!s} does not exist but its parent "
                "is writable; it will be created at submit time"
            ),
            source_authority=_PREFLIGHT_AUTHORITY,
        )
    )
    return findings


__all__ = [
    "LocalPathChecker",
    "PathChecker",
    "preflight_workdir",
    "preflight_workdir_fs",
    "preflight_workdir_paths",
]
