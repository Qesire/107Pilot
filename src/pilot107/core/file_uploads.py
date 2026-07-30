"""Chunked upload sessions for transferring large user files to the cluster.

The control plane never asks the browser to send a whole file in one request:
the API and Web BFF cap request bodies (default 2 MiB), so the client slices a
file into ``chunk_size`` pieces (default 1 MiB) and posts them one at a time.
This service stages those pieces on control-plane local disk, reassembles them,
verifies the whole-file sha256 *before* a single byte reaches the cluster
(front-loaded integrity, mirroring the production ``sha256sum -c`` step), and
only then writes the file through the owner's :class:`FileOpsExecutor`.

Sessions are owner-isolated: every chunk and the reassembled blob live under a
per-``upload_id`` staging directory, and the destination path is authorized
against the owner's allowed roots before write.  Archive extraction is opt-in
and delegated to the executor, which rejects path-traversal members.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import secrets
import shutil
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from pilot107.adapters.slurm import FileOpsExecutor
from pilot107.core.identity import is_safe_username
from pilot107.core.path_policy import resolve_owner_roots

DEFAULT_CHUNK_SIZE = 1024 * 1024  # 1 MiB — comfortably under the 2 MiB body cap.
MAX_CHUNK_SIZE = 2 * 1024 * 1024
DEFAULT_SESSION_TTL_SECONDS = 3600
_ASSEMBLED_NAME = "assembled"
_CHUNK_DIR = "chunks"


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
    chunk_size: int
    total_chunks: int
    sha256_expected: str | None
    auto_extract: bool
    state: UploadState
    created_at: str
    received_chunks: dict[int, int] = field(default_factory=dict)
    sha256_actual: str | None = None
    written_path: str | None = None
    extracted_members: int | None = None
    error: str | None = None

    @property
    def received_bytes(self) -> int:
        return sum(self.received_chunks.values())

    def to_dict(self) -> dict[str, object]:
        return {
            "upload_id": self.upload_id,
            "owner": self.owner,
            "target_path": self.target_path,
            "filename": self.filename,
            "total_size": self.total_size,
            "chunk_size": self.chunk_size,
            "total_chunks": self.total_chunks,
            "received_chunks": sorted(self.received_chunks),
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


class FileUploadService:
    """Owner-isolated chunked upload sessions staged on control-plane disk."""

    def __init__(
        self,
        *,
        executor: FileOpsExecutor,
        owner_roots: tuple[str, ...] | list[str],
        staging_root: Path,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        session_ttl_seconds: int = DEFAULT_SESSION_TTL_SECONDS,
    ) -> None:
        if not owner_roots:
            raise ValueError("file upload service requires explicit owner roots")
        if not 1 <= chunk_size <= MAX_CHUNK_SIZE:
            raise ValueError(f"chunk size must be within 1..{MAX_CHUNK_SIZE} bytes")
        if session_ttl_seconds <= 0:
            raise ValueError("session TTL must be positive")
        self.executor = executor
        self.owner_roots = tuple(owner_roots)
        self.staging_root = Path(staging_root)
        self.chunk_size = chunk_size
        self.session_ttl_seconds = session_ttl_seconds
        self._sessions: dict[str, UploadSession] = {}
        self._lock = threading.Lock()

    # -- path policy -------------------------------------------------------

    def _authorize_target(self, owner: str, target_path: str) -> str:
        if not target_path.startswith("/"):
            raise UploadError("target_path must be absolute", code="UPLOAD.UNSAFE_PATH")
        if "\x00" in target_path:
            raise UploadError("target_path contains a NUL byte", code="UPLOAD.UNSAFE_PATH")
        roots = resolve_owner_roots(self.owner_roots, user=owner)
        normalized = target_path.rstrip("/") or "/"
        for root in roots:
            root_normalized = root.rstrip("/") or "/"
            if normalized == root_normalized or normalized.startswith(
                f"{root_normalized}/"
            ):
                return normalized
        raise UploadError(
            f"target_path outside allowed roots: {target_path}",
            status=403,
            code="UPLOAD.PATH_FORBIDDEN",
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
        chunk_size: int | None = None,
        auto_extract: bool = False,
    ) -> UploadSession:
        if not is_safe_username(owner):
            raise UploadError(f"unsafe owner: {owner!r}", status=403, code="UPLOAD.OWNER")
        if not isinstance(total_size, int) or total_size <= 0:
            raise UploadError("total_size must be a positive integer")
        effective_chunk = chunk_size or self.chunk_size
        if not 1 <= effective_chunk <= MAX_CHUNK_SIZE:
            raise UploadError(
                f"chunk_size must be within 1..{MAX_CHUNK_SIZE} bytes"
            )
        if sha256_expected is not None:
            digest = sha256_expected.strip().lower()
            if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                raise UploadError("sha256_expected must be a 64-char hex digest")
            sha256_expected = digest
        authorized_target = self._authorize_target(owner, target_path)
        safe_name = _safe_filename(filename)
        total_chunks = (total_size + effective_chunk - 1) // effective_chunk
        upload_id = secrets.token_hex(16)
        session = UploadSession(
            upload_id=upload_id,
            owner=owner,
            target_path=authorized_target,
            filename=safe_name,
            total_size=total_size,
            chunk_size=effective_chunk,
            total_chunks=total_chunks,
            sha256_expected=sha256_expected,
            auto_extract=auto_extract,
            state=UploadState.INITIALIZED,
            created_at=datetime.now(UTC).isoformat(),
        )
        self._session_dir(session).mkdir(parents=True, exist_ok=True)
        (self._session_dir(session) / _CHUNK_DIR).mkdir(exist_ok=True)
        with self._lock:
            self._sessions[upload_id] = session
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
        self._purge_staging(session)
        return session

    # -- chunk intake ------------------------------------------------------

    def put_chunk(
        self, upload_id: str, owner: str, index: int, data_b64: str
    ) -> UploadSession:
        session = self._require_session(upload_id, owner)
        if session.state in _TERMINAL_STATES:
            raise UploadError(
                f"upload session is {session.state}", code="UPLOAD.STATE"
            )
        if not isinstance(index, int) or index < 0 or index >= session.total_chunks:
            raise UploadError(
                f"chunk index out of range: {index} (total {session.total_chunks})"
            )
        try:
            data = base64.b64decode(data_b64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise UploadError("chunk data is not valid base64") from exc
        expected_len = self._expected_chunk_length(session, index)
        if len(data) != expected_len:
            raise UploadError(
                f"chunk {index} must be {expected_len} bytes, got {len(data)}"
            )
        chunk_path = self._chunk_path(session, index)
        # Idempotent: re-putting an already-received index is a no-op.
        if index in session.received_chunks:
            return session
        chunk_path.write_bytes(data)
        with self._lock:
            session.received_chunks[index] = len(data)
            if session.state == UploadState.INITIALIZED:
                session.state = UploadState.UPLOADING
        return session

    def _expected_chunk_length(self, session: UploadSession, index: int) -> int:
        if index < session.total_chunks - 1:
            return session.chunk_size
        return session.total_size - (session.total_chunks - 1) * session.chunk_size

    # -- completion --------------------------------------------------------

    def complete(self, upload_id: str, owner: str) -> UploadSession:
        session = self._require_session(upload_id, owner)
        if session.state in {UploadState.WRITTEN, UploadState.EXTRACTED}:
            return session
        if session.state in {UploadState.FAILED, UploadState.ABORTED}:
            raise UploadError(
                f"upload session is {session.state}", code="UPLOAD.STATE"
            )
        missing = [
            index
            for index in range(session.total_chunks)
            if index not in session.received_chunks
        ]
        if missing:
            raise UploadError(
                f"missing {len(missing)} chunk(s); first missing index {missing[0]}",
                code="UPLOAD.INCOMPLETE",
            )
        try:
            assembled_path = self._assemble(session)
            session.state = UploadState.ASSEMBLED
            digest = self._sha256_file(assembled_path)
            session.sha256_actual = digest
            if session.sha256_expected is not None and digest != session.sha256_expected:
                session.state = UploadState.FAILED
                session.error = (
                    f"sha256 mismatch: expected {session.sha256_expected}, got {digest}"
                )
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
            self._purge_staging(session)
            raise UploadError(
                f"upload completion failed: {exc}",
                status=502,
                code="UPLOAD.WRITE_FAILED",
            ) from exc
        self._purge_staging(session)
        return session

    def _write_to_cluster(self, session: UploadSession, assembled_path: Path) -> str:
        dest = f"{session.target_path}/{session.filename}"
        offset = 0
        with open(assembled_path, "rb") as handle:
            first = True
            while True:
                block = handle.read(session.chunk_size)
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

    def _chunk_path(self, session: UploadSession, index: int) -> Path:
        return self._session_dir(session) / _CHUNK_DIR / f"{index:010d}"

    def _assemble(self, session: UploadSession) -> Path:
        assembled = self._session_dir(session) / _ASSEMBLED_NAME
        with open(assembled, "wb") as out:
            for index in range(session.total_chunks):
                out.write(self._chunk_path(session, index).read_bytes())
        return assembled

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
        return removed
