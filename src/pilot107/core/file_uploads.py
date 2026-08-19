"""Resumable, offset-based upload sessions for transferring large user files.

The control plane implements the `tus <https://tus.io>`_ resumable-upload
model on top of owner-isolated staging: the browser appends raw binary ranges
(``append_bytes``) to a per-``upload_id`` partial file instead of posting
base64 JSON chunks.  Appends are truncate-then-write at the client-declared
offset, so a retried ``PATCH`` is idempotent and an interrupted transfer can be
resumed from the persisted ``received_bytes`` offset.

Parallel uploads use the tus *concatenation* extension: the client opens
several ``is_partial`` byte buckets, fills them concurrently, then asks the
service to ``concatenate`` them into one whole session.  Quota bytes are
counted when partials are admitted so the merged session does not double count.

Once ``received_bytes`` reaches ``total_size`` the whole-file sha256 is
verified *before* a single byte reaches the cluster (front-loaded integrity,
mirroring the production ``sha256sum -c`` step), and only then is the file
written through the owner's :class:`FileOpsExecutor`.  Archive extraction is
opt-in and delegated to the executor, which rejects path-traversal members.
"""

from __future__ import annotations

import base64
import hashlib
import posixpath
import secrets
import shutil
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pilot107.adapters.slurm import FileOpsExecutor
from pilot107.core.identity import is_safe_username
from pilot107.core.path_policy import resolve_owner_roots

DEFAULT_SESSION_TTL_SECONDS = 3600
DEFAULT_MAX_ACTIVE_PER_OWNER = 8
DEFAULT_MAX_TOTAL_BYTES_PER_OWNER = 16 * 1024 * 1024 * 1024  # 16 GiB
DEFAULT_WRITE_BLOCK_SIZE = 8 * 1024 * 1024  # staging -> cluster write block
_PARTIAL_NAME = "partial"


class OwnerPathAuthorizationError(ValueError):
    """An absolute cluster path is outside the configured owner roots."""


def authorize_owner_path(
    owner_roots: tuple[str, ...] | list[str],
    *,
    owner: str,
    target_path: str,
) -> str:
    """Return a normalized owner-contained POSIX path without filesystem access."""

    if not isinstance(target_path, str) or not target_path.startswith("/"):
        raise OwnerPathAuthorizationError("path must be absolute")
    if "\x00" in target_path or any(part == ".." for part in target_path.split("/")):
        raise OwnerPathAuthorizationError("path traversal is not allowed")
    normalized = posixpath.normpath(target_path)
    roots = resolve_owner_roots(owner_roots, user=owner)
    for root in roots:
        root_normalized = posixpath.normpath(root)
        if normalized == root_normalized or normalized.startswith(f"{root_normalized}/"):
            return normalized
    raise OwnerPathAuthorizationError("path is outside the configured owner roots")


class UploadState(StrEnum):
    INITIALIZED = "initialized"
    UPLOADING = "uploading"
    ASSEMBLED = "assembled"
    VERIFIED = "verified"
    WRITTEN = "written"
    EXTRACTED = "extracted"
    FAILED = "failed"
    ABORTED = "aborted"


_TERMINAL_STATES = frozenset(
    {UploadState.WRITTEN, UploadState.EXTRACTED, UploadState.FAILED, UploadState.ABORTED}
)


class UploadError(Exception):
    """A user-facing upload-session failure carrying an HTTP status hint."""

    def __init__(self, message: str, *, status: int = 400, code: str = "UPLOAD.INVALID") -> None:
        super().__init__(message)
        self.status = status
        self.code = code


class UploadNotFound(UploadError):
    def __init__(self, upload_id: str) -> None:
        super().__init__(
            f"upload session not found: {upload_id}", status=404, code="UPLOAD.NOT_FOUND"
        )


@dataclass
class UploadSession:
    upload_id: str
    owner: str
    target_path: str
    filename: str
    total_size: int
    sha256_expected: str | None
    auto_extract: bool
    state: UploadState
    created_at: str
    is_partial: bool = False
    received_bytes: int = 0
    sha256_actual: str | None = None
    written_path: str | None = None
    extracted_members: int | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "upload_id": self.upload_id,
            "owner": self.owner,
            "target_path": self.target_path,
            "filename": self.filename,
            "total_size": self.total_size,
            "is_partial": self.is_partial,
            "received_bytes": self.received_bytes,
            "sha256_expected": self.sha256_expected,
            "sha256_actual": self.sha256_actual,
            "auto_extract": self.auto_extract,
            "state": str(self.state),
            "created_at": self.created_at,
            "written_path": self.written_path,
            "extracted_members": self.extracted_members,
            "error": self.error,
        }


