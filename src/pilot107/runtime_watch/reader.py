"""Generation-aware, bounded incremental reads for Runtime Watch logs."""

from __future__ import annotations

import base64
import codecs
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from pilot107.core.identity import UserIdentity
from pilot107.core.paths import SafePath
from pilot107.runtime_watch.model import RuntimeLogCursor, RuntimeLogStream, timestamp
from pilot107.worker.evidence import EvidenceTransport

MAX_STREAM_READ_BYTES = 256 * 1024
FINGERPRINT_BYTES = 4096


@dataclass(frozen=True)
class RuntimeLogSource:
    run_id: str
    owner: str
    stdout_path: SafePath
    stderr_path: SafePath

    def path_for(self, stream: RuntimeLogStream) -> SafePath:
        return self.stdout_path if stream == "stdout" else self.stderr_path


@dataclass(frozen=True)
class IncrementalLogRead:
    content: bytes
    text: str
    next_cursor: RuntimeLogCursor
    rotated: bool
    available: bool


class IncrementalLogReader:
    def __init__(
        self,
        *,
        transport: EvidenceTransport,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.transport = transport
        self._clock = clock or (lambda: datetime.now(UTC))

    def read_next(
        self,
        source: RuntimeLogSource,
        stream: RuntimeLogStream,
        cursor: RuntimeLogCursor,
        *,
        max_bytes: int,
    ) -> IncrementalLogRead:
        if not 1 <= max_bytes <= MAX_STREAM_READ_BYTES:
            raise ValueError("max_bytes must be between 1 byte and 256 KiB")
        if (
            cursor.run_id != source.run_id
            or cursor.owner != source.owner
            or cursor.stream != stream
        ):
            raise ValueError("Runtime log cursor does not match its source")
        now = timestamp(self._clock())
        identity = UserIdentity(username=source.owner)
        try:
            stat = self.transport.stat(identity, source.path_for(stream))
        except FileNotFoundError:
            return IncrementalLogRead(
                content=b"",
                text="",
                next_cursor=replace(
                    cursor,
                    last_checked_at=now,
                    quiet_polls=cursor.quiet_polls + 1,
                    version=cursor.version + 1,
                ),
                rotated=False,
                available=False,
            )
        if stat.kind != "regular file" or not stat.owner_readable:
            raise RuntimeError("Runtime log source is not a readable regular file")

        rotated = _is_rotated(cursor, stat.file_identity, stat.prefix_sha256, stat.size_bytes)
        generation = cursor.generation + 1 if rotated else cursor.generation
        offset = 0 if rotated else cursor.offset
        prior_remainder = b"" if rotated else base64.b64decode(cursor.decoder_remainder_base64)
        available = max(0, stat.size_bytes - offset)
        content = (
            self.transport.read_bytes_range(
                identity,
                source.path_for(stream),
                offset,
                min(max_bytes, available),
            )
            if available
            else b""
        )
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        decoder.setstate((prior_remainder, 0))
        text = decoder.decode(content, final=False)
        remainder, _ = decoder.getstate()
        has_data = bool(content)
        return IncrementalLogRead(
            content=content,
            text=text,
            next_cursor=replace(
                cursor,
                generation=generation,
                offset=offset + len(content),
                source_size=stat.size_bytes,
                source_mtime=stat.mtime_epoch,
                source_file_identity=stat.file_identity,
                source_prefix_fingerprint=stat.prefix_sha256,
                decoder_remainder_base64=base64.b64encode(remainder).decode("ascii"),
                last_data_at=now if has_data else cursor.last_data_at,
                last_checked_at=now,
                quiet_polls=0 if has_data else cursor.quiet_polls + 1,
                version=cursor.version + 1,
            ),
            rotated=rotated,
            available=True,
        )


def _is_rotated(
    cursor: RuntimeLogCursor,
    identity: str | None,
    prefix: str | None,
    size: int,
) -> bool:
    if cursor.source_file_identity is not None and identity != cursor.source_file_identity:
        return True
    if size < cursor.offset:
        return True
    return bool(
        cursor.source_prefix_fingerprint is not None
        and prefix is not None
        and prefix != cursor.source_prefix_fingerprint
        and (cursor.offset >= FINGERPRINT_BYTES or size <= cursor.source_size)
    )
