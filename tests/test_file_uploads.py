"""Unit tests for the chunked upload session service."""

from __future__ import annotations

import base64
import hashlib
import io
import tarfile
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pilot107.adapters.slurm import LocalFileOpsExecutor
from pilot107.core.file_uploads import (
    FileUploadService,
    UploadError,
    UploadNotFound,
    UploadSessionStore,
    UploadState,
)

_CHUNK = 16  # tiny chunk size to exercise multi-chunk assembly


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


class FileUploadServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.cluster_root = base / "cluster" / "alice"
        self.cluster_root.mkdir(parents=True)
        self.staging_root = base / "staging"
        self.executor = LocalFileOpsExecutor(allowed_roots=[str(self.cluster_root)])
        self.service = FileUploadService(
            executor=self.executor,
            owner_roots=(str(self.cluster_root),),
            staging_root=self.staging_root,
            chunk_size=_CHUNK,
        )
        self.owner = "alice"
        self.target = str(self.cluster_root)

    def _chunked_upload(
        self, payload: bytes, *, filename: str = "blob.bin", sha256: str | None = None,
        auto_extract: bool = False,
    ):
        session = self.service.create_session(
            owner=self.owner,
            target_path=self.target,
            filename=filename,
            total_size=len(payload),
            sha256_expected=sha256,
            auto_extract=auto_extract,
        )
        for index in range(session.total_chunks):
            start = index * _CHUNK
            self.service.put_chunk(
                session.upload_id, self.owner, index, _b64(payload[start:start + _CHUNK])
            )
        return session

    def test_happy_path_writes_verified_file_to_cluster(self) -> None:
        payload = bytes(range(256)) * 3  # 768 bytes → many chunks
        digest = hashlib.sha256(payload).hexdigest()
        session = self._chunked_upload(payload, sha256=digest)

        result = self.service.complete(session.upload_id, self.owner)

        self.assertEqual(result.state, UploadState.WRITTEN)
        self.assertEqual(result.sha256_actual, digest)
        written = self.cluster_root / "blob.bin"
        self.assertEqual(written.read_bytes(), payload)
        # Staging is purged after a successful completion.
        self.assertFalse((self.staging_root / self.owner / session.upload_id).exists())

    def test_put_chunk_is_idempotent(self) -> None:
        payload = b"A" * _CHUNK + b"B" * _CHUNK
        session = self.service.create_session(
            owner=self.owner,
            target_path=self.target,
            filename="dup.bin",
            total_size=len(payload),
        )
        self.service.put_chunk(session.upload_id, self.owner, 0, _b64(b"A" * _CHUNK))
        again = self.service.put_chunk(
            session.upload_id, self.owner, 0, _b64(b"A" * _CHUNK)
        )
        self.assertEqual(again.received_bytes, _CHUNK)  # not double counted

    def test_chunk_index_out_of_range_rejected(self) -> None:
        session = self.service.create_session(
            owner=self.owner,
            target_path=self.target,
            filename="x.bin",
            total_size=_CHUNK,
        )
        with self.assertRaisesRegex(UploadError, "out of range"):
            self.service.put_chunk(session.upload_id, self.owner, 5, _b64(b"z" * _CHUNK))

    def test_wrong_chunk_length_rejected(self) -> None:
        session = self.service.create_session(
            owner=self.owner,
            target_path=self.target,
            filename="x.bin",
            total_size=_CHUNK * 2,
        )
        with self.assertRaisesRegex(UploadError, "must be"):
            self.service.put_chunk(session.upload_id, self.owner, 0, _b64(b"short"))

    def test_invalid_base64_rejected(self) -> None:
        session = self.service.create_session(
            owner=self.owner,
            target_path=self.target,
            filename="x.bin",
            total_size=_CHUNK,
        )
        with self.assertRaisesRegex(UploadError, "base64"):
            self.service.put_chunk(session.upload_id, self.owner, 0, "!!!not-b64!!!")

    def test_sha256_mismatch_fails_and_cleans_staging(self) -> None:
        payload = b"C" * _CHUNK
        session = self._chunked_upload(payload, sha256="0" * 64)
        with self.assertRaisesRegex(UploadError, "sha256 mismatch"):
            self.service.complete(session.upload_id, self.owner)
        refreshed = self.service.get_session(session.upload_id, self.owner)
        self.assertEqual(refreshed.state, UploadState.FAILED)
        self.assertFalse((self.cluster_root / "blob.bin").exists())

    def test_missing_chunks_rejected_on_complete(self) -> None:
        session = self.service.create_session(
            owner=self.owner,
            target_path=self.target,
            filename="x.bin",
            total_size=_CHUNK * 3,
        )
        self.service.put_chunk(session.upload_id, self.owner, 0, _b64(b"A" * _CHUNK))
        with self.assertRaisesRegex(UploadError, "missing"):
            self.service.complete(session.upload_id, self.owner)

    def test_target_outside_owner_roots_forbidden(self) -> None:
        with self.assertRaisesRegex(UploadError, "outside allowed roots"):
            self.service.create_session(
                owner=self.owner,
                target_path="/etc",
                filename="x.bin",
                total_size=_CHUNK,
            )

    def test_unsafe_filename_rejected(self) -> None:
        for bad in ("../escape", "a/b", ".", ""):
            with self.assertRaises(UploadError):
                self.service.create_session(
                    owner=self.owner,
                    target_path=self.target,
                    filename=bad,
                    total_size=_CHUNK,
                )

    def test_owner_isolation(self) -> None:
        session = self._chunked_upload(b"D" * _CHUNK)
        with self.assertRaises(UploadNotFound):
            self.service.get_session(session.upload_id, "mallory")

    def test_auto_extract_writes_and_extracts(self) -> None:
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as tar:
            data = b"#!/bin/bash\necho hi\n"
            info = tarfile.TarInfo(name="run.sh")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        payload = buffer.getvalue()
        digest = hashlib.sha256(payload).hexdigest()
        session = self._chunked_upload(
            payload, filename="kit.tar", sha256=digest, auto_extract=True
        )

        result = self.service.complete(session.upload_id, self.owner)

        self.assertEqual(result.state, UploadState.EXTRACTED)
        self.assertEqual(result.extracted_members, 1)
        self.assertEqual((self.cluster_root / "run.sh").read_bytes(), b"#!/bin/bash\necho hi\n")

    def test_abort_is_terminal(self) -> None:
        session = self._chunked_upload(b"E" * _CHUNK)
        aborted = self.service.abort(session.upload_id, self.owner)
        self.assertEqual(aborted.state, UploadState.ABORTED)
        with self.assertRaisesRegex(UploadError, "aborted"):
            self.service.complete(session.upload_id, self.owner)

    def test_cleanup_expired_removes_old_sessions(self) -> None:
        session = self._chunked_upload(b"F" * _CHUNK)
        future = datetime.now(UTC) + timedelta(seconds=self.service.session_ttl_seconds + 10)
        removed = self.service.cleanup_expired(now=future)
        self.assertEqual(removed, 1)
        with self.assertRaises(UploadNotFound):
            self.service.get_session(session.upload_id, self.owner)


