"""Route-level tests for the visual filesystem + chunked upload API.

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
    FileEntry,
    FileStat,
    SlurmSubmissionRejected,
)
from pilot107.api.evidence_query import EvidenceQueryService
from pilot107.api.file_routes import FileRoutes
from pilot107.api.http_app import Pilot107HttpApi
from pilot107.core.file_uploads import FileUploadService
from pilot107.core.run_store import RunStore
from pilot107.worker.evidence import EvidenceStore

_CHUNK = 16


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _json(obj: object) -> bytes:
    return json.dumps(obj).encode("utf-8")


class FakeExecutor:
    """In-memory FileOpsExecutor; paths under /forbidden are rejected."""

    def __init__(self) -> None:
        self.files: dict[str, bytearray] = {}
        self.dirs: set[str] = {"/public/home/alice"}
        self.extract_calls: list[tuple[str, str]] = []

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
        return _b64(bytes(buffer[offset:offset + length])), len(buffer)

    def file_sha256(self, *, path, owner, timeout_seconds=30.0) -> str:
        self._check(path)
        return hashlib.sha256(bytes(self.files[path])).hexdigest()

    def list_dir(self, *, path, owner, timeout_seconds=30.0):
        self._check(path)
        prefix = path.rstrip("/") + "/"
        names: dict[str, str] = {}
        for dir_path in self.dirs:
            if dir_path.startswith(prefix) and dir_path != path:
                names[dir_path[len(prefix):].split("/")[0]] = "dir"
        for file_path in self.files:
            if file_path.startswith(prefix):
                names[file_path[len(prefix):].split("/")[0]] = "file"
        return [
            FileEntry(name=name, type=kind, size=0, mtime=0)
            for name, kind in sorted(names.items())
        ]

    def make_dir(self, *, path, owner, timeout_seconds=30.0) -> None:
        self._check(path)
        self.dirs.add(path)

    def remove_path(self, *, path, owner, timeout_seconds=30.0) -> None:
        self._check(path)
        self.files.pop(path, None)
        self.dirs.discard(path)

    def stat_path(self, *, path, owner, timeout_seconds=30.0) -> FileStat:
        self._check(path)
        if path in self.files:
            return FileStat(path=path, type="file", size=len(self.files[path]), mtime=0)
        return FileStat(path=path, type="dir", size=0, mtime=0)

    def extract_archive(self, *, archive_path, dest_dir, owner, timeout_seconds=120.0) -> int:
        self._check(archive_path)
        self.extract_calls.append((archive_path, dest_dir))
        return 3

    def create_archive(
        self, *, paths, dest_dir, archive_name, owner, timeout_seconds=120.0
    ):
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
            chunk_size=_CHUNK,
        )
        self.api = Pilot107HttpApi(
            store=run_store,
            evidence_query=EvidenceQueryService(
                store=run_store,
                evidence_store=EvidenceStore(root / "evidence"),
            ),
            file_routes=FileRoutes(
                upload_service=upload_service, executor=self.executor
            ),
            auth_required=True,
        )

    def tearDown(self) -> None:
        self._temporary.cleanup()

    @staticmethod
    def _headers(user: str) -> dict[str, str]:
        return {"X-Pilot107-User": user}

    def _upload(self, payload: bytes, *, filename="blob.bin", sha256=None, user="alice"):
        target = "/public/home/alice"
        created = self.api.handle_post(
            "/api/v1/files/uploads",
            body=_json(
                {
                    "target_path": target,
                    "filename": filename,
                    "total_size": len(payload),
                    "sha256": sha256,
                }
            ),
            headers=self._headers(user),
        )
        assert created.status == 201, created.payload
        upload_id = created.payload["upload_id"]
        total_chunks = created.payload["total_chunks"]
        for index in range(total_chunks):
            start = index * _CHUNK
            chunk = self.api.handle_post(
                f"/api/v1/files/uploads/{upload_id}/chunks",
                body=_json(
                    {"index": index, "data_b64": _b64(payload[start:start + _CHUNK])}
                ),
                headers=self._headers(user),
            )
            assert chunk.status == 200, chunk.payload
        return upload_id

    def test_full_upload_flow_writes_to_cluster(self) -> None:
        payload = bytes(range(256)) * 2
        digest = hashlib.sha256(payload).hexdigest()
        upload_id = self._upload(payload, sha256=digest)

        completed = self.api.handle_post(
            f"/api/v1/files/uploads/{upload_id}/complete",
            headers=self._headers("alice"),
        )

        self.assertEqual(completed.status, 200, completed.payload)
        self.assertEqual(completed.payload["state"], "written")
        self.assertEqual(completed.payload["sha256_actual"], digest)
        self.assertEqual(
            bytes(self.executor.files["/public/home/alice/blob.bin"]), payload
        )

    def test_get_session_progress_and_list(self) -> None:
        upload_id = self._upload(b"G" * _CHUNK)
        fetched = self.api.handle_get(
            f"/api/v1/files/uploads/{upload_id}", headers=self._headers("alice")
        )
        self.assertEqual(fetched.status, 200)
        self.assertEqual(fetched.payload["received_bytes"], _CHUNK)

        listed = self.api.handle_get(
            "/api/v1/files/uploads", headers=self._headers("alice")
        )
        self.assertEqual(listed.status, 200)
        self.assertEqual(len(listed.payload["items"]), 1)

    def test_sha256_mismatch_returns_409(self) -> None:
        upload_id = self._upload(b"H" * _CHUNK, sha256="0" * 64)
        completed = self.api.handle_post(
            f"/api/v1/files/uploads/{upload_id}/complete",
            headers=self._headers("alice"),
        )
        self.assertEqual(completed.status, 409)
        self.assertEqual(completed.payload["error"]["code"], "UPLOAD.SHA256_MISMATCH")

    def test_target_outside_roots_returns_403(self) -> None:
        created = self.api.handle_post(
            "/api/v1/files/uploads",
            body=_json(
                {"target_path": "/etc", "filename": "x", "total_size": _CHUNK}
            ),
            headers=self._headers("alice"),
        )
        self.assertEqual(created.status, 403)

    def test_cross_owner_session_is_not_found(self) -> None:
        upload_id = self._upload(b"I" * _CHUNK)
        fetched = self.api.handle_get(
            f"/api/v1/files/uploads/{upload_id}", headers=self._headers("bob")
        )
        self.assertEqual(fetched.status, 404)

    def test_abort_session(self) -> None:
        upload_id = self._upload(b"J" * _CHUNK)
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
        self.assertEqual(
            response.payload["path"], "/public/home/alice/bundle.tar.gz"
        )
        self.assertIn("/public/home/alice/bundle.tar.gz", self.executor.files)

    def test_archive_requires_paths(self) -> None:
        response = self.api.handle_post(
            "/api/v1/files/archive",
            body=_json({"paths": [], "dest_dir": "/public/home/alice"}),
            headers=self._headers("alice"),
        )
        self.assertEqual(response.status, 400)


if __name__ == "__main__":
    unittest.main()
