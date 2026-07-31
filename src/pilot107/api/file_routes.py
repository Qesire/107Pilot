"""HTTP boundary for the visual filesystem and chunked uploads.

Routes (all under ``/api/v1/files``), owner-scoped via ``X-Pilot107-User``:

GET  /files?path=                      list a directory
GET  /files/content?path=&offset=&length=   read a byte range (base64)
GET  /files/uploads                    list the owner's upload sessions
GET  /files/uploads/{id}               one upload session's progress
POST /files/uploads                    initialize an upload session
POST /files/uploads/{id}/chunks        store one base64 chunk
POST /files/uploads/{id}/complete      verify + write (+ optional extract)
POST /files/uploads/{id}/abort         abandon a session
POST /files/mkdir                      create a directory
POST /files/delete                     remove a file or tree
POST /files/rename                     rename or move a file/directory
POST /files/archive                    pack paths into a tar.gz

Path authorization is enforced both here (upload destinations against the
owner roots) and inside the executor backend (every primitive re-checks the
owner's allowed roots before moving a byte).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict
from typing import Any

from pilot107.adapters.slurm import (
    FileOpsExecutor,
    SlurmSubmissionRejected,
    SlurmTransportError,
)
from pilot107.api.http_types import ApiResponse
from pilot107.api.metrics import ControlPlaneMetrics
from pilot107.core.file_uploads import (
    FileUploadService,
    UploadError,
    UploadNotFound,
)
from pilot107.core.identity import UserIdentity

_DEFAULT_READ_LENGTH = 1024 * 1024
_MAX_READ_LENGTH = 2 * 1024 * 1024


class FileRoutes:
    def __init__(
        self,
        *,
        upload_service: FileUploadService,
        executor: FileOpsExecutor,
        metrics: ControlPlaneMetrics | None = None,
    ) -> None:
        self.upload_service = upload_service
        self.executor = executor
        self._metrics = metrics

    # -- GET ---------------------------------------------------------------

    def handle_get(
        self,
        parts: list[str],
        *,
        params: Mapping[str, list[str]],
        identity: UserIdentity | None,
    ) -> ApiResponse | None:
        if not parts or parts[0] != "files":
            return None
        if identity is None:
            return _error(401, "AUTH.MISSING", "identity required for file operations")
        owner = identity.username

        # GET /files?path=
        if len(parts) == 1:
            path = _first_param(params, "path")
            if not path:
                return _error(400, "FILES.INVALID_QUERY", "path is required")
            try:
                entries = self.executor.list_dir(path=path, owner=owner)
            except SlurmSubmissionRejected as exc:
                return _error(403, "FILES.PATH_FORBIDDEN", str(exc))
            except SlurmTransportError as exc:
                return _error(502, "FILES.LIST_FAILED", str(exc))
            return ApiResponse(
                status=200,
                payload={
                    "path": path,
                    "entries": [asdict(entry) for entry in entries],
                },
            )

        # GET /files/content?path=&offset=&length=
        if len(parts) == 2 and parts[1] == "content":
            path = _first_param(params, "path")
            if not path:
                return _error(400, "FILES.INVALID_QUERY", "path is required")
            offset = _int_param(params, "offset", default=0)
            length = _int_param(params, "length", default=_DEFAULT_READ_LENGTH)
            if offset < 0:
                return _error(400, "FILES.INVALID_QUERY", "offset must be >= 0")
            if not 1 <= length <= _MAX_READ_LENGTH:
                return _error(
                    400, "FILES.INVALID_QUERY", f"length must be within 1..{_MAX_READ_LENGTH}"
                )
            try:
                data_b64, size = self.executor.read_bytes_chunk(
                    path=path, offset=offset, length=length, owner=owner
                )
            except SlurmSubmissionRejected as exc:
                return _error(403, "FILES.PATH_FORBIDDEN", str(exc))
            except SlurmTransportError as exc:
                return _error(502, "FILES.READ_FAILED", str(exc))
            return ApiResponse(
                status=200,
                payload={
                    "path": path,
                    "offset": offset,
                    "size": size,
                    "data_b64": data_b64,
                },
            )

        # GET /files/uploads
        if len(parts) == 2 and parts[1] == "uploads":
            sessions = self.upload_service.list_sessions(owner)
            return ApiResponse(
                status=200,
                payload={"items": [session.to_dict() for session in sessions]},
            )

        # GET /files/uploads/{id}
        if len(parts) == 3 and parts[1] == "uploads":
            try:
                session = self.upload_service.get_session(parts[2], owner)
            except UploadNotFound as exc:
                return _upload_error(exc)
            return ApiResponse(status=200, payload=session.to_dict())

        return None

    # -- POST --------------------------------------------------------------

    def handle_post(
        self,
        parts: list[str],
        *,
        body: bytes,
        identity: UserIdentity | None,
    ) -> ApiResponse | None:
        if not parts or parts[0] != "files":
            return None
        if identity is None:
            return _error(401, "AUTH.MISSING", "identity required for file operations")
        owner = identity.username

        # POST /files/uploads
        if len(parts) == 2 and parts[1] == "uploads":
            payload, error = _json_body(body)
            if error is not None:
                return error
            try:
                session = self.upload_service.create_session(
                    owner=owner,
                    target_path=_required_string(payload, "target_path"),
                    filename=_required_string(payload, "filename"),
                    total_size=_required_int(payload, "total_size"),
                    sha256_expected=_optional_string(payload, "sha256"),
                    chunk_size=_optional_int(payload, "chunk_size"),
                    auto_extract=bool(payload.get("auto_extract", False)),
                )
            except UploadError as exc:
                if self._metrics is not None and exc.code.startswith("UPLOAD.QUOTA"):
                    self._metrics.observe_upload_event(outcome="quota_rejected")
                return _upload_error(exc)
            if self._metrics is not None:
                self._metrics.observe_upload_event(
                    outcome="created", size_bytes=session.total_size
                )
            return ApiResponse(status=201, payload=session.to_dict())

        # POST /files/uploads/{id}/chunks
        if len(parts) == 4 and parts[1] == "uploads" and parts[3] == "chunks":
            payload, error = _json_body(body)
            if error is not None:
                return error
            try:
                index = _required_int(payload, "index")
                data_b64 = _required_string(payload, "data_b64")
                session = self.upload_service.put_chunk(
                    parts[2], owner, index, data_b64
                )
            except UploadError as exc:
                return _upload_error(exc)
            if self._metrics is not None:
                self._metrics.observe_upload_event(
                    outcome="chunk", size_bytes=session.chunk_size
                )
            return ApiResponse(status=200, payload=session.to_dict())

        # POST /files/uploads/{id}/complete
        if len(parts) == 4 and parts[1] == "uploads" and parts[3] == "complete":
            try:
                session = self.upload_service.complete(parts[2], owner)
            except UploadError as exc:
                if self._metrics is not None:
                    self._metrics.observe_upload_event(outcome="failed")
                return _upload_error(exc)
            if self._metrics is not None:
                self._metrics.observe_upload_event(outcome="completed")
            return ApiResponse(status=200, payload=session.to_dict())

        # POST /files/uploads/{id}/abort
        if len(parts) == 4 and parts[1] == "uploads" and parts[3] == "abort":
            try:
                session = self.upload_service.abort(parts[2], owner)
            except UploadError as exc:
                return _upload_error(exc)
            if self._metrics is not None:
                self._metrics.observe_upload_event(outcome="aborted")
            return ApiResponse(status=200, payload=session.to_dict())

        # POST /files/mkdir
        if len(parts) == 2 and parts[1] == "mkdir":
            payload, error = _json_body(body)
            if error is not None:
                return error
            path = _optional_string(payload, "path")
            if not path:
                return _error(400, "FILES.INVALID_REQUEST", "path is required")
            try:
                self.executor.make_dir(path=path, owner=owner)
            except SlurmSubmissionRejected as exc:
                return _error(403, "FILES.PATH_FORBIDDEN", str(exc))
            except SlurmTransportError as exc:
                return _error(502, "FILES.MKDIR_FAILED", str(exc))
            return ApiResponse(status=201, payload={"status": "ok", "path": path})

        # POST /files/delete
        if len(parts) == 2 and parts[1] == "delete":
            payload, error = _json_body(body)
            if error is not None:
                return error
            path = _optional_string(payload, "path")
            if not path:
                return _error(400, "FILES.INVALID_REQUEST", "path is required")
            try:
                self.executor.remove_path(path=path, owner=owner)
            except SlurmSubmissionRejected as exc:
                return _error(403, "FILES.PATH_FORBIDDEN", str(exc))
            except SlurmTransportError as exc:
                return _error(502, "FILES.DELETE_FAILED", str(exc))
            return ApiResponse(status=200, payload={"status": "ok", "path": path})

        # POST /files/rename  (rename or move; new_path may be in another dir)
        if len(parts) == 2 and parts[1] == "rename":
            payload, error = _json_body(body)
            if error is not None:
                return error
            path = _optional_string(payload, "path")
            new_path = _optional_string(payload, "new_path")
            if not path:
                return _error(400, "FILES.INVALID_REQUEST", "path is required")
            if not new_path:
                return _error(400, "FILES.INVALID_REQUEST", "new_path is required")
            overwrite = bool(payload.get("overwrite", False))
            try:
                self.executor.rename_path(
                    path=path, new_path=new_path, owner=owner, overwrite=overwrite
                )
            except SlurmSubmissionRejected as exc:
                message = str(exc)
                if "already exists" in message:
                    return _error(409, "FILES.TARGET_EXISTS", message)
                return _error(403, "FILES.PATH_FORBIDDEN", message)
            except SlurmTransportError as exc:
                return _error(502, "FILES.RENAME_FAILED", str(exc))
            return ApiResponse(
                status=200,
                payload={"status": "ok", "path": path, "new_path": new_path},
            )

        # POST /files/archive
        if len(parts) == 2 and parts[1] == "archive":
            payload, error = _json_body(body)
            if error is not None:
                return error
            paths = payload.get("paths")
            dest_dir = _optional_string(payload, "dest_dir")
            if not isinstance(paths, list) or not paths:
                return _error(400, "FILES.INVALID_REQUEST", "paths must be a non-empty list")
            if not all(isinstance(item, str) and item.strip() for item in paths):
                return _error(400, "FILES.INVALID_REQUEST", "paths must be strings")
            if not dest_dir:
                return _error(400, "FILES.INVALID_REQUEST", "dest_dir is required")
            archive_name = _optional_string(payload, "archive_name") or "archive.tar.gz"
            try:
                archive_path, size = self.executor.create_archive(
                    paths=[item.strip() for item in paths],
                    dest_dir=dest_dir,
                    archive_name=archive_name,
                    owner=owner,
                )
            except SlurmSubmissionRejected as exc:
                return _error(403, "FILES.PATH_FORBIDDEN", str(exc))
            except SlurmTransportError as exc:
                return _error(502, "FILES.ARCHIVE_FAILED", str(exc))
            return ApiResponse(
                status=201,
                payload={"status": "ok", "path": archive_path, "size": size},
            )

        return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _json_body(body: bytes) -> tuple[dict[str, Any], ApiResponse | None]:
    if not body:
        return {}, None
    try:
        parsed = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}, _error(400, "INVALID_JSON", "request body is not valid JSON")
    if not isinstance(parsed, dict):
        return {}, _error(400, "INVALID_JSON", "request body must be a JSON object")
    return parsed, None


def _first_param(params: Mapping[str, list[str]], key: str) -> str | None:
    values = params.get(key, [])
    if values and values[0].strip():
        return values[0].strip()
    return None


def _int_param(params: Mapping[str, list[str]], key: str, *, default: int) -> int:
    raw = _first_param(params, key)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _required_string(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise UploadError(f"{key} must be a non-empty string")
    return value.strip()


def _optional_string(payload: Mapping[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise UploadError(f"{key} must be a string")
    return value


def _required_int(payload: Mapping[str, Any], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise UploadError(f"{key} must be an integer")
    return value


def _optional_int(payload: Mapping[str, Any], key: str) -> int | None:
    value = payload.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise UploadError(f"{key} must be an integer")
    return value


def _upload_error(exc: UploadError) -> ApiResponse:
    return _error(exc.status, exc.code, str(exc))


def _error(status: int, code: str, message: str) -> ApiResponse:
    return ApiResponse(
        status=status, payload={"error": {"code": code, "message": message}}
    )
