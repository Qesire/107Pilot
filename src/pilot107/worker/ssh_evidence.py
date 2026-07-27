"""Bounded EvidenceTransport implementation over the typed SSH relay."""

from __future__ import annotations

import base64
import json
from pathlib import Path, PurePosixPath
from typing import Any

from pilot107.adapters.ssh_relay import (
    FixedRemoteProgram,
    SshRelayClient,
    SshRelayPolicyError,
    SshSessionState,
)
from pilot107.core.identity import UserIdentity
from pilot107.core.paths import PathPolicyError, SafePath
from pilot107.worker.evidence import (
    EvidenceCapability,
    EvidencePolicy,
    EvidenceRoot,
    FileStat,
    InventoryFile,
    OutputInventory,
    TextTail,
)

# Immutable application code.  User-controlled paths and limits are positional
# arguments; containment and symlink checks are repeated on the remote host.
SSH_EVIDENCE_FS_PROGRAM = r"""
import base64
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import stat
import sys

op = sys.argv[1]
roots = [Path(item).resolve(strict=True) for item in json.loads(sys.argv[2])]

def emit(payload):
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))

def fail(code, message):
    print(json.dumps({"error": message}, separators=(",", ":")), file=sys.stderr)
    raise SystemExit(code)

def safe(path_text, *, must_exist=True):
    requested = Path(path_text)
    if not requested.is_absolute() or ".." in requested.parts:
        fail(45, "path_policy_denied")
    if requested.is_symlink():
        fail(45, "symlink_denied")
    try:
        resolved = requested.resolve(strict=must_exist)
    except FileNotFoundError:
        fail(44, "missing")
    if not any(resolved == root or resolved.is_relative_to(root) for root in roots):
        fail(45, "path_policy_denied")
    return resolved

path = safe(sys.argv[3], must_exist=(op != "prepare"))

if op == "prepare":
    run_id = sys.argv[4]
    safe_run_chars = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.:-"
    if not run_id or any(ch not in safe_run_chars for ch in run_id):
        fail(45, "invalid_run_id")
    base = safe(str(roots[0]), must_exist=True)
    target = base / ".107pilot" / "runs" / run_id
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    resolved = safe(str(target), must_exist=True)
    emit({"path": str(resolved)})
elif op == "stat":
    current = path.stat()
    mode = current.st_mode
    if stat.S_ISREG(mode):
        kind = "regular file"
    elif stat.S_ISDIR(mode):
        kind = "directory"
    else:
        fail(46, "special_file_denied")
    emit({
        "path": str(path),
        "kind": kind,
        "size_bytes": current.st_size,
        "mtime_epoch": current.st_mtime,
        "owner_readable": bool(mode & stat.S_IRUSR),
    })
elif op == "tail":
    limit = int(sys.argv[4])
    if limit <= 0:
        fail(45, "invalid_limit")
    current = path.stat()
    if not stat.S_ISREG(current.st_mode):
        fail(46, "not_regular_file")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
        stream.seek(max(0, current.st_size - limit))
        data = stream.read(limit)
    emit({
        "path": str(path),
        "max_bytes": limit,
        "tail_b64": base64.b64encode(data).decode("ascii"),
        "bytes_read": len(data),
        "truncated": current.st_size > limit,
        "sha256": digest.hexdigest(),
    })
elif op == "range":
    offset = int(sys.argv[4])
    length = int(sys.argv[5])
    if offset < 0 or length <= 0:
        fail(45, "invalid_range")
    current = path.stat()
    if not stat.S_ISREG(current.st_mode):
        fail(46, "not_regular_file")
    with path.open("rb") as stream:
        stream.seek(offset)
        data = stream.read(length)
    emit({"data_b64": base64.b64encode(data).decode("ascii")})
elif op == "inventory":
    if not path.is_dir():
        fail(46, "not_directory")
    max_depth = int(sys.argv[4])
    max_files = int(sys.argv[5])
    max_single = int(sys.argv[6])
    max_total = int(sys.argv[7])
    excluded = tuple(json.loads(sys.argv[8]))
    if min(max_depth, max_files, max_single, max_total) <= 0:
        fail(45, "invalid_inventory_policy")
    files = []
    skipped = []
    total = 0
    stop = False
    for current_root, directories, names in os.walk(path, followlinks=False):
        current = Path(current_root)
        relative_root = current.relative_to(path)
        depth = 0 if str(relative_root) == "." else len(relative_root.parts)
        directories[:] = [
            name for name in directories
            if not (current / name).is_symlink()
        ]
        if depth >= max_depth:
            directories[:] = []
        for name in sorted(names):
            candidate = current / name
            if candidate.is_symlink():
                skipped.append(f"symlink:{candidate.relative_to(path).as_posix()}")
                continue
            relative = candidate.relative_to(path).as_posix()
            if any(
                fnmatch.fnmatch(name, pattern)
                or fnmatch.fnmatch(relative, pattern)
                for pattern in excluded
            ):
                continue
            current_stat = candidate.stat()
            if not stat.S_ISREG(current_stat.st_mode):
                skipped.append(f"special:{relative}")
                continue
            if len(files) >= max_files:
                skipped.append("max_files_reached")
                stop = True
                break
            if total + current_stat.st_size > max_total:
                skipped.append("max_total_inventory_bytes_reached")
                stop = True
                break
            total += current_stat.st_size
            sha256 = None
            if current_stat.st_size <= max_single:
                digest = hashlib.sha256()
                with candidate.open("rb") as stream:
                    while chunk := stream.read(1024 * 1024):
                        digest.update(chunk)
                sha256 = digest.hexdigest()
            files.append({
                "path": str(candidate.resolve(strict=True)),
                "relative_path": relative,
                "size_bytes": current_stat.st_size,
                "mtime_epoch": current_stat.st_mtime,
                "sha256": sha256,
            })
        if stop:
            break
    emit({"root": str(path), "files": files, "skipped": skipped, "total_size_bytes": total})
else:
    fail(45, "unsupported_operation")
""".strip()