class UploadSessionStoreTests(unittest.TestCase):
    """SQLite store persistence for upload sessions."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.cluster_root = base / "cluster" / "alice"
        self.cluster_root.mkdir(parents=True)
        self.staging_root = base / "staging"
        self.db_path = base / "test.db"
        self.store = UploadSessionStore(self.db_path)
        self.executor = LocalFileOpsExecutor(allowed_roots=[str(self.cluster_root)])
        self.service = FileUploadService(
            executor=self.executor,
            owner_roots=(str(self.cluster_root),),
            staging_root=self.staging_root,
            chunk_size=_CHUNK,
            store=self.store,
        )
        self.owner = "alice"
        self.target = str(self.cluster_root)

    def test_session_persisted_on_create(self) -> None:
        session = self.service.create_session(
            owner=self.owner, target_path=self.target,
            filename="test.bin", total_size=32,
        )
        row = self.store.get(session.upload_id)
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(row.owner, self.owner)
        self.assertEqual(row.state, UploadState.INITIALIZED)

    def test_session_survives_restart(self) -> None:
        """A new service instance reloads non-terminal sessions from the store."""
        session = self.service.create_session(
            owner=self.owner, target_path=self.target,
            filename="restart.bin", total_size=16,
        )
        # Simulate restart: new service with same store
        service2 = FileUploadService(
            executor=self.executor,
            owner_roots=(str(self.cluster_root),),
            staging_root=self.staging_root,
            chunk_size=_CHUNK,
            store=self.store,
        )
        reloaded = service2.get_session(session.upload_id, self.owner)
        self.assertEqual(reloaded.filename, "restart.bin")
        self.assertEqual(reloaded.state, UploadState.INITIALIZED)

    def test_chunk_update_persisted(self) -> None:
        session = self.service.create_session(
            owner=self.owner, target_path=self.target,
            filename="chunk.bin", total_size=32,
        )
        self.service.put_chunk(session.upload_id, self.owner, 0, _b64(b"A" * _CHUNK))
        row = self.store.get(session.upload_id)
        assert row is not None
        self.assertEqual(row.state, UploadState.UPLOADING)
        self.assertIn(0, row.received_chunks)

    def test_complete_persisted(self) -> None:
        payload = b"X" * 32
        sha = hashlib.sha256(payload).hexdigest()
        session = self.service.create_session(
            owner=self.owner, target_path=self.target,
            filename="done.bin", total_size=32, sha256_expected=sha,
        )
        for i in range(2):
            self.service.put_chunk(
                session.upload_id, self.owner, i,
                _b64(payload[i * _CHUNK:(i + 1) * _CHUNK]),
            )
        self.service.complete(session.upload_id, self.owner)
        row = self.store.get(session.upload_id)
        assert row is not None
        self.assertEqual(row.state, UploadState.WRITTEN)
        self.assertEqual(row.sha256_actual, sha)

    def test_cleanup_removes_from_store(self) -> None:
        session = self.service.create_session(
            owner=self.owner, target_path=self.target,
            filename="expire.bin", total_size=16,
        )
        future = datetime.now(UTC) + timedelta(seconds=self.service.session_ttl_seconds + 10)
        self.service.cleanup_expired(now=future)
        self.assertIsNone(self.store.get(session.upload_id))


if __name__ == "__main__":
    unittest.main()
