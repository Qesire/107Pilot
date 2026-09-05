"""Unit tests for the offset-based (tus) upload session service."""

from __future__ import annotations

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

_APPEND = 16  # tiny append slice to exercise multi-append offset tracking
_BLOCK = 16  # tiny staging -> cluster write block to exercise multi-block writes


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


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
            write_block_size=_BLOCK,
        )
        self.owner = "alice"
        self.target = str(self.cluster_root)

    def _upload(
        self,
        payload: bytes,
        *,
        filename: str = "blob.bin",
        sha256: str | None = None,
        auto_extract: bool = False,
        append_size: int = _APPEND,
    ):
        """Create a whole session and append the payload in small slices."""
        session = self.service.create_session(
            owner=self.owner,
            target_path=self.target,
            filename=filename,
            total_size=len(payload),
            sha256_expected=sha256,
            auto_extract=auto_extract,
        )
        offset = 0
        while offset < len(payload):
            chunk = payload[offset : offset + append_size]
            new_offset = self.service.append_bytes(
                session.upload_id, self.owner, offset, chunk
            )
            self.assertEqual(new_offset, offset + len(chunk))
            offset = new_offset
        return session

    def test_happy_path_writes_verified_file_to_cluster(self) -> None:
        payload = bytes(range(256)) * 3  # 768 bytes -> many appends and blocks
        digest = _digest(payload)
        session = self._upload(payload, sha256=digest)

        result = self.service.complete(session.upload_id, self.owner)

        self.assertEqual(result.state, UploadState.WRITTEN)
        self.assertEqual(result.sha256_actual, digest)
        written = self.cluster_root / "blob.bin"
        self.assertEqual(written.read_bytes(), payload)
        # Staging is purged after a successful completion.
        self.assertFalse((self.staging_root / self.owner / session.upload_id).exists())

    def test_append_bytes_tracks_received_offset(self) -> None:
        session = self.service.create_session(
            owner=self.owner,
            target_path=self.target,
            filename="x.bin",
            total_size=_APPEND * 2,
        )
        self.assertEqual(session.received_bytes, 0)
        self.assertEqual(session.state, UploadState.INITIALIZED)

        new_offset = self.service.append_bytes(
            session.upload_id, self.owner, 0, b"A" * _APPEND
        )
        self.assertEqual(new_offset, _APPEND)
        refreshed = self.service.get_session(session.upload_id, self.owner)
        self.assertEqual(refreshed.received_bytes, _APPEND)
        self.assertEqual(refreshed.state, UploadState.UPLOADING)

    def test_append_bytes_is_idempotent_on_retry(self) -> None:
        """Rewriting an already-received range truncates first (no corruption)."""
        payload = b"A" * _APPEND + b"B" * _APPEND
        session = self.service.create_session(
            owner=self.owner,
            target_path=self.target,
            filename="dup.bin",
            total_size=len(payload),
        )
        self.service.append_bytes(session.upload_id, self.owner, 0, b"A" * _APPEND)
        # Retry the same range, then continue; the file must not double-count.
        again = self.service.append_bytes(
            session.upload_id, self.owner, 0, b"A" * _APPEND
        )
        self.assertEqual(again, _APPEND)
        final = self.service.append_bytes(
            session.upload_id, self.owner, _APPEND, b"B" * _APPEND
        )
        self.assertEqual(final, len(payload))

        result = self.service.complete(session.upload_id, self.owner)
        self.assertEqual(result.sha256_actual, _digest(payload))
        self.assertEqual((self.cluster_root / "dup.bin").read_bytes(), payload)

    def test_offset_ahead_of_received_rejected_with_409(self) -> None:
        session = self.service.create_session(
            owner=self.owner,
            target_path=self.target,
            filename="gap.bin",
            total_size=_APPEND * 3,
        )
        self.service.append_bytes(session.upload_id, self.owner, 0, b"A" * _APPEND)
        with self.assertRaises(UploadError) as ctx:
            # Skip a range: offset 32 is ahead of the 16 bytes received.
            self.service.append_bytes(
                session.upload_id, self.owner, _APPEND * 2, b"C" * _APPEND
            )
        self.assertEqual(ctx.exception.code, "UPLOAD.OFFSET_MISMATCH")
        self.assertEqual(ctx.exception.status, 409)

    def test_sha256_mismatch_fails_and_cleans_staging(self) -> None:
        payload = b"C" * _APPEND
        session = self._upload(payload, sha256="0" * 64)
        with self.assertRaisesRegex(UploadError, "sha256 mismatch"):
            self.service.complete(session.upload_id, self.owner)
        refreshed = self.service.get_session(session.upload_id, self.owner)
        self.assertEqual(refreshed.state, UploadState.FAILED)
        self.assertFalse((self.cluster_root / "blob.bin").exists())
        self.assertFalse((self.staging_root / self.owner / session.upload_id).exists())

    def test_incomplete_upload_rejected_on_complete(self) -> None:
        session = self.service.create_session(
            owner=self.owner,
            target_path=self.target,
            filename="x.bin",
            total_size=_APPEND * 3,
        )
        self.service.append_bytes(session.upload_id, self.owner, 0, b"A" * _APPEND)
        with self.assertRaises(UploadError) as ctx:
            self.service.complete(session.upload_id, self.owner)
        self.assertEqual(ctx.exception.code, "UPLOAD.INCOMPLETE")

    def test_target_outside_owner_roots_forbidden(self) -> None:
        with self.assertRaisesRegex(UploadError, "outside allowed roots"):
            self.service.create_session(
                owner=self.owner,
                target_path="/etc",
                filename="x.bin",
                total_size=_APPEND,
            )

    def test_unsafe_filename_rejected(self) -> None:
        for bad in ("../escape", "a/b", ".", ""):
            with self.assertRaises(UploadError):
                self.service.create_session(
                    owner=self.owner,
                    target_path=self.target,
                    filename=bad,
                    total_size=_APPEND,
                )

    def test_owner_isolation(self) -> None:
        session = self._upload(b"D" * _APPEND)
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
        session = self._upload(
            payload, filename="kit.tar", sha256=_digest(payload), auto_extract=True
        )

        result = self.service.complete(session.upload_id, self.owner)

        self.assertEqual(result.state, UploadState.EXTRACTED)
        self.assertEqual(result.extracted_members, 1)
        self.assertEqual(
            (self.cluster_root / "run.sh").read_bytes(), b"#!/bin/bash\necho hi\n"
        )

    def test_abort_is_terminal(self) -> None:
        session = self._upload(b"E" * _APPEND)
        aborted = self.service.abort(session.upload_id, self.owner)
        self.assertEqual(aborted.state, UploadState.ABORTED)
        self.assertFalse((self.staging_root / self.owner / session.upload_id).exists())
        with self.assertRaisesRegex(UploadError, "aborted"):
            self.service.complete(session.upload_id, self.owner)

    def test_cleanup_expired_removes_old_sessions(self) -> None:
        session = self._upload(b"F" * _APPEND)
        future = datetime.now(UTC) + timedelta(
            seconds=self.service.session_ttl_seconds + 10
        )
        removed = self.service.cleanup_expired(now=future)
        self.assertEqual(removed, 1)
        with self.assertRaises(UploadNotFound):
            self.service.get_session(session.upload_id, self.owner)


