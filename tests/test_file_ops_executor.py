"""Unit tests for the local file-ops executor primitives.

Covers the full round-trip (chunked write → read → sha256 → list → stat →
remove), path-authorization rejection outside ``allowed_roots``, sequential
offset semantics, and archive-extraction traversal protection.
"""

from __future__ import annotations

import base64
import hashlib
import io
import tarfile
import tempfile
import unittest
from pathlib import Path

from pilot107.adapters.slurm import (
    LocalFileOpsExecutor,
    SlurmSubmissionRejected,
    SlurmTransportError,
)


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


class LocalFileOpsExecutorTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.executor = LocalFileOpsExecutor(allowed_roots=[str(self.root)])

    def test_write_read_sha256_roundtrip(self) -> None:
        target = self.root / "blob.bin"
        payload = b"hello-" + bytes(range(256)) + b"-world"
        expected = hashlib.sha256(payload).hexdigest()

        # Write in two chunks: first truncates, second appends via offset.
        first, second = payload[:10], payload[10:]
        size = self.executor.write_bytes_chunk(
            path=str(target), data_b64=_b64(first), offset=0, owner="alice"
        )
        self.assertEqual(size, len(first))
        size = self.executor.write_bytes_chunk(
            path=str(target), data_b64=_b64(second), offset=len(first), owner="alice"
        )
        self.assertEqual(size, len(payload))

        data_b64, total = self.executor.read_bytes_chunk(
            path=str(target), offset=0, length=len(payload) + 100, owner="alice"
        )
        self.assertEqual(total, len(payload))
        self.assertEqual(base64.b64decode(data_b64), payload)

        self.assertEqual(
            self.executor.file_sha256(path=str(target), owner="alice"), expected
        )

    def test_append_offset_writes_at_end(self) -> None:
        target = self.root / "append.bin"
        self.executor.write_bytes_chunk(
            path=str(target), data_b64=_b64(b"AAA"), offset=0, owner="alice"
        )
        size = self.executor.write_bytes_chunk(
            path=str(target), data_b64=_b64(b"BBB"), offset=-1, owner="alice"
        )
        self.assertEqual(size, 6)
        self.assertEqual(target.read_bytes(), b"AAABBB")

    def test_offset_mismatch_is_rejected(self) -> None:
        target = self.root / "mismatch.bin"
        self.executor.write_bytes_chunk(
            path=str(target), data_b64=_b64(b"AAAA"), offset=0, owner="alice"
        )
        with self.assertRaisesRegex(SlurmTransportError, "does not match file size"):
            self.executor.write_bytes_chunk(
                path=str(target), data_b64=_b64(b"X"), offset=2, owner="alice"
            )

    def test_write_rejects_path_outside_allowed_roots(self) -> None:
        outside = self.root.parent / "elsewhere.bin"
        with self.assertRaisesRegex(SlurmSubmissionRejected, "outside allowed roots"):
            self.executor.write_bytes_chunk(
                path=str(outside), data_b64=_b64(b"x"), offset=0, owner="alice"
            )

    def test_list_and_stat_and_remove(self) -> None:
        (self.root / "a.txt").write_bytes(b"12345")
        (self.root / "sub").mkdir()

        entries = self.executor.list_dir(path=str(self.root), owner="alice")
        names = {entry.name: entry.type for entry in entries}
        self.assertEqual(names["a.txt"], "file")
        self.assertEqual(names["sub"], "dir")

        stat = self.executor.stat_path(path=str(self.root / "a.txt"), owner="alice")
        self.assertEqual(stat.type, "file")
        self.assertEqual(stat.size, 5)

        self.executor.remove_path(path=str(self.root / "a.txt"), owner="alice")
        self.assertFalse((self.root / "a.txt").exists())

        # Directory removal is recursive.
        (self.root / "sub" / "inner.txt").write_bytes(b"z")
        self.executor.remove_path(path=str(self.root / "sub"), owner="alice")
        self.assertFalse((self.root / "sub").exists())

    def test_stat_missing_path_raises(self) -> None:
        with self.assertRaisesRegex(SlurmTransportError, "does not exist"):
            self.executor.stat_path(path=str(self.root / "nope"), owner="alice")

    def _make_tar(self, members: dict[str, bytes]) -> Path:
        archive_path = self.root / "archive.tar"
        with tarfile.open(archive_path, "w") as tar:
            for name, data in members.items():
                info = tarfile.TarInfo(name=name)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        return archive_path

    def test_extract_valid_archive(self) -> None:
        archive = self._make_tar(
            {"run.sh": b"#!/bin/bash\n", "config/app.env": b"KEY=1\n"}
        )
        dest = self.root / "extracted"
        count = self.executor.extract_archive(
            archive_path=str(archive), dest_dir=str(dest), owner="alice"
        )
        self.assertEqual(count, 2)
        self.assertEqual((dest / "run.sh").read_bytes(), b"#!/bin/bash\n")
        self.assertEqual((dest / "config" / "app.env").read_bytes(), b"KEY=1\n")

    def test_extract_rejects_traversal_member(self) -> None:
        archive_path = self.root / "evil.tar"
        with tarfile.open(archive_path, "w") as tar:
            info = tarfile.TarInfo(name="../escape.txt")
            info.size = 3
            tar.addfile(info, io.BytesIO(b"bad"))
        dest = self.root / "dest"
        with self.assertRaisesRegex(SlurmSubmissionRejected, "escapes destination"):
            self.executor.extract_archive(
                archive_path=str(archive_path), dest_dir=str(dest), owner="alice"
            )

    def test_extract_rejects_absolute_member(self) -> None:
        archive_path = self.root / "abs.tar"
        with tarfile.open(archive_path, "w") as tar:
            info = tarfile.TarInfo(name="/etc/passwd")
            info.size = 1
            tar.addfile(info, io.BytesIO(b"x"))
        dest = self.root / "dest2"
        with self.assertRaises(SlurmSubmissionRejected):
            self.executor.extract_archive(
                archive_path=str(archive_path), dest_dir=str(dest), owner="alice"
            )

    def test_extract_rejects_symlink_member(self) -> None:
        archive_path = self.root / "link.tar"
        with tarfile.open(archive_path, "w") as tar:
            info = tarfile.TarInfo(name="evil-link")
            info.type = tarfile.SYMTYPE
            info.linkname = "/etc/shadow"
            tar.addfile(info)
        dest = self.root / "dest3"
        with self.assertRaisesRegex(SlurmSubmissionRejected, "link members"):
            self.executor.extract_archive(
                archive_path=str(archive_path), dest_dir=str(dest), owner="alice"
            )

    def test_create_archive_packs_sources(self) -> None:
        (self.root / "a.txt").write_bytes(b"1")
        (self.root / "b.txt").write_bytes(b"22")
        archive_path, size = self.executor.create_archive(
            paths=[str(self.root / "a.txt"), str(self.root / "b.txt")],
            dest_dir=str(self.root),
            archive_name="bundle.tar.gz",
            owner="alice",
        )
        self.assertEqual(archive_path, str(self.root / "bundle.tar.gz"))
        self.assertGreater(size, 0)
        with tarfile.open(archive_path, "r:gz") as tar:
            self.assertEqual(sorted(tar.getnames()), ["a.txt", "b.txt"])

    def test_create_archive_rejects_unsafe_name(self) -> None:
        (self.root / "a.txt").write_bytes(b"1")
        with self.assertRaisesRegex(SlurmSubmissionRejected, "unsafe archive name"):
            self.executor.create_archive(
                paths=[str(self.root / "a.txt")],
                dest_dir=str(self.root),
                archive_name="../escape.tar.gz",
                owner="alice",
            )


if __name__ == "__main__":
    unittest.main()
