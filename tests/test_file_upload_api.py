"""Route-level tests for the visual filesystem + tus resumable upload API.

Uses an in-memory ``FakeExecutor`` implementing the ``FileOpsExecutor``
protocol so the cluster side is deterministic and error mapping (forbidden
paths, transport failures) can be asserted without a real backend.
"""

from __future__ import annotations

import base64
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from pilot107.adapters.slurm import (
    DiskUsage,
    FileEntry,
    FileListPage,
    FileSearchEntry,
    FileSearchPage,
    FileStat,
    SlurmSubmissionRejected,
)
from pilot107.api.evidence_query import EvidenceQueryService
from pilot107.api.file_routes import FileRoutes
from pilot107.api.http_app import Pilot107HttpApi
from pilot107.core.file_uploads import FileUploadService
from pilot107.core.run_store import RunStore
from pilot107.worker.evidence import EvidenceStore

_TUS = "/api/v1/files/tus"
_PATCH_CONTENT_TYPE = "application/offset+octet-stream"


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _json(obj: object) -> bytes:
    return json.dumps(obj).encode("utf-8")


def _metadata_header(**fields: str) -> str:
    """Build a tus ``Upload-Metadata`` header (``key b64val,key b64val``)."""
    return ",".join(
        f"{key} {base64.b64encode(value.encode('utf-8')).decode('ascii')}"
        for key, value in fields.items()
    )