class SshEvidenceTransport:
    def __init__(
        self,
        *,
        client: SshRelayClient,
        max_single_read_bytes: int = 10 * 1024 * 1024,
    ) -> None:
        if max_single_read_bytes <= 0:
            raise ValueError("max_single_read_bytes must be positive")
        self.client = client
        self.config = client.config
        self.allowed_roots = self.config.expanded_owner_roots()
        self.max_single_read_bytes = max_single_read_bytes

    def probe(self, identity: UserIdentity) -> EvidenceCapability:
        self._require_identity(identity)
        check = self.client.check()
        active = check.state == SshSessionState.ACTIVE
        return EvidenceCapability(
            transport="ssh_relay",
            can_stat=active,
            can_tail=active,
            can_inventory=active,
            can_copy_selected=False,
            authorized_roots=self.allowed_roots,
            max_single_read_bytes=self.max_single_read_bytes,
            notes=(check.status_code,),
        )

    def prepare_run_root(
        self,
        identity: UserIdentity,
        run_id: str,
        policy: EvidencePolicy,
    ) -> EvidenceRoot:
        self._require_identity(identity)
        payload = self._invoke("prepare", self.allowed_roots[0], run_id)
        resolved = _absolute_remote_path(_required_str(payload, "path"))
        return EvidenceRoot(
            run_id=run_id,
            path=SafePath(
                original=resolved,
                resolved=Path(resolved),
                root=Path(self.allowed_roots[0]),
            ),
        )

    def stat(self, identity: UserIdentity, path: SafePath) -> FileStat:
        self._require_identity(identity)
        payload = self._invoke("stat", self._authorized_path(path))
        return FileStat(
            path=_required_str(payload, "path"),
            kind=_required_str(payload, "kind"),
            size_bytes=int(payload["size_bytes"]),
            mtime_epoch=float(payload["mtime_epoch"]),
            owner_readable=bool(payload["owner_readable"]),
        )

    def read_text_tail(
        self,
        identity: UserIdentity,
        path: SafePath,
        max_bytes: int,
    ) -> TextTail:
        self._require_identity(identity)
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        bounded = min(max_bytes, self.max_single_read_bytes)
        payload = self._invoke("tail", self._authorized_path(path), str(bounded))
        data = _decode_b64(payload, "tail_b64")
        return TextTail(
            path=_required_str(payload, "path"),
            max_bytes=max_bytes,
            tail=data.decode("utf-8", errors="replace"),
            bytes_read=int(payload["bytes_read"]),
            truncated=bool(payload["truncated"]),
            sha256=_required_str(payload, "sha256"),
        )

    def read_bytes_range(
        self,
        identity: UserIdentity,
        path: SafePath,
        offset: int,
        length: int,
    ) -> bytes:
        self._require_identity(identity)
        if offset < 0 or length <= 0 or length > self.max_single_read_bytes:
            raise ValueError("invalid or oversized SSH evidence byte range")
        payload = self._invoke(
            "range",
            self._authorized_path(path),
            str(offset),
            str(length),
        )
        return _decode_b64(payload, "data_b64")

    def inventory(
        self,
        identity: UserIdentity,
        root: SafePath,
        policy: EvidencePolicy,
    ) -> OutputInventory:
        self._require_identity(identity)
        payload = self._invoke(
            "inventory",
            self._authorized_path(root),
            str(policy.max_depth),
            str(policy.max_files),
            str(min(policy.max_single_read_bytes, self.max_single_read_bytes)),
            str(policy.max_total_inventory_bytes),
            json.dumps(policy.excluded_patterns, separators=(",", ":")),
        )
        raw_files = payload.get("files")
        if not isinstance(raw_files, list):
            raise RuntimeError("SSH evidence inventory response is invalid")
        files = [
            InventoryFile(
                path=_required_str(item, "path"),
                relative_path=_required_str(item, "relative_path"),
                size_bytes=int(item["size_bytes"]),
                mtime_epoch=float(item["mtime_epoch"]),
                sha256=None if item.get("sha256") is None else str(item["sha256"]),
            )
            for item in raw_files
            if isinstance(item, dict)
        ]
        return OutputInventory(
            root=_required_str(payload, "root"),
            files=files,
            skipped=[str(item) for item in payload.get("skipped", [])],
            total_size_bytes=int(payload.get("total_size_bytes", 0)),
        )

    def _invoke(self, operation: str, path: str, *args: str) -> dict[str, Any]:
        result = self.client.execute_fixed_program(
            FixedRemoteProgram.EVIDENCE_FS,
            (
                operation,
                json.dumps(self.allowed_roots, separators=(",", ":")),
                path,
                *args,
            ),
            portal_owner=self.config.portal_owner,
            timeout_seconds=self.config.timeout_seconds,
        )
        if result.returncode == 44:
            raise FileNotFoundError(path)
        if result.returncode in {45, 46}:
            raise PathPolicyError("SSH evidence path or file type denied")
        if result.returncode != 0:
            raise RuntimeError(f"SSH evidence operation failed: {operation}")
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError("SSH evidence operation returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("SSH evidence operation returned a non-object")
        return payload

    def _require_identity(self, identity: UserIdentity) -> None:
        if identity.username != self.config.portal_owner:
            raise SshRelayPolicyError("SSH evidence owner mismatch")

    def _authorized_path(self, path: SafePath) -> str:
        candidate = _absolute_remote_path(str(path.resolved))
        if any(
            candidate == root or candidate.startswith(f"{root.rstrip('/')}/")
            for root in self.allowed_roots
        ):
            return candidate
        raise PathPolicyError("SSH evidence path outside owner roots")


def _absolute_remote_path(path: str) -> str:
    candidate = PurePosixPath(path)
    if not candidate.is_absolute() or ".." in candidate.parts or str(candidate) == "/":
        raise PathPolicyError("invalid remote evidence path")
    return str(candidate)


def _required_str(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"SSH evidence response missing {key}")
    return value


def _decode_b64(payload: dict[str, Any], key: str) -> bytes:
    value = _required_str(payload, key)
    try:
        return base64.b64decode(value, validate=True)
    except ValueError as exc:
        raise RuntimeError(f"SSH evidence response has invalid {key}") from exc