class PartialConcatTests(unittest.TestCase):
    """tus concatenation extension: parallel partial buckets merged whole."""

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
            write_block_size=_BLOCK,
        )
        self.owner = "alice"
        self.target = str(self.cluster_root)

    def _fill(self, session, payload: bytes) -> None:
        self.service.append_bytes(session.upload_id, self.owner, 0, payload)

    def test_concatenate_merges_partials_in_order(self) -> None:
        part_a = b"A" * 100
        part_b = b"B" * 50
        part_c = b"C" * 25
        payload = part_a + part_b + part_c
        digest = _digest(payload)

        s1 = self.service.create_partial_session(owner=self.owner, total_size=len(part_a))
        s2 = self.service.create_partial_session(owner=self.owner, total_size=len(part_b))
        s3 = self.service.create_partial_session(owner=self.owner, total_size=len(part_c))
        self.assertTrue(s1.is_partial)
        self.assertEqual(s1.target_path, "")
        self._fill(s1, part_a)
        self._fill(s2, part_b)
        self._fill(s3, part_c)

        merged = self.service.concatenate(
            owner=self.owner,
            partial_ids=[s1.upload_id, s2.upload_id, s3.upload_id],
            target_path=self.target,
            filename="merged.bin",
            sha256_expected=digest,
        )

        self.assertFalse(merged.is_partial)
        self.assertEqual(merged.total_size, len(payload))
        self.assertEqual(merged.received_bytes, len(payload))
        # Partials are gone after the merge.
        for partial in (s1, s2, s3):
            with self.assertRaises(UploadNotFound):
                self.service.get_session(partial.upload_id, self.owner)

        result = self.service.complete(merged.upload_id, self.owner)
        self.assertEqual(result.state, UploadState.WRITTEN)
        self.assertEqual((self.cluster_root / "merged.bin").read_bytes(), payload)

    def test_concatenate_defaults_total_to_sum_of_parts(self) -> None:
        s1 = self.service.create_partial_session(owner=self.owner, total_size=40)
        s2 = self.service.create_partial_session(owner=self.owner, total_size=60)
        self._fill(s1, b"x" * 40)
        self._fill(s2, b"y" * 60)

        merged = self.service.concatenate(
            owner=self.owner,
            partial_ids=[s1.upload_id, s2.upload_id],
            target_path=self.target,
            filename="sum.bin",
        )
        self.assertEqual(merged.total_size, 100)

    def test_concatenate_rejects_incomplete_partial(self) -> None:
        s1 = self.service.create_partial_session(owner=self.owner, total_size=40)
        self._fill(s1, b"x" * 20)  # only half received
        with self.assertRaises(UploadError) as ctx:
            self.service.concatenate(
                owner=self.owner,
                partial_ids=[s1.upload_id],
                target_path=self.target,
                filename="x.bin",
            )
        self.assertEqual(ctx.exception.code, "UPLOAD.CONCAT_INCOMPLETE")
        self.assertEqual(ctx.exception.status, 409)

    def test_concatenate_rejects_non_partial_session(self) -> None:
        whole = self.service.create_session(
            owner=self.owner,
            target_path=self.target,
            filename="w.bin",
            total_size=16,
        )
        with self.assertRaises(UploadError) as ctx:
            self.service.concatenate(
                owner=self.owner,
                partial_ids=[whole.upload_id],
                target_path=self.target,
                filename="x.bin",
            )
        self.assertEqual(ctx.exception.code, "UPLOAD.CONCAT_INVALID")

    def test_concatenate_rejects_length_mismatch(self) -> None:
        s1 = self.service.create_partial_session(owner=self.owner, total_size=40)
        self._fill(s1, b"x" * 40)
        with self.assertRaises(UploadError) as ctx:
            self.service.concatenate(
                owner=self.owner,
                partial_ids=[s1.upload_id],
                target_path=self.target,
                filename="x.bin",
                total_size=999,  # does not match the 40 bytes of parts
            )
        self.assertEqual(ctx.exception.code, "UPLOAD.CONCAT_INVALID")

    def test_concatenate_requires_partial_owner_match(self) -> None:
        s1 = self.service.create_partial_session(owner=self.owner, total_size=40)
        self._fill(s1, b"x" * 40)
        with self.assertRaises(UploadNotFound):
            self.service.concatenate(
                owner="mallory",
                partial_ids=[s1.upload_id],
                target_path=self.target,
                filename="x.bin",
            )

    def test_complete_rejects_partial_session(self) -> None:
        s1 = self.service.create_partial_session(owner=self.owner, total_size=16)
        self._fill(s1, b"x" * 16)
        with self.assertRaises(UploadError) as ctx:
            self.service.complete(s1.upload_id, self.owner)
        self.assertEqual(ctx.exception.code, "UPLOAD.STATE")

    def test_concatenate_authorizes_target_path(self) -> None:
        s1 = self.service.create_partial_session(owner=self.owner, total_size=16)
        self._fill(s1, b"x" * 16)
        with self.assertRaises(UploadError) as ctx:
            self.service.concatenate(
                owner=self.owner,
                partial_ids=[s1.upload_id],
                target_path="/etc",
                filename="x.bin",
            )
        self.assertEqual(ctx.exception.code, "UPLOAD.PATH_FORBIDDEN")


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
            write_block_size=_BLOCK,
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
        self.assertEqual(row.received_bytes, 0)
        self.assertFalse(row.is_partial)

    def test_partial_session_persisted_with_flag(self) -> None:
        session = self.service.create_partial_session(owner=self.owner, total_size=32)
        row = self.store.get(session.upload_id)
        assert row is not None
        self.assertTrue(row.is_partial)
        self.assertEqual(row.target_path, "")

    def test_session_survives_restart(self) -> None:
        """A new service instance reloads non-terminal sessions from the store."""
        session = self.service.create_session(
            owner=self.owner, target_path=self.target,
            filename="restart.bin", total_size=16,
        )
        self.service.append_bytes(session.upload_id, self.owner, 0, b"A" * 8)
        # Simulate restart: new service with same store
        service2 = FileUploadService(
            executor=self.executor,
            owner_roots=(str(self.cluster_root),),
            staging_root=self.staging_root,
            write_block_size=_BLOCK,
            store=self.store,
        )
        reloaded = service2.get_session(session.upload_id, self.owner)
        self.assertEqual(reloaded.filename, "restart.bin")
        self.assertEqual(reloaded.state, UploadState.UPLOADING)
        self.assertEqual(reloaded.received_bytes, 8)

    def test_append_update_persisted(self) -> None:
        session = self.service.create_session(
            owner=self.owner, target_path=self.target,
            filename="chunk.bin", total_size=32,
        )
        self.service.append_bytes(session.upload_id, self.owner, 0, b"A" * _APPEND)
        row = self.store.get(session.upload_id)
        assert row is not None
        self.assertEqual(row.state, UploadState.UPLOADING)
        self.assertEqual(row.received_bytes, _APPEND)

    def test_complete_persisted(self) -> None:
        payload = b"X" * 32
        sha = _digest(payload)
        session = self.service.create_session(
            owner=self.owner, target_path=self.target,
            filename="done.bin", total_size=32, sha256_expected=sha,
        )
        self.service.append_bytes(session.upload_id, self.owner, 0, payload)
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
        future = datetime.now(UTC) + timedelta(
            seconds=self.service.session_ttl_seconds + 10
        )
        self.service.cleanup_expired(now=future)
        self.assertIsNone(self.store.get(session.upload_id))

    def test_legacy_chunk_schema_is_dropped_and_recreated(self) -> None:
        """A pre-tus database (received_chunks_json) is rebuilt offset-based."""
        legacy = Path(self._tmp.name) / "legacy.db"
        import sqlite3

        conn = sqlite3.connect(str(legacy))
        conn.execute(
            "CREATE TABLE upload_sessions ("
            "upload_id TEXT PRIMARY KEY, received_chunks_json TEXT)"
        )
        conn.commit()
        conn.close()

        store = UploadSessionStore(legacy)
        with store.connect() as conn:
            columns = {r[1] for r in conn.execute("PRAGMA table_info(upload_sessions)")}
        self.assertIn("received_bytes", columns)
        self.assertIn("is_partial", columns)
        self.assertNotIn("received_chunks_json", columns)


