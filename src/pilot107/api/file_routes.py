"""HTTP boundary for the visual filesystem and tus resumable uploads.

Routes (all under ``/api/v1/files``), owner-scoped via ``X-Pilot107-User``:

GET  /files?path=                      list a directory
GET  /files/content?path=&offset=&length=   read a byte range (base64)
GET  /files/usage?path=                recursive storage usage (defaults to home)
GET  /files/uploads                    list the owner's upload sessions
GET  /files/uploads/{id}               one upload session's progress
POST /files/uploads/{id}/complete      verify + write (+ optional extract)
POST /files/uploads/{id}/abort         abandon a session
POST /files/mkdir                      create a directory
POST /files/create-file                create an empty file
POST /files/delete                     remove a file or tree
POST /files/rename                     rename or move a file/directory
POST /files/copy                       copy files/directories into a directory
POST /files/archive                    pack paths into a tar.gz
POST /files/extract                    safely unpack an archive (tar/zip/rar)

tus resumable-upload endpoints (``Tus-Resumable: 1.0.0``), backing the
browser's ``tus-js-client``.  Creation carries the destination in
``Upload-Metadata``; parallel uploads use the concatenation extension:

OPTIONS /files/tus                     capability discovery
POST    /files/tus                     create (normal | ``Upload-Concat: partial``
                                        | ``Upload-Concat: concat;<urls>``)
HEAD    /files/tus/{id}                current ``Upload-Offset`` (resume probe)
PATCH   /files/tus/{id}                append raw bytes at ``Upload-Offset``
DELETE  /files/tus/{id}                terminate + purge a session

Path authorization is enforced both here (upload destinations against the
owner roots) and inside the executor backend (every primitive re-checks the
owner's allowed roots before moving a byte).
"""

from __future__ import annotations

import base64
import binascii
import json
import posixpath
from collections.abc import Mapping
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from pilot107.adapters.slurm import (
    FileOpsExecutor,
    SlurmSubmissionRejected,
    SlurmTransportError,
)
from pilot107.api.http_types import ApiResponse
from pilot107.api.metrics import ControlPlaneMetrics
from pilot107.core.file_uploads import (
    DEFAULT_MAX_TOTAL_BYTES_PER_OWNER,
    FileUploadService,
    UploadError,
    UploadNotFound,
)
from pilot107.core.identity import UserIdentity
from pilot107.core.path_policy import OwnerRootPolicyError, resolve_owner_roots

_DEFAULT_READ_LENGTH = 1024 * 1024
_MAX_READ_LENGTH = 2 * 1024 * 1024