class FakeExecutor:
    """In-memory FileOpsExecutor; paths under /forbidden are rejected."""

    def __init__(self) -> None:
        self.files: dict[str, bytearray] = {}
        self.dirs: set[str] = {"/public/home/alice"}
        self.extract_calls: list[tuple[str, str]] = []
        self.search_calls: list[dict[str, object]] = []

    def _check(self, path: str) -> None:
        if path.startswith("/forbidden"):
            raise SlurmSubmissionRejected(f"path outside allowed roots: {path}")

    def write_bytes_chunk(self, *, path, data_b64, offset, owner, timeout_seconds=30.0) -> int:
        self._check(path)
        data = base64.b64decode(data_b64)
        buffer = self.files.setdefault(path, bytearray())
        if offset == 0:
            buffer[:] = data
        elif offset < 0:
            buffer.extend(data)
        else:
            buffer[offset:offset] = data
        return len(buffer)

    def read_bytes_chunk(self, *, path, offset, length, owner, timeout_seconds=30.0):
        self._check(path)
        if path not in self.files:
            from pilot107.adapters.slurm import SlurmTransportError

            raise SlurmTransportError(f"not a regular file: {path}")
        buffer = self.files[path]
        return _b64(bytes(buffer[offset : offset + length])), len(buffer)

    def file_sha256(self, *, path, owner, timeout_seconds=30.0) -> str:
        self._check(path)
        return hashlib.sha256(bytes(self.files[path])).hexdigest()

    def list_dir(
        self,
        *,
        path,
        owner,
        limit=500,
        cursor=None,
        timeout_seconds=30.0,
    ) -> FileListPage:
        self._check(path)
        prefix = path.rstrip("/") + "/"
        names: dict[str, str] = {}
        for dir_path in self.dirs:
            if dir_path.startswith(prefix) and dir_path != path:
                names[dir_path[len(prefix) :].split("/")[0]] = "dir"
        for file_path in self.files:
            if file_path.startswith(prefix):
                names[file_path[len(prefix) :].split("/")[0]] = "file"
        entries = [
            FileEntry(name=name, type=kind, size=0, mtime=0) for name, kind in sorted(names.items())
        ]
        offset = int(cursor or "0")
        page_entries = entries[offset : offset + limit]
        next_offset = offset + len(page_entries)
        has_more = next_offset < len(entries)
        return FileListPage(
            path=path,
            entries=tuple(page_entries),
            limit=limit,
            has_more=has_more,
            next_cursor=str(next_offset) if has_more else None,
            directory_revision="fake-v1",
        )

    def search_files(self, **kwargs) -> FileSearchPage:
        root = str(kwargs["root"])
        self._check(root)
        self.search_calls.append(kwargs)
        return FileSearchPage(
            items=(
                FileSearchEntry(
                    path=f"{root.rstrip('/')}/models/weights.bin",
                    relative_path="models/weights.bin",
                    type="file",
                    size=7,
                    mtime=1_700_000_000,
                ),
            ),
            incomplete=True,
            next_cursor="opaque-next",
            warnings=("unreadable directory: private",),
        )

    def make_dir(self, *, path, owner, timeout_seconds=30.0) -> None:
        self._check(path)
        self.dirs.add(path)

    def remove_path(self, *, path, owner, timeout_seconds=30.0) -> None:
        self._check(path)
        self.files.pop(path, None)
        self.dirs.discard(path)

    def rename_path(self, *, path, new_path, owner, overwrite=False, timeout_seconds=30.0) -> None:
        self._check(path)
        self._check(new_path)
        if path in self.files:
            if new_path in self.files and not overwrite:
                raise SlurmSubmissionRejected(f"target already exists: {new_path}")
            self.files[new_path] = self.files.pop(path)
        elif path in self.dirs:
            self.dirs.discard(path)
            self.dirs.add(new_path)
        else:
            from pilot107.adapters.slurm import SlurmTransportError

            raise SlurmTransportError(f"path does not exist: {path}")

    def stat_path(self, *, path, owner, timeout_seconds=30.0) -> FileStat:
        self._check(path)
        if path in self.files:
            return FileStat(path=path, type="file", size=len(self.files[path]), mtime=0)
        return FileStat(path=path, type="dir", size=0, mtime=0)

    def disk_usage(self, *, path, owner, timeout_seconds=30.0) -> DiskUsage:
        self._check(path)
        prefix = path.rstrip("/") + "/"
        used = sum(
            len(buffer)
            for file_path, buffer in self.files.items()
            if file_path == path or file_path.startswith(prefix)
        )
        return DiskUsage(path=path, used_bytes=used, total_bytes=None)

    def extract_archive(self, *, archive_path, dest_dir, owner, timeout_seconds=120.0) -> int:
        self._check(archive_path)
        self.extract_calls.append((archive_path, dest_dir))
        return 3

    def create_archive(self, *, paths, dest_dir, archive_name, owner, timeout_seconds=120.0):
        self._check(dest_dir)
        for item in paths:
            self._check(item)
        archive_path = f"{dest_dir.rstrip('/')}/{archive_name}"
        self.files[archive_path] = bytearray(b"TARDATA")
        return archive_path, len(self.files[archive_path])


class FileUploadApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        root = Path(self._temporary.name)
        run_store = RunStore(root / "pilot107.db")
        self.executor = FakeExecutor()
        upload_service = FileUploadService(
            executor=self.executor,
            owner_roots=("/public/home/{user}",),
            staging_root=root / "staging",
        )
        self.api = Pilot107HttpApi(
            store=run_store,
            evidence_query=EvidenceQueryService(
                store=run_store,
                evidence_store=EvidenceStore(root / "evidence"),
            ),
            file_routes=FileRoutes(upload_service=upload_service, executor=self.executor),
            auth_required=True,
        )

    def tearDown(self) -> None:
        self._temporary.cleanup()

    @staticmethod
    def _headers(user: str) -> dict[str, str]:
        return {"X-Pilot107-User": user}

    def test_search_route_returns_bounded_owner_scoped_page(self) -> None:
        response = self.api.handle_get(
            "/api/v1/files/search?root=/public/home/alice&q=model&kind=file&limit=20",
            headers=self._headers("alice"),
        )

        self.assertEqual(response.status, 200)
        self.assertEqual(response.payload["root"], "/public/home/alice")
        self.assertEqual(response.payload["items"][0]["relative_path"], "models/weights.bin")
        self.assertTrue(response.payload["incomplete"])
        self.assertEqual(response.payload["next_cursor"], "opaque-next")
        call = self.executor.search_calls[-1]
        self.assertEqual(call["owner"], "alice")
        self.assertEqual(call["scan_limit"], 10_000)
        self.assertEqual(call["time_limit_ms"], 750)

    def test_search_route_rejects_invalid_filters(self) -> None:
        invalid_queries = (
            "q=x&limit=101",
            "q=x&limit=not-a-number",
            "q=x&kind=symlink",
            "q=x&size_min=-1",
            "q=x&size_min=10&size_max=2",
            "q=x&mtime_from=2026-08-25T12%3A00%3A00",
        )
        for query in invalid_queries:
            with self.subTest(query=query):
                response = self.api.handle_get(
                    f"/api/v1/files/search?root=/public/home/alice&{query}",
                    headers=self._headers("alice"),
                )
                self.assertEqual(response.status, 400, response.payload)

    def test_search_route_rejects_forbidden_root(self) -> None:
        response = self.api.handle_get(
            "/api/v1/files/search?root=/forbidden/bob&q=x",
            headers=self._headers("alice"),
        )

        self.assertEqual(response.status, 403)
        self.assertEqual(response.payload["error"]["code"], "FILES.PATH_FORBIDDEN")

    # -- tus helpers -------------------------------------------------------

    def _create(
        self,
        *,
        length: int | None = None,
        user: str = "alice",
        metadata: str | None = None,
        concat: str | None = None,
    ):
        headers = {**self._headers(user), "Tus-Resumable": "1.0.0"}
        if length is not None:
            headers["Upload-Length"] = str(length)
        if metadata is not None:
            headers["Upload-Metadata"] = metadata
        if concat is not None:
            headers["Upload-Concat"] = concat
        return self.api.handle_post(_TUS, body=b"", headers=headers)

    @staticmethod
    def _upload_id_from(response) -> str:
        assert response.headers is not None
        return response.headers["Location"].rsplit("/", 1)[-1]

    def _patch(
        self,
        upload_id: str,
        offset: int,
        data: bytes,
        *,
        user: str = "alice",
        content_type: str = _PATCH_CONTENT_TYPE,
        include_offset: bool = True,
    ):
        headers = {**self._headers(user), "Tus-Resumable": "1.0.0"}
        if content_type is not None:
            headers["Content-Type"] = content_type
        if include_offset:
            headers["Upload-Offset"] = str(offset)
        return self.api.handle_patch(f"{_TUS}/{upload_id}", body=data, headers=headers)

    def _head(self, upload_id: str, *, user: str = "alice"):
        return self.api.handle_head(
            f"{_TUS}/{upload_id}", headers={**self._headers(user), "Tus-Resumable": "1.0.0"}
        )

    def _delete(self, upload_id: str, *, user: str = "alice"):
        return self.api.handle_delete(
            f"{_TUS}/{upload_id}", headers={**self._headers(user), "Tus-Resumable": "1.0.0"}
        )

    def _tus_upload(
        self,
        payload: bytes,
        *,
        filename: str = "blob.bin",
        target: str = "/public/home/alice",
        sha256: str | None = None,
        auto_extract: bool = False,
        user: str = "alice",
    ) -> str:
        """Create a tus upload, PATCH the whole payload, return the upload_id."""
        fields = {"filename": filename, "target_path": target}
        if sha256 is not None:
            fields["sha256"] = sha256
        if auto_extract:
            fields["auto_extract"] = "true"
        created = self._create(length=len(payload), user=user, metadata=_metadata_header(**fields))
        assert created.status == 201, created.payload
        upload_id = self._upload_id_from(created)
        new_offset = self._patch(upload_id, 0, payload, user=user)
        assert new_offset.status == 204, new_offset.payload
        return upload_id

    def _complete(self, upload_id: str, *, user: str = "alice"):
        return self.api.handle_post(
            f"/api/v1/files/uploads/{upload_id}/complete", headers=self._headers(user)
        )

    # -- tus flow ----------------------------------------------------------

    def test_tus_full_upload_flow_writes_to_cluster(self) -> None:
        payload = bytes(range(256)) * 2
        digest = hashlib.sha256(payload).hexdigest()
        created = self._create(
            length=len(payload),
            metadata=_metadata_header(
                filename="blob.bin",
                target_path="/public/home/alice",
                sha256=digest,
            ),
        )
        self.assertEqual(created.status, 201, created.payload)
        assert created.headers is not None
        self.assertEqual(created.headers["Tus-Resumable"], "1.0.0")
        upload_id = self._upload_id_from(created)

        # Append in two slices, resuming from the reported offset.
        half = len(payload) // 2
        first = self._patch(upload_id, 0, payload[:half])
        self.assertEqual(first.status, 204, first.payload)
        assert first.headers is not None
        self.assertEqual(first.headers["Upload-Offset"], str(half))

        resumed = self._head(upload_id)
        self.assertEqual(resumed.status, 200)
        assert resumed.headers is not None
        self.assertEqual(resumed.headers["Upload-Offset"], str(half))
        self.assertEqual(resumed.headers["Upload-Length"], str(len(payload)))

        second = self._patch(upload_id, half, payload[half:])
        self.assertEqual(second.status, 204, second.payload)

        completed = self._complete(upload_id)
        self.assertEqual(completed.status, 200, completed.payload)
        self.assertEqual(completed.payload["state"], "written")
        self.assertEqual(completed.payload["sha256_actual"], digest)
        self.assertEqual(bytes(self.executor.files["/public/home/alice/blob.bin"]), payload)

    def test_tus_create_requires_upload_length(self) -> None:
        created = self._create(
            metadata=_metadata_header(filename="x.bin", target_path="/public/home/alice")
        )
        self.assertEqual(created.status, 400)
        self.assertEqual(created.payload["error"]["code"], "TUS.MISSING_LENGTH")

    def test_tus_create_requires_target_metadata(self) -> None:
        created = self._create(length=16)
        self.assertEqual(created.status, 400)
        self.assertEqual(created.payload["error"]["code"], "TUS.MISSING_METADATA")

    def test_tus_patch_offset_mismatch_returns_409(self) -> None:
        upload_id = self._tus_upload(b"A" * 32)
        # The upload is already complete (offset 32); PATCH at 0 is a rewrite
        # but PATCH ahead of the received offset is a gap -> 409.
        gap = self._patch(upload_id, 64, b"B" * 16)
        self.assertEqual(gap.status, 409)
        self.assertEqual(gap.payload["error"]["code"], "UPLOAD.OFFSET_MISMATCH")

    def test_tus_patch_wrong_content_type_returns_415(self) -> None:
        created = self._create(
            length=16,
            metadata=_metadata_header(filename="x.bin", target_path="/public/home/alice"),
        )
        upload_id = self._upload_id_from(created)
        response = self._patch(upload_id, 0, b"A" * 16, content_type="application/json")
        self.assertEqual(response.status, 415)
        self.assertEqual(response.payload["error"]["code"], "TUS.INVALID_CONTENT_TYPE")

    def test_tus_patch_missing_offset_returns_400(self) -> None:
        created = self._create(
            length=16,
            metadata=_metadata_header(filename="x.bin", target_path="/public/home/alice"),
        )
        upload_id = self._upload_id_from(created)
        response = self._patch(upload_id, 0, b"A" * 16, include_offset=False)
        self.assertEqual(response.status, 400)
        self.assertEqual(response.payload["error"]["code"], "TUS.MISSING_OFFSET")

    def test_tus_head_unknown_returns_404(self) -> None:
        response = self._head("does-not-exist")
        self.assertEqual(response.status, 404)
        self.assertEqual(response.payload["error"]["code"], "UPLOAD.NOT_FOUND")

    def test_tus_delete_terminates_session(self) -> None:
        created = self._create(
            length=32,
            metadata=_metadata_header(filename="x.bin", target_path="/public/home/alice"),
        )
        upload_id = self._upload_id_from(created)
        self._patch(upload_id, 0, b"A" * 16)

        deleted = self._delete(upload_id)
        self.assertEqual(deleted.status, 204)
        assert deleted.headers is not None
        self.assertEqual(deleted.headers["Tus-Resumable"], "1.0.0")

        # The session is now aborted: further appends are rejected and the
        # session reports the terminal aborted state.
        patched = self._patch(upload_id, 16, b"B" * 16)
        self.assertEqual(patched.status, 400)
        self.assertEqual(patched.payload["error"]["code"], "UPLOAD.STATE")
        fetched = self.api.handle_get(
            f"/api/v1/files/uploads/{upload_id}", headers=self._headers("alice")
        )
        self.assertEqual(fetched.status, 200)
        self.assertEqual(fetched.payload["state"], "aborted")

    def test_tus_options_advertises_capabilities(self) -> None:
        response = self.api.handle_options(_TUS, headers=self._headers("alice"))
        self.assertEqual(response.status, 204)
        assert response.headers is not None
        self.assertEqual(response.headers["Tus-Version"], "1.0.0")
        self.assertEqual(response.headers["Tus-Extension"], "creation,termination,concatenation")
        self.assertIn("Tus-Max-Size", response.headers)

    def test_tus_concat_merges_three_partials(self) -> None:
        parts = [b"A" * 100, b"B" * 60, b"C" * 40]
        payload = b"".join(parts)
        digest = hashlib.sha256(payload).hexdigest()

        partial_ids = []
        for part in parts:
            created = self._create(length=len(part), concat="partial")
            self.assertEqual(created.status, 201, created.payload)
            partial_id = self._upload_id_from(created)
            patched = self._patch(partial_id, 0, part)
            self.assertEqual(patched.status, 204, patched.payload)
            partial_ids.append(partial_id)

        # tus-js-client sends ``final;`` with absolute URLs and no Upload-Length.
        concat_header = "final;" + " ".join(f"{_TUS}/{pid}" for pid in partial_ids)
        merged = self._create(
            concat=concat_header,
            metadata=_metadata_header(
                filename="merged.bin",
                target_path="/public/home/alice",
                sha256=digest,
            ),
        )
        self.assertEqual(merged.status, 201, merged.payload)
        merged_id = self._upload_id_from(merged)

        completed = self._complete(merged_id)
        self.assertEqual(completed.status, 200, completed.payload)
        self.assertEqual(completed.payload["state"], "written")
        self.assertEqual(bytes(self.executor.files["/public/home/alice/merged.bin"]), payload)
        # Partials are cleaned up after the merge.
        for partial_id in partial_ids:
            self.assertEqual(self._head(partial_id).status, 404)

    def test_tus_concat_accepts_spec_concat_prefix(self) -> None:
        created = self._create(length=16, concat="partial")
        partial_id = self._upload_id_from(created)
        self._patch(partial_id, 0, b"Z" * 16)

        merged = self._create(
            concat=f"concat;{_TUS}/{partial_id}",
            metadata=_metadata_header(filename="spec.bin", target_path="/public/home/alice"),
        )
        self.assertEqual(merged.status, 201, merged.payload)
        merged_id = self._upload_id_from(merged)
        completed = self._complete(merged_id)
        self.assertEqual(completed.status, 200, completed.payload)
        self.assertEqual(bytes(self.executor.files["/public/home/alice/spec.bin"]), b"Z" * 16)

    def test_tus_concat_incomplete_partial_returns_409(self) -> None:
        created = self._create(length=32, concat="partial")
        partial_id = self._upload_id_from(created)
        self._patch(partial_id, 0, b"A" * 8)  # only a quarter received

        merged = self._create(
            concat=f"final;{_TUS}/{partial_id}",
            metadata=_metadata_header(filename="x.bin", target_path="/public/home/alice"),
        )
        self.assertEqual(merged.status, 409)
        self.assertEqual(merged.payload["error"]["code"], "UPLOAD.CONCAT_INCOMPLETE")

    def test_tus_sha256_mismatch_returns_409(self) -> None:
        upload_id = self._tus_upload(b"H" * 32, sha256="0" * 64)
        completed = self._complete(upload_id)
        self.assertEqual(completed.status, 409)
        self.assertEqual(completed.payload["error"]["code"], "UPLOAD.SHA256_MISMATCH")

    def test_tus_create_target_outside_roots_returns_403(self) -> None:
        created = self._create(
            length=16,
            metadata=_metadata_header(filename="x.bin", target_path="/etc"),
        )
        self.assertEqual(created.status, 403)
        self.assertEqual(created.payload["error"]["code"], "UPLOAD.PATH_FORBIDDEN")

    def test_tus_create_rejects_lexical_parent_traversal(self) -> None:
        created = self._create(
            length=1,
            metadata=_metadata_header(
                filename="x.bin",
                target_path="/public/home/alice/../bob",
            ),
        )

        self.assertEqual(created.status, 400)
        self.assertEqual(created.payload["error"]["code"], "UPLOAD.UNSAFE_PATH")

    def test_tus_cross_owner_head_is_not_found(self) -> None:
        upload_id = self._tus_upload(b"I" * 16)
        self.assertEqual(self._head(upload_id, user="bob").status, 404)

    def test_tus_missing_identity_returns_401(self) -> None:
        response = self.api.handle_post(
            _TUS,
            body=b"",
            headers={"Upload-Length": "16", "Tus-Resumable": "1.0.0"},
        )
        self.assertEqual(response.status, 401)

    def test_get_session_progress_and_list(self) -> None:
        upload_id = self._tus_upload(b"G" * 32)
        fetched = self.api.handle_get(
            f"/api/v1/files/uploads/{upload_id}", headers=self._headers("alice")
        )
        self.assertEqual(fetched.status, 200)
        self.assertEqual(fetched.payload["received_bytes"], 32)
        self.assertFalse(fetched.payload["is_partial"])

        listed = self.api.handle_get("/api/v1/files/uploads", headers=self._headers("alice"))
        self.assertEqual(listed.status, 200)
        self.assertEqual(len(listed.payload["items"]), 1)

    def test_abort_session(self) -> None:
        upload_id = self._tus_upload(b"J" * 16)
        aborted = self.api.handle_post(
            f"/api/v1/files/uploads/{upload_id}/abort", headers=self._headers("alice")
        )
        self.assertEqual(aborted.status, 200)
        self.assertEqual(aborted.payload["state"], "aborted")

    def test_missing_identity_returns_401(self) -> None:
        response = self.api.handle_get("/api/v1/files?path=/public/home/alice")
        self.assertEqual(response.status, 401)

    def test_list_dir_route(self) -> None:
        self.executor.files["/public/home/alice/run.sh"] = bytearray(b"#!/bin/bash\n")
        response = self.api.handle_get(
            "/api/v1/files?path=/public/home/alice", headers=self._headers("alice")
        )
        self.assertEqual(response.status, 200)
        names = {entry["name"]: entry["type"] for entry in response.payload["entries"]}
        self.assertEqual(names["run.sh"], "file")

    def test_content_read_route(self) -> None:
        self.executor.files["/public/home/alice/data.bin"] = bytearray(b"0123456789")
        response = self.api.handle_get(
            "/api/v1/files/content?path=/public/home/alice/data.bin&offset=2&length=4",
            headers=self._headers("alice"),
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(base64.b64decode(response.payload["data_b64"]), b"2345")
        self.assertEqual(response.payload["size"], 10)

    def test_usage_route_defaults_to_home(self) -> None:
        self.executor.files["/public/home/alice/a.txt"] = bytearray(b"12345")
        self.executor.files["/public/home/alice/sub/b.txt"] = bytearray(b"678")
        response = self.api.handle_get("/api/v1/files/usage", headers=self._headers("alice"))
        self.assertEqual(response.status, 200)
        self.assertEqual(response.payload["home"], "/public/home/alice")
        self.assertEqual(response.payload["used_bytes"], 8)
        self.assertIsNone(response.payload["total_bytes"])
        self.assertIn("observed_at", response.payload)

    def test_usage_route_forbidden_path_maps_to_403(self) -> None:
        response = self.api.handle_get(
            "/api/v1/files/usage?path=/forbidden/place", headers=self._headers("alice")
        )
        self.assertEqual(response.status, 403)
        self.assertEqual(response.payload["error"]["code"], "FILES.PATH_FORBIDDEN")

    def test_mkdir_and_delete_routes(self) -> None:
        created = self.api.handle_post(
            "/api/v1/files/mkdir",
            body=_json({"path": "/public/home/alice/newdir"}),
            headers=self._headers("alice"),
        )
        self.assertEqual(created.status, 201)
        self.assertIn("/public/home/alice/newdir", self.executor.dirs)

        self.executor.files["/public/home/alice/gone.txt"] = bytearray(b"x")
        deleted = self.api.handle_post(
            "/api/v1/files/delete",
            body=_json({"path": "/public/home/alice/gone.txt"}),
            headers=self._headers("alice"),
        )
        self.assertEqual(deleted.status, 200)
        self.assertNotIn("/public/home/alice/gone.txt", self.executor.files)

    def test_forbidden_path_maps_to_403(self) -> None:
        response = self.api.handle_get(
            "/api/v1/files?path=/forbidden/place", headers=self._headers("alice")
        )
        self.assertEqual(response.status, 403)
        self.assertEqual(response.payload["error"]["code"], "FILES.PATH_FORBIDDEN")

    def test_archive_route_packs_paths(self) -> None:
        self.executor.files["/public/home/alice/a.txt"] = bytearray(b"1")
        self.executor.files["/public/home/alice/b.txt"] = bytearray(b"2")
        response = self.api.handle_post(
            "/api/v1/files/archive",
            body=_json(
                {
                    "paths": [
                        "/public/home/alice/a.txt",
                        "/public/home/alice/b.txt",
                    ],
                    "dest_dir": "/public/home/alice",
                    "archive_name": "bundle.tar.gz",
                }
            ),
            headers=self._headers("alice"),
        )
        self.assertEqual(response.status, 201, response.payload)
        self.assertEqual(response.payload["path"], "/public/home/alice/bundle.tar.gz")
        self.assertIn("/public/home/alice/bundle.tar.gz", self.executor.files)

    def test_archive_requires_paths(self) -> None:
        response = self.api.handle_post(
            "/api/v1/files/archive",
            body=_json({"paths": [], "dest_dir": "/public/home/alice"}),
            headers=self._headers("alice"),
        )
        self.assertEqual(response.status, 400)

    def test_rename_route_moves_file(self) -> None:
        self.executor.files["/public/home/alice/old.txt"] = bytearray(b"data")
        response = self.api.handle_post(
            "/api/v1/files/rename",
            body=_json(
                {
                    "path": "/public/home/alice/old.txt",
                    "new_path": "/public/home/alice/new.txt",
                }
            ),
            headers=self._headers("alice"),
        )
        self.assertEqual(response.status, 200, response.payload)
        self.assertNotIn("/public/home/alice/old.txt", self.executor.files)
        self.assertIn("/public/home/alice/new.txt", self.executor.files)

    def test_rename_rejects_existing_target_without_overwrite(self) -> None:
        self.executor.files["/public/home/alice/a.txt"] = bytearray(b"1")
        self.executor.files["/public/home/alice/b.txt"] = bytearray(b"2")
        response = self.api.handle_post(
            "/api/v1/files/rename",
            body=_json(
                {
                    "path": "/public/home/alice/a.txt",
                    "new_path": "/public/home/alice/b.txt",
                }
            ),
            headers=self._headers("alice"),
        )
        self.assertEqual(response.status, 409)

    def test_rename_requires_new_path(self) -> None:
        response = self.api.handle_post(
            "/api/v1/files/rename",
            body=_json({"path": "/public/home/alice/a.txt"}),
            headers=self._headers("alice"),
        )
        self.assertEqual(response.status, 400)

    def test_extract_route_unpacks_archive(self) -> None:
        response = self.api.handle_post(
            "/api/v1/files/extract",
            body=_json(
                {
                    "path": "/public/home/alice/bundle.tar.gz",
                    "dest_dir": "/public/home/alice/out",
                }
            ),
            headers=self._headers("alice"),
        )
        self.assertEqual(response.status, 200, response.payload)
        self.assertEqual(response.payload["members"], 3)
        self.assertEqual(response.payload["dest_dir"], "/public/home/alice/out")
        self.assertEqual(
            self.executor.extract_calls,
            [("/public/home/alice/bundle.tar.gz", "/public/home/alice/out")],
        )

    def test_extract_defaults_dest_dir_to_archive_parent(self) -> None:
        response = self.api.handle_post(
            "/api/v1/files/extract",
            body=_json({"path": "/public/home/alice/data/bundle.tar.gz"}),
            headers=self._headers("alice"),
        )
        self.assertEqual(response.status, 200, response.payload)
        self.assertEqual(response.payload["dest_dir"], "/public/home/alice/data")
        self.assertEqual(
            self.executor.extract_calls,
            [("/public/home/alice/data/bundle.tar.gz", "/public/home/alice/data")],
        )

    def test_extract_rejects_forbidden_path(self) -> None:
        response = self.api.handle_post(
            "/api/v1/files/extract",
            body=_json({"path": "/forbidden/bundle.tar.gz"}),
            headers=self._headers("alice"),
        )
        self.assertEqual(response.status, 403)
        self.assertEqual(response.payload["error"]["code"], "FILES.PATH_FORBIDDEN")

    def test_extract_requires_path(self) -> None:
        response = self.api.handle_post(
            "/api/v1/files/extract",
            body=_json({"dest_dir": "/public/home/alice"}),
            headers=self._headers("alice"),
        )
        self.assertEqual(response.status, 400)


class FileUploadQuotaApiTests(unittest.TestCase):
    """tus creation surfaces per-owner quota rejections as 429."""

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        root = Path(self._temporary.name)
        run_store = RunStore(root / "pilot107.db")
        self.executor = FakeExecutor()
        upload_service = FileUploadService(
            executor=self.executor,
            owner_roots=("/public/home/{user}",),
            staging_root=root / "staging",
            max_active_per_owner=2,
            max_total_bytes_per_owner=1024,
        )
        self.api = Pilot107HttpApi(
            store=run_store,
            evidence_query=EvidenceQueryService(
                store=run_store,
                evidence_store=EvidenceStore(root / "evidence"),
            ),
            file_routes=FileRoutes(upload_service=upload_service, executor=self.executor),
            auth_required=True,
        )

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def _create(self, length: int) -> object:
        return self.api.handle_post(
            _TUS,
            body=b"",
            headers={
                "X-Pilot107-User": "alice",
                "Tus-Resumable": "1.0.0",
                "Upload-Length": str(length),
                "Upload-Metadata": _metadata_header(
                    filename="q.bin", target_path="/public/home/alice"
                ),
            },
        )

    def test_byte_quota_returns_429(self) -> None:
        self.assertEqual(self._create(900).status, 201)
        rejected = self._create(200)
        self.assertEqual(rejected.status, 429)
        self.assertEqual(rejected.payload["error"]["code"], "UPLOAD.QUOTA_BYTES")

    def test_concurrent_quota_returns_429(self) -> None:
        self.assertEqual(self._create(100).status, 201)
        self.assertEqual(self._create(100).status, 201)
        rejected = self._create(100)
        self.assertEqual(rejected.status, 429)
        self.assertEqual(rejected.payload["error"]["code"], "UPLOAD.QUOTA_CONCURRENT")


if __name__ == "__main__":
    unittest.main()