class UploadQuotaTests(unittest.TestCase):
    """Per-owner concurrency and byte quota enforcement."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.staging = root / "staging"
        self.target = str(root / "home" / "alice")
        Path(self.target).mkdir(parents=True)
        self.executor = LocalFileOpsExecutor(allowed_roots=[str(root / "home")])
        self.service = FileUploadService(
            executor=self.executor,
            owner_roots=(str(root / "home"),),
            staging_root=self.staging,
            max_active_per_owner=2,
            max_total_bytes_per_owner=1024,
        )

    def test_concurrent_session_limit_enforced(self) -> None:
        self.service.create_session(
            owner="alice", target_path=self.target,
            filename="a.bin", total_size=100,
        )
        self.service.create_session(
            owner="alice", target_path=self.target,
            filename="b.bin", total_size=100,
        )
        with self.assertRaises(UploadError) as ctx:
            self.service.create_session(
                owner="alice", target_path=self.target,
                filename="c.bin", total_size=100,
            )
        self.assertEqual(ctx.exception.code, "UPLOAD.QUOTA_CONCURRENT")
        self.assertEqual(ctx.exception.status, 429)

    def test_byte_quota_enforced(self) -> None:
        self.service.create_session(
            owner="alice", target_path=self.target,
            filename="big.bin", total_size=900,
        )
        with self.assertRaises(UploadError) as ctx:
            self.service.create_session(
                owner="alice", target_path=self.target,
                filename="extra.bin", total_size=200,
            )
        self.assertEqual(ctx.exception.code, "UPLOAD.QUOTA_BYTES")
        self.assertEqual(ctx.exception.status, 429)

    def test_partial_counts_bytes_but_not_active(self) -> None:
        """Parallel partial buckets fill the byte cap, not the concurrency cap."""
        self.service.create_session(
            owner="alice", target_path=self.target,
            filename="a.bin", total_size=100,
        )
        self.service.create_session(
            owner="alice", target_path=self.target,
            filename="b.bin", total_size=100,
        )
        # Two whole sessions already hold both active slots, but a partial is
        # still admitted because partials skip the concurrency cap.
        partial = self.service.create_partial_session(owner="alice", total_size=100)
        self.assertIsNotNone(partial.upload_id)
        # A third whole session is still rejected by the concurrency cap.
        with self.assertRaises(UploadError) as ctx:
            self.service.create_session(
                owner="alice", target_path=self.target,
                filename="c.bin", total_size=100,
            )
        self.assertEqual(ctx.exception.code, "UPLOAD.QUOTA_CONCURRENT")

    def test_partial_byte_quota_enforced(self) -> None:
        self.service.create_partial_session(owner="alice", total_size=900)
        with self.assertRaises(UploadError) as ctx:
            self.service.create_partial_session(owner="alice", total_size=200)
        self.assertEqual(ctx.exception.code, "UPLOAD.QUOTA_BYTES")

    def test_concatenate_excludes_partials_from_byte_quota(self) -> None:
        """Merging re-admits the parts' bytes once, not twice."""
        p1 = self.service.create_partial_session(owner="alice", total_size=400)
        p2 = self.service.create_partial_session(owner="alice", total_size=400)
        self.service.append_bytes(p1.upload_id, "alice", 0, b"x" * 400)
        self.service.append_bytes(p2.upload_id, "alice", 0, b"y" * 400)
        # Without exclusion this would be 800 (parts) + 800 (whole) > 1024.
        merged = self.service.concatenate(
            owner="alice",
            partial_ids=[p1.upload_id, p2.upload_id],
            target_path=self.target,
            filename="big.bin",
        )
        self.assertEqual(merged.total_size, 800)

    def test_terminal_sessions_free_quota(self) -> None:
        s1 = self.service.create_session(
            owner="alice", target_path=self.target,
            filename="a.bin", total_size=100,
        )
        self.service.create_session(
            owner="alice", target_path=self.target,
            filename="b.bin", total_size=100,
        )
        self.service.abort(s1.upload_id, "alice")
        # Now one slot is free
        s3 = self.service.create_session(
            owner="alice", target_path=self.target,
            filename="c.bin", total_size=100,
        )
        self.assertIsNotNone(s3.upload_id)

    def test_different_owners_have_independent_quota(self) -> None:
        bob_target = str(Path(self._tmp.name) / "home" / "bob")
        Path(bob_target).mkdir(parents=True)
        self.service.create_session(
            owner="alice", target_path=self.target,
            filename="a.bin", total_size=100,
        )
        self.service.create_session(
            owner="alice", target_path=self.target,
            filename="b.bin", total_size=100,
        )
        # Bob still has quota
        session = self.service.create_session(
            owner="bob", target_path=bob_target,
            filename="x.bin", total_size=100,
        )
        self.assertEqual(session.owner, "bob")


if __name__ == "__main__":
    unittest.main()