def _safe_filename(filename: str) -> str:
    cleaned = filename.strip()
    if (
        not cleaned
        or cleaned in {".", ".."}
        or "/" in cleaned
        or "\\" in cleaned
        or "\x00" in cleaned
    ):
        raise UploadError(f"unsafe upload filename: {filename!r}", code="UPLOAD.UNSAFE_NAME")
    return cleaned


def _normalize_sha256(value: str | None) -> str | None:
    if value is None:
        return None
    digest = value.strip().lower()
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise UploadError("sha256_expected must be a 64-char hex digest")
    return digest


_UPLOAD_SESSIONS_DDL = """
CREATE TABLE IF NOT EXISTS upload_sessions (
    upload_id TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    target_path TEXT NOT NULL,
    filename TEXT NOT NULL,
    total_size INTEGER NOT NULL,
    sha256_expected TEXT,
    auto_extract INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL,
    created_at TEXT NOT NULL,
    received_bytes INTEGER NOT NULL DEFAULT 0,
    is_partial INTEGER NOT NULL DEFAULT 0,
    sha256_actual TEXT,
    written_path TEXT,
    extracted_members INTEGER,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_upload_sessions_owner
    ON upload_sessions(owner, created_at DESC);
"""


def _session_from_row(row: sqlite3.Row | dict[str, Any]) -> UploadSession:
    """Reconstruct an UploadSession from a database row."""
    return UploadSession(
        upload_id=row["upload_id"],
        owner=row["owner"],
        target_path=row["target_path"],
        filename=row["filename"],
        total_size=row["total_size"],
        sha256_expected=row["sha256_expected"],
        auto_extract=bool(row["auto_extract"]),
        state=UploadState(row["state"]),
        created_at=row["created_at"],
        is_partial=bool(row["is_partial"]),
        received_bytes=int(row["received_bytes"]),
        sha256_actual=row["sha256_actual"],
        written_path=row["written_path"],
        extracted_members=row["extracted_members"],
        error=row["error"],
    )


