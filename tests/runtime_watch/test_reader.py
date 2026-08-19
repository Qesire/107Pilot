from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from pilot107.core.identity import UserIdentity
from pilot107.core.paths import SafePath
from pilot107.runtime_watch.model import RuntimeLogCursor
from pilot107.runtime_watch.reader import IncrementalLogReader, RuntimeLogSource
from pilot107.worker.evidence import FileStat


class MemoryTransport:
    def __init__(self, content: bytes, *, identity: str = "dev:1") -> None:
        self.content = content
        self.identity = identity

    def stat(self, identity: UserIdentity, path: SafePath) -> FileStat:
        return FileStat(
            path=str(path.resolved),
            kind="regular file",
            size_bytes=len(self.content),
            mtime_epoch=1.0,
            owner_readable=True,
            file_identity=self.identity,
            prefix_sha256=hashlib.sha256(self.content[:4096]).hexdigest(),
        )

    def read_bytes_range(
        self,
        identity: UserIdentity,
        path: SafePath,
        offset: int,
        length: int,
    ) -> bytes:
        return self.content[offset : offset + length]


def _source() -> RuntimeLogSource:
    root = Path("/logs")
    return RuntimeLogSource(
        run_id="run1",
        owner="alice",
        stdout_path=SafePath(original="/logs/out", resolved=root / "out", root=root),
        stderr_path=SafePath(original="/logs/err", resolved=root / "err", root=root),
    )


def _reader(transport: MemoryTransport) -> IncrementalLogReader:
    return IncrementalLogReader(
        transport=transport,
        clock=lambda: datetime(2026, 8, 19, tzinfo=UTC),
    )


def test_reader_preserves_split_utf8_character() -> None:
    transport = MemoryTransport("中".encode())
    reader = _reader(transport)
    cursor = RuntimeLogCursor.initial(run_id="run1", owner="alice", stream="stdout")

    first = reader.read_next(_source(), "stdout", cursor, max_bytes=2)
    second = reader.read_next(_source(), "stdout", first.next_cursor, max_bytes=2)

    assert first.text + second.text == "中"
    assert first.content == "中".encode()[:2]
    assert second.content == "中".encode()[2:]
    assert second.next_cursor.offset == 3


def test_append_to_short_file_does_not_look_like_rotation() -> None:
    transport = MemoryTransport(b"first\n")
    reader = _reader(transport)
    initial = RuntimeLogCursor.initial(run_id="run1", owner="alice", stream="stdout")
    first = reader.read_next(_source(), "stdout", initial, max_bytes=32)
    transport.content += b"second\n"

    second = reader.read_next(_source(), "stdout", first.next_cursor, max_bytes=32)

    assert not second.rotated
    assert second.content == b"second\n"
    assert second.next_cursor.generation == 0


def test_replacement_and_copytruncate_start_a_new_generation() -> None:
    transport = MemoryTransport(b"old log\n")
    reader = _reader(transport)
    initial = RuntimeLogCursor.initial(run_id="run1", owner="alice", stream="stdout")
    first = reader.read_next(_source(), "stdout", initial, max_bytes=32)
    transport.content = b"new\n"
    transport.identity = "dev:2"

    replaced = reader.read_next(_source(), "stdout", first.next_cursor, max_bytes=32)

    assert replaced.rotated
    assert replaced.content == b"new\n"
    assert replaced.next_cursor.generation == 1
    assert replaced.next_cursor.offset == 4


def test_reader_rejects_ranges_above_stream_limit() -> None:
    reader = _reader(MemoryTransport(b""))
    cursor = RuntimeLogCursor.initial(run_id="run1", owner="alice", stream="stdout")

    with pytest.raises(ValueError, match="256 KiB"):
        reader.read_next(_source(), "stdout", cursor, max_bytes=256 * 1024 + 1)
