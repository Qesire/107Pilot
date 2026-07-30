"""Integrity-bound local control-plane backup and empty-root restore."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Protocol
from uuid import uuid4

from pilot107.core.redaction import redact_sensitive_text

BACKUP_SCHEMA = "pilot107.control_plane_backup.v1"


class RecoveryError(RuntimeError):
    """Raised when a backup or restore fails closed."""


class PostgresBackupAdapter(Protocol):
    def dump(self, *, dsn: str, destination: Path) -> None: ...

    def restore(self, *, dsn: str, source: Path) -> None: ...


@dataclass(frozen=True)
class BackupResult:
    backup_root: Path
    backup_id: str
    manifest_sha256: str
    file_count: int
    total_size_bytes: int


@dataclass(frozen=True)
class RestoreResult:
    restore_root: Path
    backup_id: str
    file_count: int
    postgres_restored: bool


class PgToolsBackupAdapter:
    """Explicit pg_dump/pg_restore adapter; DSNs are never persisted."""

    def __init__(
        self,
        *,
        pg_dump: str = "pg_dump",
        pg_restore: str = "pg_restore",
        timeout_seconds: int = 600,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.pg_dump = pg_dump
        self.pg_restore = pg_restore
        self.timeout_seconds = timeout_seconds

    def dump(self, *, dsn: str, destination: Path) -> None:
        self._run(
            [
                self.pg_dump,
                "--format=custom",
                "--no-owner",
                "--no-privileges",
                f"--file={destination}",
            ],
            operation="pg_dump",
            dsn=dsn,
        )

    def restore(self, *, dsn: str, source: Path) -> None:
        self._run(
            [
                self.pg_restore,
                "--clean",
                "--if-exists",
                "--no-owner",
                "--no-privileges",
                str(source),
            ],
            operation="pg_restore",
            dsn=dsn,
        )

    def _run(self, argv: Sequence[str], *, operation: str, dsn: str) -> None:
        environment = os.environ.copy()
        environment["PGDATABASE"] = dsn
        try:
            result = subprocess.run(
                list(argv),
                check=False,
                capture_output=True,
                env=environment,
                text=True,
                timeout=self.timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RecoveryError(f"{operation} could not run: {exc}") from exc
        if result.returncode != 0:
            stderr = redact_sensitive_text(result.stderr.strip()[-2000:], secrets=(dsn,))
            raise RecoveryError(f"{operation} failed with exit {result.returncode}: {stderr}")


def create_control_plane_backup(
    *,
    destination: Path,
    sqlite_db: Path,
    evidence_root: Path | None = None,
    capsule_root: Path | None = None,
    upload_staging_root: Path | None = None,
    postgres_dsn: str | None = None,
    postgres_adapter: PostgresBackupAdapter | None = None,
    quiesced: bool,
) -> BackupResult:
    if not quiesced:
        raise RecoveryError("backup requires an explicitly quiesced control plane")
    if destination.is_symlink():
        raise RecoveryError("backup destination must not be a symlink")
    destination = destination.resolve()
    if destination.exists():
        raise RecoveryError(f"backup destination already exists: {destination}")
    _require_regular_source(sqlite_db, "SQLite database")
    for source_root, label in (
        (evidence_root, "Evidence"),
        (capsule_root, "Capsule"),
        (upload_staging_root, "Upload staging"),
    ):
        if source_root is not None and source_root.exists():
            resolved_source = source_root.resolve()
            if _is_within(destination, resolved_source):
                raise RecoveryError(f"backup destination is inside the {label} source tree")
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".pilot107-backup-", dir=destination.parent))
    backup_id = f"backup_{uuid4().hex}"
    try:
        payload = staging / "payload"
        sqlite_destination = payload / "sqlite" / "pilot107.db"
        sqlite_destination.parent.mkdir(parents=True)
        _sqlite_snapshot(sqlite_db, sqlite_destination)
        _validate_sqlite(sqlite_destination)
        _copy_optional_tree(evidence_root, payload / "evidence", "Evidence")
        _copy_optional_tree(capsule_root, payload / "capsules", "Capsule")
        _copy_optional_tree(upload_staging_root, payload / "upload-staging", "Upload staging")
        postgres_included = postgres_dsn is not None
        if postgres_included:
            assert postgres_dsn is not None
            adapter = postgres_adapter or PgToolsBackupAdapter()
            postgres_destination = payload / "postgres" / "control.dump"
            postgres_destination.parent.mkdir(parents=True)
            adapter.dump(dsn=postgres_dsn, destination=postgres_destination)
            _require_regular_source(postgres_destination, "PostgreSQL dump")

        files = _inventory(payload, base=staging)
        manifest = {
            "schema": BACKUP_SCHEMA,
            "backup_id": backup_id,
            "created_at": datetime.now(UTC).isoformat(),
            "components": {
                "sqlite": True,
                "evidence": evidence_root is not None and evidence_root.exists(),
                "capsules": capsule_root is not None and capsule_root.exists(),
                "upload_staging": upload_staging_root is not None and upload_staging_root.exists(),
                "postgres": postgres_included,
            },
            "files": files,
        }
        manifest_path = staging / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        manifest_sha256 = _sha256(manifest_path)
        os.replace(staging, destination)
        return BackupResult(
            backup_root=destination,
            backup_id=backup_id,
            manifest_sha256=manifest_sha256,
            file_count=len(files),
            total_size_bytes=sum(int(item["size_bytes"]) for item in files),
        )
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def verify_control_plane_backup(backup_root: Path) -> dict[str, Any]:
    root = backup_root.resolve()
    manifest_path = root / "manifest.json"
    _require_regular_source(manifest_path, "backup manifest")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RecoveryError(f"backup manifest is unreadable: {exc}") from exc
    if not isinstance(manifest, dict) or manifest.get("schema") != BACKUP_SCHEMA:
        raise RecoveryError("backup manifest schema is invalid")
    backup_id = manifest.get("backup_id")
    files = manifest.get("files")
    if not isinstance(backup_id, str) or not backup_id.startswith("backup_"):
        raise RecoveryError("backup_id is invalid")
    if not isinstance(files, list):
        raise RecoveryError("backup file inventory is invalid")
    expected: set[str] = set()
    for item in files:
        if not isinstance(item, dict):
            raise RecoveryError("backup file entry is invalid")
        relative = _safe_manifest_path(item.get("path"))
        encoded = relative.as_posix()
        if encoded in expected:
            raise RecoveryError(f"duplicate backup file entry: {encoded}")
        expected.add(encoded)
        source = root.joinpath(*relative.parts)
        _require_regular_source(source, f"backup file {encoded}")
        size = item.get("size_bytes")
        digest = item.get("sha256")
        if not isinstance(size, int) or size < 0 or source.stat().st_size != size:
            raise RecoveryError(f"backup file size mismatch: {encoded}")
        if not isinstance(digest, str) or _sha256(source) != digest:
            raise RecoveryError(f"backup file digest mismatch: {encoded}")
    actual = {item["path"] for item in _inventory(root / "payload", base=root)}
    if actual != expected:
        raise RecoveryError("backup payload does not match manifest inventory")
    components = manifest.get("components")
    if not isinstance(components, dict) or components.get("sqlite") is not True:
        raise RecoveryError("backup component metadata is invalid")
    _validate_sqlite(root / "payload" / "sqlite" / "pilot107.db")
    return manifest


def restore_control_plane_backup(
    *,
    backup_root: Path,
    destination: Path,
    postgres_dsn: str | None = None,
    postgres_adapter: PostgresBackupAdapter | None = None,
    postgres_allow_reset: bool = False,
    quiesced: bool,
) -> RestoreResult:
    if not quiesced:
        raise RecoveryError("restore requires an explicitly quiesced control plane")
    manifest = verify_control_plane_backup(backup_root)
    if destination.is_symlink():
        raise RecoveryError("restore destination must not be a symlink")
    destination = destination.resolve()
    backup_resolved = backup_root.resolve()
    if _is_within(destination, backup_resolved):
        raise RecoveryError("restore destination must not be inside the backup tree")
    _require_empty_destination(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".pilot107-restore-", dir=destination.parent))
    try:
        payload = backup_root.resolve() / "payload"
        shutil.copy2(payload / "sqlite" / "pilot107.db", staging / "pilot107.db")
        for name in ("evidence", "capsules", "upload-staging"):
            source = payload / name
            if source.exists():
                _copy_tree(source, staging / name, name)
        _validate_sqlite(staging / "pilot107.db")
        postgres_dump = payload / "postgres" / "control.dump"
        postgres_restored = postgres_dump.exists()
        if postgres_restored:
            if postgres_dsn is None:
                raise RecoveryError(
                    "backup contains PostgreSQL data but no restore DSN was provided"
                )
            if not postgres_allow_reset:
                raise RecoveryError(
                    "PostgreSQL restore requires explicit postgres_allow_reset"
                )
            adapter = postgres_adapter or PgToolsBackupAdapter()
            adapter.restore(dsn=postgres_dsn, source=postgres_dump)
        if destination.exists():
            destination.rmdir()
        os.replace(staging, destination)
        return RestoreResult(
            restore_root=destination,
            backup_id=str(manifest["backup_id"]),
            file_count=len(manifest["files"]),
            postgres_restored=postgres_restored,
        )
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _sqlite_snapshot(source: Path, destination: Path) -> None:
    try:
        with sqlite3.connect(source) as source_conn, sqlite3.connect(destination) as target_conn:
            source_conn.backup(target_conn)
    except sqlite3.Error as exc:
        raise RecoveryError(f"SQLite snapshot failed: {exc}") from exc


def _validate_sqlite(path: Path) -> None:
    _require_regular_source(path, "SQLite snapshot")
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
            quick_check = conn.execute("PRAGMA quick_check").fetchone()
            if quick_check is None or str(quick_check[0]) != "ok":
                raise RecoveryError(f"SQLite quick_check failed: {quick_check}")
            foreign_key_errors = conn.execute("PRAGMA foreign_key_check").fetchall()
            if foreign_key_errors:
                raise RecoveryError(
                    f"SQLite foreign_key_check failed: {len(foreign_key_errors)} rows"
                )
    except sqlite3.Error as exc:
        raise RecoveryError(f"SQLite validation failed: {exc}") from exc


def _copy_optional_tree(source: Path | None, destination: Path, label: str) -> None:
    if source is None:
        return
    _copy_tree(source, destination, label)


def _copy_tree(source: Path, destination: Path, label: str) -> None:
    if source.is_symlink() or not source.is_dir():
        raise RecoveryError(f"{label} root must be a real directory: {source}")
    destination.mkdir(parents=True, exist_ok=True)
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        target = destination / relative
        if path.is_symlink():
            raise RecoveryError(f"{label} tree contains a symlink: {relative}")
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif path.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
        else:
            raise RecoveryError(f"{label} tree contains a special file: {relative}")


def _inventory(root: Path, *, base: Path) -> list[dict[str, Any]]:
    if not root.exists():
        return []
    if root.is_symlink() or not root.is_dir():
        raise RecoveryError(f"backup payload root must be a real directory: {root}")
    inventory: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise RecoveryError(f"backup payload contains a symlink: {path.relative_to(base)}")
        if path.is_file():
            inventory.append(
                {
                    "path": path.relative_to(base).as_posix(),
                    "size_bytes": path.stat().st_size,
                    "sha256": _sha256(path),
                }
            )
        elif not path.is_dir():
            raise RecoveryError(f"backup payload contains a special file: {path}")
    return inventory


def _safe_manifest_path(value: object) -> PurePosixPath:
    if not isinstance(value, str) or not value:
        raise RecoveryError("backup file path is invalid")
    path = PurePosixPath(value)
    if (
        not path.parts
        or path.is_absolute()
        or ".." in path.parts
        or path.parts[0] != "payload"
    ):
        raise RecoveryError(f"unsafe backup file path: {value}")
    return path


def _require_regular_source(path: Path, label: str) -> None:
    if path.is_symlink() or not path.is_file():
        raise RecoveryError(f"{label} is missing or not a regular file: {path}")


def _require_empty_destination(path: Path) -> None:
    if not path.exists():
        return
    if path.is_symlink() or not path.is_dir():
        raise RecoveryError(f"restore destination must be an empty directory: {path}")
    if any(path.iterdir()):
        raise RecoveryError(f"restore destination is not empty: {path}")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True
