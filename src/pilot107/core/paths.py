"""SafePath authorization helpers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


class PathPolicyError(ValueError):
    """Raised when a path violates the SafePath policy."""


@dataclass(frozen=True)
class SafePath:
    original: str
    resolved: Path
    root: Path


def _reject_nul(path: str) -> None:
    if "\x00" in path:
        raise PathPolicyError("path contains NUL byte")


def _resolve_for_policy(path: Path) -> Path:
    """Resolve existing path or nearest existing parent.

    This allows checking intended output paths whose leaf does not exist yet,
    while still resolving symlinks in existing parents.
    """

    if path.exists():
        return path.resolve(strict=True)
    parent = path.parent
    if not parent.exists():
        raise PathPolicyError(f"parent does not exist: {parent}")
    return parent.resolve(strict=True) / path.name


def authorize_path(path: str, allowed_roots: list[str | Path]) -> SafePath:
    """Return a SafePath if path is inside one of allowed_roots after realpath.

    The check is based on resolved filesystem paths, not string prefixes.
    """

    _reject_nul(path)
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        raise PathPolicyError("path must be absolute")

    resolved_candidate = _resolve_for_policy(candidate)
    resolved_roots = [Path(root).expanduser().resolve(strict=True) for root in allowed_roots]

    for root in resolved_roots:
        try:
            resolved_candidate.relative_to(root)
        except ValueError:
            continue
        return SafePath(original=path, resolved=resolved_candidate, root=root)

    raise PathPolicyError("path is outside allowed roots")


def reject_special_file(path: Path) -> None:
    """Reject devices, FIFOs, sockets and other non-regular filesystem objects."""

    if not path.exists():
        return
    if path.is_file() or path.is_dir() or path.is_symlink():
        return
    raise PathPolicyError(f"unsupported special file: {path}")