_TUS_VERSION = "1.0.0"
_TUS_EXTENSIONS = "creation,termination,concatenation"
_TUS_MAX_SIZE = DEFAULT_MAX_TOTAL_BYTES_PER_OWNER
_TUS_CONTENT_TYPE = "application/offset+octet-stream"


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

        # GET /files/usage?path=
        if len(parts) == 2 and parts[1] == "usage":
            path = _first_param(params, "path")
            if not path:
                try:
                    roots = resolve_owner_roots(
                        self.upload_service.owner_roots, user=owner
                    )
                except OwnerRootPolicyError as exc:
                    return _error(403, "FILES.PATH_FORBIDDEN", str(exc))
                if not roots:
                    return _error(404, "FILES.NO_HOME", "no owner root configured")
                path = roots[0]
            try:
                usage = self.executor.disk_usage(path=path, owner=owner)
            except SlurmSubmissionRejected as exc:
                return _error(403, "FILES.PATH_FORBIDDEN", str(exc))
            except SlurmTransportError as exc:
                return _error(502, "FILES.USAGE_FAILED", str(exc))
            return ApiResponse(
                status=200,
                payload={
                    "home": usage.path,
                    "used_bytes": usage.used_bytes,
                    "total_bytes": usage.total_bytes,
                    "observed_at": datetime.now(UTC).isoformat(),
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
        headers: Mapping[str, str] | None = None,
    ) -> ApiResponse | None:
        if not parts or parts[0] != "files":
            return None
        if identity is None:
            return _error(401, "AUTH.MISSING", "identity required for file operations")
        owner = identity.username

        # POST /files/tus  (tus creation: normal | partial | concatenation)
        if len(parts) == 2 and parts[1] == "tus":
            return self._tus_create(headers or {}, owner=owner)

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

        # POST /files/copy  (batch copy into dest_dir; overwrites like move)
        if len(parts) == 2 and parts[1] == "copy":
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
            try:
                copied = self.executor.copy_entries(
                    paths=[item.strip() for item in paths],
                    dest_dir=dest_dir,
                    owner=owner,
                )
            except SlurmSubmissionRejected as exc:
                return _error(403, "FILES.PATH_FORBIDDEN", str(exc))
            except SlurmTransportError as exc:
                return _error(502, "FILES.COPY_FAILED", str(exc))
            return ApiResponse(
                status=200,
                payload={"status": "ok", "copied": copied, "dest_dir": dest_dir},
            )

        # POST /files/create-file  (empty file; 409 when the name is taken)
        if len(parts) == 2 and parts[1] == "create-file":
            payload, error = _json_body(body)
            if error is not None:
                return error
            dir_path = _optional_string(payload, "dir")
            name = _optional_string(payload, "name")
            if not dir_path:
                return _error(400, "FILES.INVALID_REQUEST", "dir is required")
            if not name:
                return _error(400, "FILES.INVALID_REQUEST", "name is required")
            try:
                created = self.executor.create_file(
                    dir_path=dir_path, name=name, owner=owner
                )
            except SlurmSubmissionRejected as exc:
                message = str(exc)
                if "already exists" in message:
                    return _error(409, "FILES.TARGET_EXISTS", message)
                return _error(403, "FILES.PATH_FORBIDDEN", message)
            except SlurmTransportError as exc:
                return _error(502, "FILES.CREATE_FAILED", str(exc))
            return ApiResponse(status=201, payload={"status": "ok", "path": created})

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

        # POST /files/extract  (dest_dir defaults to the archive's parent)
        if len(parts) == 2 and parts[1] == "extract":
            payload, error = _json_body(body)
            if error is not None:
                return error
            path = _optional_string(payload, "path")
            if not path:
                return _error(400, "FILES.INVALID_REQUEST", "path is required")
            dest_dir = _optional_string(payload, "dest_dir") or posixpath.dirname(
                path.rstrip("/")
            )
            if not dest_dir:
                dest_dir = "/"
            try:
                members = self.executor.extract_archive(
                    archive_path=path, dest_dir=dest_dir, owner=owner
                )
            except SlurmSubmissionRejected as exc:
                return _error(403, "FILES.PATH_FORBIDDEN", str(exc))
            except SlurmTransportError as exc:
                return _error(502, "FILES.EXTRACT_FAILED", str(exc))
            return ApiResponse(
                status=200,
                payload={"status": "ok", "members": members, "dest_dir": dest_dir},
            )

        return None

    # -- tus resumable uploads ---------------------------------------------

    def handle_options(
        self,
        parts: list[str],
        *,
        identity: UserIdentity | None,
    ) -> ApiResponse | None:
        if not parts or parts[0] != "files":
            return None
        if not (len(parts) == 2 and parts[1] == "tus"):
            return None
        return ApiResponse(
            status=204,
            payload={},
            headers={
                "Tus-Resumable": _TUS_VERSION,
                "Tus-Version": _TUS_VERSION,
                "Tus-Extension": _TUS_EXTENSIONS,
                "Tus-Max-Size": str(_TUS_MAX_SIZE),
            },
        )

    def handle_head(
        self,
        parts: list[str],
        *,
        identity: UserIdentity | None,
    ) -> ApiResponse | None:
        if not parts or parts[0] != "files":
            return None
        if identity is None:
            return _error(401, "AUTH.MISSING", "identity required for file operations")
        owner = identity.username
        # HEAD /files/tus/{id}
        if len(parts) == 3 and parts[1] == "tus":
            try:
                session = self.upload_service.get_session(parts[2], owner)
            except UploadNotFound as exc:
                return _tus_error(exc)
            return ApiResponse(
                status=200,
                payload={},
                headers={
                    "Tus-Resumable": _TUS_VERSION,
                    "Upload-Offset": str(session.received_bytes),
                    "Upload-Length": str(session.total_size),
                    "Cache-Control": "no-store",
                },
            )
        return None

    def handle_patch(
        self,
        parts: list[str],
        *,
        body: bytes,
        identity: UserIdentity | None,
        headers: Mapping[str, str] | None = None,
    ) -> ApiResponse | None:
        if not parts or parts[0] != "files":
            return None
        if identity is None:
            return _error(401, "AUTH.MISSING", "identity required for file operations")
        owner = identity.username
        # PATCH /files/tus/{id}
        if len(parts) == 3 and parts[1] == "tus":
            request_headers = headers or {}
            content_type = _header(request_headers, "Content-Type") or ""
            if content_type.split(";", 1)[0].strip() != _TUS_CONTENT_TYPE:
                return _tus_error_response(
                    415,
                    "TUS.INVALID_CONTENT_TYPE",
                    f"Content-Type must be {_TUS_CONTENT_TYPE}",
                )
            offset_raw = _header(request_headers, "Upload-Offset")
            if offset_raw is None:
                return _tus_error_response(
                    400, "TUS.MISSING_OFFSET", "Upload-Offset header is required"
                )
            try:
                offset = int(str(offset_raw).strip())
            except ValueError:
                return _tus_error_response(
                    400, "TUS.INVALID_OFFSET", "Upload-Offset must be an integer"
                )
            data = body or b""
            try:
                new_offset = self.upload_service.append_bytes(
                    parts[2], owner, offset, data
                )
            except UploadError as exc:
                return _tus_error(exc)
            if self._metrics is not None:
                self._metrics.observe_upload_event(
                    outcome="chunk", size_bytes=len(data)
                )
            return ApiResponse(
                status=204,
                payload={},
                headers={
                    "Tus-Resumable": _TUS_VERSION,
                    "Upload-Offset": str(new_offset),
                },
            )
        return None

    def handle_delete(
        self,
        parts: list[str],
        *,
        identity: UserIdentity | None,
    ) -> ApiResponse | None:
        if not parts or parts[0] != "files":
            return None
        if identity is None:
            return _error(401, "AUTH.MISSING", "identity required for file operations")
        owner = identity.username
        # DELETE /files/tus/{id}
        if len(parts) == 3 and parts[1] == "tus":
            try:
                self.upload_service.abort(parts[2], owner)
            except UploadError as exc:
                return _tus_error(exc)
            if self._metrics is not None:
                self._metrics.observe_upload_event(outcome="aborted")
            return ApiResponse(
                status=204, payload={}, headers={"Tus-Resumable": _TUS_VERSION}
            )
        return None

    def _tus_create(self, headers: Mapping[str, str], *, owner: str) -> ApiResponse:
        upload_length = _parse_upload_length(_header(headers, "Upload-Length"))
        concat = (_header(headers, "Upload-Concat") or "").strip()
        metadata = _parse_tus_metadata(_header(headers, "Upload-Metadata"))

        # concatenation: merge completed partial uploads into a whole session.
        # The tus spec uses ``concat;`` while tus-js-client sends ``final;``;
        # accept both.
        concat_lower = concat.lower()
        if concat_lower.startswith("concat;") or concat_lower.startswith("final;"):
            partial_urls = concat.split(";", 1)[1].strip().split()
            partial_ids = [
                url.rstrip("/").rsplit("/", 1)[-1]
                for url in partial_urls
                if url.strip()
            ]
            if not partial_ids:
                return _tus_error_response(
                    400, "TUS.CONCAT_INVALID", "Upload-Concat lists no partial uploads"
                )
            try:
                session = self.upload_service.concatenate(
                    owner=owner,
                    partial_ids=partial_ids,
                    total_size=upload_length,
                    target_path=metadata.get("target_path", ""),
                    filename=metadata.get("filename", ""),
                    sha256_expected=metadata.get("sha256") or None,
                    auto_extract=_metadata_bool(metadata.get("auto_extract")),
                )
            except UploadError as exc:
                return _tus_error(exc)
            if self._metrics is not None:
                self._metrics.observe_upload_event(
                    outcome="created", size_bytes=session.total_size
                )
            return _tus_created(session.upload_id)

        # partial: a parallel-upload byte bucket (destination set at concat)
        if concat.lower() == "partial":
            if upload_length is None:
                return _tus_error_response(
                    400, "TUS.MISSING_LENGTH", "Upload-Length is required"
                )
            try:
                session = self.upload_service.create_partial_session(
                    owner=owner, total_size=upload_length
                )
            except UploadError as exc:
                return _tus_error(exc)
            return _tus_created(session.upload_id)

        # normal creation
        if upload_length is None:
            return _tus_error_response(
                400, "TUS.MISSING_LENGTH", "Upload-Length is required"
            )
        target_path = metadata.get("target_path")
        filename = metadata.get("filename")
        if not target_path or not filename:
            return _tus_error_response(
                400,
                "TUS.MISSING_METADATA",
                "target_path and filename metadata are required",
            )
        try:
            session = self.upload_service.create_session(
                owner=owner,
                target_path=target_path,
                filename=filename,
                total_size=upload_length,
                sha256_expected=metadata.get("sha256") or None,
                auto_extract=_metadata_bool(metadata.get("auto_extract")),
            )
        except UploadError as exc:
            if self._metrics is not None and exc.code.startswith("UPLOAD.QUOTA"):
                self._metrics.observe_upload_event(outcome="quota_rejected")
            return _tus_error(exc)
        if self._metrics is not None:
            self._metrics.observe_upload_event(
                outcome="created", size_bytes=session.total_size
            )
        return _tus_created(session.upload_id)


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


def _header(headers: Mapping[str, str], name: str) -> str | None:
    """Case-insensitive header lookup (WSGI/http.server style mappings)."""
    lowered = name.lower()
    for key, value in headers.items():
        if key.lower() == lowered:
            return value
    return None


def _parse_upload_length(raw: str | None) -> int | None:
    if raw is None:
        return None
    try:
        value = int(str(raw).strip())
    except ValueError:
        return None
    return value if value >= 0 else None


def _parse_tus_metadata(raw: str | None) -> dict[str, str]:
    """Parse ``Upload-Metadata`` (``key b64val,key b64val``) into strings."""
    metadata: dict[str, str] = {}
    if not raw:
        return metadata
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair:
            continue
        key, _, value_b64 = pair.partition(" ")
        key = key.strip()
        value_b64 = value_b64.strip()
        if not key:
            continue
        if value_b64:
            try:
                metadata[key] = base64.b64decode(value_b64).decode("utf-8")
            except (binascii.Error, UnicodeDecodeError, ValueError):
                metadata[key] = ""
        else:
            metadata[key] = ""
    return metadata


def _metadata_bool(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes"}


def _tus_created(upload_id: str) -> ApiResponse:
    return ApiResponse(
        status=201,
        payload={},
        headers={
            "Tus-Resumable": _TUS_VERSION,
            "Location": f"/api/v1/files/tus/{upload_id}",
        },
    )


def _tus_error(exc: UploadError) -> ApiResponse:
    return _tus_error_response(exc.status, exc.code, str(exc))


def _tus_error_response(status: int, code: str, message: str) -> ApiResponse:
    return ApiResponse(
        status=status,
        payload={"error": {"code": code, "message": message}},
        headers={"Tus-Resumable": _TUS_VERSION},
    )


def _optional_string(payload: Mapping[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise UploadError(f"{key} must be a string")
    return value


def _upload_error(exc: UploadError) -> ApiResponse:
    return _error(exc.status, exc.code, str(exc))


def _error(status: int, code: str, message: str) -> ApiResponse:
    return ApiResponse(
        status=status, payload={"error": {"code": code, "message": message}}
    )