class UploadSessionStore:
    """SQLite-backed persistence for upload session metadata."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._drop_legacy_chunk_schema()
        with self.connect() as conn:
            conn.executescript(_UPLOAD_SESSIONS_DDL)

    def _drop_legacy_chunk_schema(self) -> None:
        """Drop the pre-tus chunk-indexed table so it is recreated offset-based.

        Upload sessions are transient staging metadata; dropping the legacy
        layout on upgrade simply discards any in-flight sessions from an older
        build rather than carrying over an incompatible row shape.
        """
        with self.connect() as conn:
            columns = {r[1] for r in conn.execute("PRAGMA table_info(upload_sessions)")}
            if "received_chunks_json" in columns:
                conn.execute("DROP TABLE upload_sessions")

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def insert(self, session: UploadSession) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO upload_sessions "
                "(upload_id, owner, target_path, filename, total_size, "
                " sha256_expected, auto_extract, state, created_at, "
                " received_bytes, is_partial, sha256_actual, written_path, "
                " extracted_members, error) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    session.upload_id, session.owner, session.target_path,
                    session.filename, session.total_size,
                    session.sha256_expected, int(session.auto_extract),
                    str(session.state), session.created_at,
                    session.received_bytes, int(session.is_partial),
                    session.sha256_actual, session.written_path,
                    session.extracted_members, session.error,
                ),
            )

    def update(self, session: UploadSession) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE upload_sessions SET state=?, received_bytes=?, "
                "sha256_actual=?, written_path=?, extracted_members=?, error=? "
                "WHERE upload_id=?",
                (
                    str(session.state), session.received_bytes,
                    session.sha256_actual, session.written_path,
                    session.extracted_members, session.error,
                    session.upload_id,
                ),
            )

    def get(self, upload_id: str) -> UploadSession | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM upload_sessions WHERE upload_id=?", (upload_id,)
            ).fetchone()
        return _session_from_row(row) if row else None

    def list_by_owner(self, owner: str) -> list[UploadSession]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM upload_sessions WHERE owner=? ORDER BY created_at DESC",
                (owner,),
            ).fetchall()
        return [_session_from_row(r) for r in rows]

    def delete(self, upload_id: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM upload_sessions WHERE upload_id=?", (upload_id,))

    def delete_terminal_before(self, cutoff_iso: str) -> int:
        terminal = ",".join(f"'{s}'" for s in _TERMINAL_STATES)
        with self.connect() as conn:
            cur = conn.execute(
                f"DELETE FROM upload_sessions WHERE state IN ({terminal}) AND created_at < ?",
                (cutoff_iso,),
            )
            return cur.rowcount


class FileUploadService:
    """Owner-isolated, offset-based upload sessions staged on control-plane disk."""

    def __init__(
        self,
        *,
        executor: FileOpsExecutor,
        owner_roots: tuple[str, ...] | list[str],
        staging_root: Path,
        write_block_size: int = DEFAULT_WRITE_BLOCK_SIZE,
        session_ttl_seconds: int = DEFAULT_SESSION_TTL_SECONDS,
        store: UploadSessionStore | None = None,
        max_active_per_owner: int = DEFAULT_MAX_ACTIVE_PER_OWNER,
        max_total_bytes_per_owner: int = DEFAULT_MAX_TOTAL_BYTES_PER_OWNER,
    ) -> None:
        if not owner_roots:
            raise ValueError("file upload service requires explicit owner roots")
        if write_block_size <= 0:
            raise ValueError("write block size must be positive")
        if session_ttl_seconds <= 0:
            raise ValueError("session TTL must be positive")
        if max_active_per_owner <= 0:
            raise ValueError("max_active_per_owner must be positive")
        if max_total_bytes_per_owner <= 0:
            raise ValueError("max_total_bytes_per_owner must be positive")
        self.executor = executor
        self.owner_roots = tuple(owner_roots)
        self.staging_root = Path(staging_root)
        self.write_block_size = write_block_size
        self.session_ttl_seconds = session_ttl_seconds
        self.store = store
        self.max_active_per_owner = max_active_per_owner
        self.max_total_bytes_per_owner = max_total_bytes_per_owner
        self._sessions: dict[str, UploadSession] = {}
        self._lock = threading.Lock()
        if store is not None:
            self._load_sessions_from_store()

    def _load_sessions_from_store(self) -> None:
        """Reload non-terminal sessions from the persistent store on startup."""
        assert self.store is not None
        with self.store.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM upload_sessions WHERE state NOT IN (?,?,?,?)",
                tuple(str(s) for s in _TERMINAL_STATES),
            ).fetchall()
        with self._lock:
            for row in rows:
                session = _session_from_row(row)
                self._sessions[session.upload_id] = session

    def _persist_insert(self, session: UploadSession) -> None:
        if self.store is not None:
            self.store.insert(session)

    def _persist_update(self, session: UploadSession) -> None:
        if self.store is not None:
            self.store.update(session)

    # -- path policy -------------------------------------------------------

    def _authorize_target(self, owner: str, target_path: str) -> str:
        if not target_path.startswith("/"):
            raise UploadError("target_path must be absolute", code="UPLOAD.UNSAFE_PATH")
        if "\x00" in target_path:
            raise UploadError("target_path contains a NUL byte", code="UPLOAD.UNSAFE_PATH")
        if any(part == ".." for part in target_path.split("/")):
            raise UploadError("target_path contains traversal", code="UPLOAD.UNSAFE_PATH")
        try:
            return authorize_owner_path(
                self.owner_roots,
                owner=owner,
                target_path=target_path,
            )
        except OwnerPathAuthorizationError:
            pass
        raise UploadError(
            f"target_path outside allowed roots: {target_path}",
            status=403,
            code="UPLOAD.PATH_FORBIDDEN",
        )

    # -- quota enforcement -------------------------------------------------

    def _enforce_owner_quota(
        self,
        owner: str,
        incoming_size: int,
        *,
        is_partial: bool = False,
        exclude_ids: frozenset[str] = frozenset(),
    ) -> None:
        """Reject new sessions that would exceed per-owner concurrency or byte caps.

        Partial (parallel-upload) byte buckets count against the byte cap but
        not the concurrency cap, so a single parallel upload's N parts do not
        exhaust the owner's whole-session slots.  ``exclude_ids`` lets the
        concatenation step release the parts it merges before re-admitting the
        same bytes as one whole session.
        """
        with self._lock:
            active = [
                s
                for s in self._sessions.values()
                if s.owner == owner
                and s.state not in _TERMINAL_STATES
                and s.upload_id not in exclude_ids
            ]
        if not is_partial:
            whole = [s for s in active if not s.is_partial]
            if len(whole) >= self.max_active_per_owner:
                raise UploadError(
                    f"too many active upload sessions ({len(whole)}/{self.max_active_per_owner})",
                    status=429,
                    code="UPLOAD.QUOTA_CONCURRENT",
                )
        pending_bytes = sum(s.total_size for s in active)
        if pending_bytes + incoming_size > self.max_total_bytes_per_owner:
            raise UploadError(
                f"upload quota exceeded: {pending_bytes + incoming_size} bytes "
                f"would exceed {self.max_total_bytes_per_owner} byte limit",
                status=429,
                code="UPLOAD.QUOTA_BYTES",
            )

    # -- session lifecycle -------------------------------------------------

    def create_session(
        self,
        *,
        owner: str,
        target_path: str,
        filename: str,
        total_size: int,
        sha256_expected: str | None = None,
        auto_extract: bool = False,
    ) -> UploadSession:
        if not is_safe_username(owner):
            raise UploadError(f"unsafe owner: {owner!r}", status=403, code="UPLOAD.OWNER")
        if not isinstance(total_size, int) or total_size <= 0:
            raise UploadError("total_size must be a positive integer")
        self._enforce_owner_quota(owner, total_size)
        sha256_expected = _normalize_sha256(sha256_expected)
        authorized_target = self._authorize_target(owner, target_path)
        safe_name = _safe_filename(filename)
        return self._new_session(
            owner=owner,
            target_path=authorized_target,
            filename=safe_name,
            total_size=total_size,
            sha256_expected=sha256_expected,
            auto_extract=auto_extract,
            is_partial=False,
        )

    def create_partial_session(self, *, owner: str, total_size: int) -> UploadSession:
        """Create a tus concatenation *partial* byte bucket (no target yet)."""
        if not is_safe_username(owner):
            raise UploadError(f"unsafe owner: {owner!r}", status=403, code="UPLOAD.OWNER")
        if not isinstance(total_size, int) or total_size <= 0:
            raise UploadError("total_size must be a positive integer")
        self._enforce_owner_quota(owner, total_size, is_partial=True)
        return self._new_session(
            owner=owner,
            target_path="",
            filename="",
            total_size=total_size,
            sha256_expected=None,
            auto_extract=False,
            is_partial=True,
        )

    def _new_session(
        self,
        *,
        owner: str,
        target_path: str,
        filename: str,
        total_size: int,
        sha256_expected: str | None,
        auto_extract: bool,
        is_partial: bool,
    ) -> UploadSession:
        upload_id = secrets.token_hex(16)
        session = UploadSession(
            upload_id=upload_id,
            owner=owner,
            target_path=target_path,
            filename=filename,
            total_size=total_size,
            sha256_expected=sha256_expected,
            auto_extract=auto_extract,
            state=UploadState.INITIALIZED,
            created_at=datetime.now(UTC).isoformat(),
            is_partial=is_partial,
        )
        self._session_dir(session).mkdir(parents=True, exist_ok=True)
        self._partial_path(session).touch()
        with self._lock:
            self._sessions[upload_id] = session
        self._persist_insert(session)
        return session

    def get_session(self, upload_id: str, owner: str) -> UploadSession:
        return self._require_session(upload_id, owner)

    def list_sessions(self, owner: str) -> list[UploadSession]:
        with self._lock:
            return [
                session
                for session in self._sessions.values()
                if session.owner == owner
            ]

    def abort(self, upload_id: str, owner: str) -> UploadSession:
        session = self._require_session(upload_id, owner)
        with self._lock:
            if session.state not in _TERMINAL_STATES:
                session.state = UploadState.ABORTED
        self._persist_update(session)
        self._purge_staging(session)
        return session

    # -- byte intake -------------------------------------------------------

    def append_bytes(self, upload_id: str, owner: str, offset: int, data: bytes) -> int:
        """Append ``data`` at ``offset``; returns the new received byte count.

        The offset must not run ahead of what has already been received (a gap
        means the client skipped bytes, rejected with 409).  Rewriting an
        already-received range truncates back to ``offset`` first, which keeps
        retried ``PATCH`` requests idempotent instead of corrupting the file.
        """
        session = self._require_session(upload_id, owner)
        if session.state in _TERMINAL_STATES:
            raise UploadError(
                f"upload session is {session.state}", code="UPLOAD.STATE"
            )
        if not isinstance(offset, int) or offset < 0:
            raise UploadError("offset must be a non-negative integer")
        if offset > session.received_bytes:
            raise UploadError(
                f"offset {offset} is ahead of received bytes {session.received_bytes}",
                status=409,
                code="UPLOAD.OFFSET_MISMATCH",
            )
        path = self._partial_path(session)
        with open(path, "r+b") as handle:
            handle.truncate(offset)
            handle.seek(offset)
            handle.write(data)
        with self._lock:
            session.received_bytes = offset + len(data)
            if session.state == UploadState.INITIALIZED:
                session.state = UploadState.UPLOADING
        self._persist_update(session)
        return session.received_bytes

    # -- concatenation -----------------------------------------------------

    def concatenate(
        self,
        *,
        owner: str,
        partial_ids: list[str],
        target_path: str,
        filename: str,
        total_size: int | None = None,
        sha256_expected: str | None = None,
        auto_extract: bool = False,
    ) -> UploadSession:
        """Merge completed partial uploads into one whole session (tus concat).

        ``total_size`` may be omitted, in which case it defaults to the sum of
        the parts; when provided it must match that sum exactly.
        """
        if not is_safe_username(owner):
            raise UploadError(f"unsafe owner: {owner!r}", status=403, code="UPLOAD.OWNER")
        if not partial_ids:
            raise UploadError("concatenation requires at least one partial upload")
        partials = [self._require_session(pid, owner) for pid in partial_ids]
        for partial in partials:
            if not partial.is_partial:
                raise UploadError(
                    f"upload {partial.upload_id} is not a partial upload",
                    code="UPLOAD.CONCAT_INVALID",
                )
            if partial.state in _TERMINAL_STATES:
                raise UploadError(
                    f"partial upload {partial.upload_id} is {partial.state}",
                    code="UPLOAD.CONCAT_INVALID",
                )
            if partial.received_bytes != partial.total_size:
                raise UploadError(
                    f"partial upload {partial.upload_id} is incomplete "
                    f"({partial.received_bytes}/{partial.total_size})",
                    status=409,
                    code="UPLOAD.CONCAT_INCOMPLETE",
                )
        declared = sum(p.total_size for p in partials)
        if total_size is None:
            total_size = declared
        if not isinstance(total_size, int) or total_size <= 0:
            raise UploadError("total_size must be a positive integer")
        if declared != total_size:
            raise UploadError(
                f"concat length {total_size} does not match sum of parts {declared}",
                code="UPLOAD.CONCAT_INVALID",
            )
        sha256_expected = _normalize_sha256(sha256_expected)
        authorized_target = self._authorize_target(owner, target_path)
        safe_name = _safe_filename(filename)
        # The parts' bytes were quota-counted when admitted; exclude them so
        # re-admitting the merged whole does not double count.
        self._enforce_owner_quota(owner, total_size, exclude_ids=frozenset(partial_ids))

        session = UploadSession(
            upload_id=secrets.token_hex(16),
            owner=owner,
            target_path=authorized_target,
            filename=safe_name,
            total_size=total_size,
            sha256_expected=sha256_expected,
            auto_extract=auto_extract,
            state=UploadState.INITIALIZED,
            created_at=datetime.now(UTC).isoformat(),
            is_partial=False,
        )
        self._session_dir(session).mkdir(parents=True, exist_ok=True)
        with open(self._partial_path(session), "wb") as out:
            for partial in partials:
                with open(self._partial_path(partial), "rb") as src:
                    shutil.copyfileobj(src, out)
        session.received_bytes = total_size
        session.state = UploadState.UPLOADING
        with self._lock:
            self._sessions[session.upload_id] = session
            for partial in partials:
                self._sessions.pop(partial.upload_id, None)
        self._persist_insert(session)
        for partial in partials:
            if self.store is not None:
                self.store.delete(partial.upload_id)
            self._purge_staging(partial)
        return session

    # -- completion --------------------------------------------------------

    def complete(self, upload_id: str, owner: str) -> UploadSession:
        session = self._require_session(upload_id, owner)
        if session.state in {UploadState.WRITTEN, UploadState.EXTRACTED}:
            return session
        if session.state in {UploadState.FAILED, UploadState.ABORTED}:
            raise UploadError(
                f"upload session is {session.state}", code="UPLOAD.STATE"
            )
        if session.is_partial:
            raise UploadError(
                "partial uploads are merged via concatenation, not completed",
                code="UPLOAD.STATE",
            )
        if session.received_bytes != session.total_size:
            raise UploadError(
                f"upload incomplete: {session.received_bytes}/{session.total_size} bytes",
                code="UPLOAD.INCOMPLETE",
            )
        try:
            assembled_path = self._partial_path(session)
            session.state = UploadState.ASSEMBLED
            digest = self._sha256_file(assembled_path)
            session.sha256_actual = digest
            if session.sha256_expected is not None and digest != session.sha256_expected:
                session.state = UploadState.FAILED
                session.error = (
                    f"sha256 mismatch: expected {session.sha256_expected}, got {digest}"
                )
                self._persist_update(session)
                self._purge_staging(session)
                raise UploadError(
                    session.error, status=409, code="UPLOAD.SHA256_MISMATCH"
                )
            session.state = UploadState.VERIFIED
            written_path = self._write_to_cluster(session, assembled_path)
            session.written_path = written_path
            session.state = UploadState.WRITTEN
            if session.auto_extract:
                members = self.executor.extract_archive(
                    archive_path=written_path,
                    dest_dir=session.target_path,
                    owner=session.owner,
                )
                session.extracted_members = members
                session.state = UploadState.EXTRACTED
        except UploadError:
            raise
        except Exception as exc:  # transport / executor failure
            session.state = UploadState.FAILED
            session.error = str(exc)
            self._persist_update(session)
            self._purge_staging(session)
            raise UploadError(
                f"upload completion failed: {exc}",
                status=502,
                code="UPLOAD.WRITE_FAILED",
            ) from exc
        self._persist_update(session)
        self._purge_staging(session)
        return session

    def _write_to_cluster(self, session: UploadSession, assembled_path: Path) -> str:
        dest = f"{session.target_path}/{session.filename}"
        offset = 0
        with open(assembled_path, "rb") as handle:
            first = True
            while True:
                block = handle.read(self.write_block_size)
                if not block:
                    break
                write_offset = 0 if first else offset
                self.executor.write_bytes_chunk(
                    path=dest,
                    data_b64=base64.b64encode(block).decode("ascii"),
                    offset=write_offset,
                    owner=session.owner,
                )
                offset += len(block)
                first = False
        return dest

    # -- staging helpers ---------------------------------------------------

    def _require_session(self, upload_id: str, owner: str) -> UploadSession:
        with self._lock:
            session = self._sessions.get(upload_id)
        if session is None or session.owner != owner:
            raise UploadNotFound(upload_id)
        return session

    def _session_dir(self, session: UploadSession) -> Path:
        return self.staging_root / session.owner / session.upload_id

    def _partial_path(self, session: UploadSession) -> Path:
        return self._session_dir(session) / _PARTIAL_NAME

    @staticmethod
    def _sha256_file(path: Path) -> str:
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    def _purge_staging(self, session: UploadSession) -> None:
        shutil.rmtree(self._session_dir(session), ignore_errors=True)

    # -- maintenance -------------------------------------------------------

    def cleanup_expired(self, *, now: datetime | None = None) -> int:
        moment = now or datetime.now(UTC)
        removed = 0
        with self._lock:
            expired_ids = []
            for upload_id, session in self._sessions.items():
                created = datetime.fromisoformat(session.created_at)
                age = (moment - created).total_seconds()
                if session.state in _TERMINAL_STATES or age > self.session_ttl_seconds:
                    expired_ids.append(upload_id)
            for upload_id in expired_ids:
                session = self._sessions.pop(upload_id)
                removed += 1
                self._purge_staging(session)
                if self.store is not None:
                    self.store.delete(upload_id)
        return removed
