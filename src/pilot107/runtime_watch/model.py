"""Immutable domain records for incremental Runtime Watch persistence."""

from __future__ import annotations

import base64
import binascii
import hashlib
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

RuntimeLogStream = Literal["stdout", "stderr"]
RuntimeAlertSeverity = Literal["info", "warning", "critical"]

_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_DIGEST = re.compile(r"^[a-f0-9]{64}$")
_MAX_SEGMENT_BYTES = 1024 * 1024


class RuntimeWatchState(StrEnum):
    WATCHING = "watching"
    WAITING_FOR_LOG = "waiting_for_log"
    ACTIVE = "active"
    QUIET_BACKOFF = "quiet_backoff"
    DEGRADED = "degraded"
    FINALIZING = "finalizing"
    STOPPED = "stopped"


class RuntimeWatchConflict(RuntimeError):
    """A write used a stale lease, non-monotonic cursor, or conflicting replay."""


@dataclass(frozen=True)
class RuntimeLogCursor:
    run_id: str
    owner: str
    stream: RuntimeLogStream
    generation: int
    offset: int
    source_size: int
    source_mtime: float | None
    source_file_identity: str | None
    source_prefix_fingerprint: str | None
    decoder_remainder_base64: str
    last_data_at: str | None
    last_checked_at: str | None
    quiet_polls: int
    version: int

    def __post_init__(self) -> None:
        _identifier(self.run_id, "run_id")
        _identifier(self.owner, "owner")
        if self.stream not in {"stdout", "stderr"}:
            raise ValueError("Runtime cursor stream is invalid")
        for value, label in (
            (self.generation, "generation"),
            (self.offset, "offset"),
            (self.source_size, "source_size"),
            (self.quiet_polls, "quiet_polls"),
            (self.version, "version"),
        ):
            _non_negative_integer(value, label)
        if self.quiet_polls > 1_048_576:
            raise ValueError("quiet_polls exceeds the Runtime cursor limit")
        if self.source_mtime is not None and (
            isinstance(self.source_mtime, bool)
            or not isinstance(self.source_mtime, (int, float))
            or not math.isfinite(self.source_mtime)
            or self.source_mtime < 0
        ):
            raise ValueError("source_mtime is invalid")
        if self.source_size < self.offset:
            raise ValueError("source_size cannot be behind the Runtime cursor offset")
        if self.source_file_identity is not None:
            _bounded_text(
                self.source_file_identity,
                "source_file_identity",
                maximum=256,
            )
        if self.source_prefix_fingerprint is not None:
            _digest(self.source_prefix_fingerprint, "source_prefix_fingerprint")
        try:
            remainder = base64.b64decode(
                self.decoder_remainder_base64,
                validate=True,
            )
        except (binascii.Error, ValueError) as exc:
            raise ValueError("decoder_remainder_base64 is invalid") from exc
        if len(self.decoder_remainder_base64) > 32 or len(remainder) > 8:
            raise ValueError("decoder remainder exceeds the Runtime cursor limit")
        for timestamp_value, timestamp_label in (
            (self.last_data_at, "last_data_at"),
            (self.last_checked_at, "last_checked_at"),
        ):
            if timestamp_value is not None:
                _timestamp(timestamp_value, timestamp_label)

    @classmethod
    def initial(
        cls,
        *,
        run_id: str,
        owner: str,
        stream: RuntimeLogStream,
    ) -> RuntimeLogCursor:
        return cls(
            run_id=run_id,
            owner=owner,
            stream=stream,
            generation=0,
            offset=0,
            source_size=0,
            source_mtime=None,
            source_file_identity=None,
            source_prefix_fingerprint=None,
            decoder_remainder_base64="",
            last_data_at=None,
            last_checked_at=None,
            quiet_polls=0,
            version=0,
        )


@dataclass(frozen=True)
class RuntimeWatchRecord:
    watch_id: str
    run_id: str
    owner: str
    connection_id: str
    state: RuntimeWatchState
    version: int
    next_poll_at: str | None
    lease_owner: str | None
    lease_expires_at: str | None
    fencing_token: int
    cursors: tuple[RuntimeLogCursor, ...]
    created_at: str
    updated_at: str
    stopped_at: str | None
    last_error_code: str | None
    last_error_at: str | None
    schema_version: str = "pilot107.runtime-watch/v1"

    def __post_init__(self) -> None:
        for value, label in (
            (self.watch_id, "watch_id"),
            (self.run_id, "run_id"),
            (self.owner, "owner"),
            (self.connection_id, "connection_id"),
        ):
            _identifier(value, label)
        if not isinstance(self.state, RuntimeWatchState):
            raise TypeError("Runtime Watch state is invalid")
        _non_negative_integer(self.version, "version")
        _non_negative_integer(self.fencing_token, "fencing_token")
        if (self.lease_owner is None) != (self.lease_expires_at is None):
            raise ValueError("Runtime Watch lease fields must be paired")
        if self.lease_owner is not None:
            _identifier(self.lease_owner, "lease_owner")
        for timestamp_value, timestamp_label in (
            (self.next_poll_at, "next_poll_at"),
            (self.lease_expires_at, "lease_expires_at"),
            (self.stopped_at, "stopped_at"),
            (self.last_error_at, "last_error_at"),
        ):
            if timestamp_value is not None:
                _timestamp(timestamp_value, timestamp_label)
        if self.last_error_code is not None:
            _identifier(self.last_error_code, "last_error_code")
        _timestamp(self.created_at, "created_at")
        _timestamp(self.updated_at, "updated_at")
        cursors = tuple(self.cursors)
        if [item.stream for item in cursors] != ["stdout", "stderr"]:
            raise ValueError("Runtime Watch must contain stdout and stderr cursors")
        if any(item.run_id != self.run_id or item.owner != self.owner for item in cursors):
            raise ValueError("Runtime Watch cursor binding is invalid")
        object.__setattr__(self, "cursors", cursors)


@dataclass(frozen=True)
class RuntimeWatchLease:
    watch_id: str
    run_id: str
    owner: str
    worker_id: str
    version: int
    fencing_token: int
    expires_at: str

    def __post_init__(self) -> None:
        for value, label in (
            (self.watch_id, "watch_id"),
            (self.run_id, "run_id"),
            (self.owner, "owner"),
            (self.worker_id, "worker_id"),
        ):
            _identifier(value, label)
        _non_negative_integer(self.version, "version")
        if not isinstance(self.fencing_token, int) or self.fencing_token < 1:
            raise ValueError("fencing_token must be positive")
        _timestamp(self.expires_at, "expires_at")


@dataclass(frozen=True)
class RuntimeLogSegmentDraft:
    run_id: str
    owner: str
    stream: RuntimeLogStream
    generation: int
    start_offset: int
    content: bytes

    def __post_init__(self) -> None:
        _identifier(self.run_id, "run_id")
        _identifier(self.owner, "owner")
        if self.stream not in {"stdout", "stderr"}:
            raise ValueError("Runtime segment stream is invalid")
        _non_negative_integer(self.generation, "generation")
        _non_negative_integer(self.start_offset, "start_offset")
        if not isinstance(self.content, bytes) or not self.content:
            raise ValueError("Runtime segment content must be non-empty bytes")
        if len(self.content) > _MAX_SEGMENT_BYTES:
            raise ValueError("Runtime segment content exceeds the size limit")

    @property
    def end_offset(self) -> int:
        return self.start_offset + len(self.content)

    @property
    def content_sha256(self) -> str:
        return hashlib.sha256(self.content).hexdigest()

    @property
    def segment_id(self) -> str:
        payload = b"\0".join(
            (
                self.run_id.encode(),
                self.stream.encode(),
                str(self.generation).encode(),
                str(self.start_offset).encode(),
                self.content_sha256.encode(),
            )
        )
        return f"segment-{hashlib.sha256(payload).hexdigest()}"


@dataclass(frozen=True)
class RuntimeLogSegment:
    segment_id: str
    watch_id: str
    run_id: str
    owner: str
    stream: RuntimeLogStream
    generation: int
    start_offset: int
    end_offset: int
    content_sha256: str
    content_size: int
    content_ref: str
    created_at: str
    schema_version: str = "pilot107.runtime-log-segment/v1"

    def __post_init__(self) -> None:
        for value, label in (
            (self.segment_id, "segment_id"),
            (self.watch_id, "watch_id"),
            (self.run_id, "run_id"),
            (self.owner, "owner"),
        ):
            _identifier(value, label)
        if self.stream not in {"stdout", "stderr"}:
            raise ValueError("Runtime segment stream is invalid")
        for numeric_value, numeric_label in (
            (self.generation, "generation"),
            (self.start_offset, "start_offset"),
            (self.end_offset, "end_offset"),
            (self.content_size, "content_size"),
        ):
            _non_negative_integer(numeric_value, numeric_label)
        if self.end_offset <= self.start_offset:
            raise ValueError("Runtime segment offsets are invalid")
        if self.content_size != self.end_offset - self.start_offset:
            raise ValueError("Runtime segment size does not match its offsets")
        _digest(self.content_sha256, "content_sha256")
        if self.content_ref != f"sha256:{self.content_sha256}":
            raise ValueError("Runtime segment content_ref is invalid")
        _timestamp(self.created_at, "created_at")


@dataclass(frozen=True)
class RuntimeAlert:
    alert_id: str
    watch_id: str
    run_id: str
    owner: str
    code: str
    severity: RuntimeAlertSeverity
    summary: str
    segment_id: str | None
    generation: int
    offset: int
    created_at: str
    schema_version: str = "pilot107.runtime-alert/v1"

    def __post_init__(self) -> None:
        for value, label in (
            (self.alert_id, "alert_id"),
            (self.watch_id, "watch_id"),
            (self.run_id, "run_id"),
            (self.owner, "owner"),
            (self.code, "code"),
        ):
            _identifier(value, label)
        if self.severity not in {"info", "warning", "critical"}:
            raise ValueError("Runtime alert severity is invalid")
        _bounded_text(self.summary, "summary", maximum=4096)
        if self.segment_id is not None:
            _identifier(self.segment_id, "segment_id")
        _non_negative_integer(self.generation, "generation")
        _non_negative_integer(self.offset, "offset")
        _timestamp(self.created_at, "created_at")

    @classmethod
    def create(
        cls,
        *,
        watch_id: str,
        run_id: str,
        owner: str,
        code: str,
        severity: RuntimeAlertSeverity,
        summary: str,
        segment_id: str | None,
        generation: int,
        offset: int,
        created_at: str,
    ) -> RuntimeAlert:
        payload = b"\0".join(
            (
                watch_id.encode(),
                code.encode(),
                str(generation).encode(),
                str(offset).encode(),
                (segment_id or "").encode(),
            )
        )
        return cls(
            alert_id=f"alert-{hashlib.sha256(payload).hexdigest()}",
            watch_id=watch_id,
            run_id=run_id,
            owner=owner,
            code=code,
            severity=severity,
            summary=summary,
            segment_id=segment_id,
            generation=generation,
            offset=offset,
            created_at=created_at,
        )


def timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("Runtime Watch clock must be timezone-aware")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_timestamp(value: str) -> datetime:
    _timestamp(value, "timestamp")
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def runtime_watch_payload(record: RuntimeWatchRecord) -> dict[str, Any]:
    if not isinstance(record, RuntimeWatchRecord):
        raise TypeError("record must be RuntimeWatchRecord")
    return {
        "schema_version": record.schema_version,
        "watch_id": record.watch_id,
        "run_id": record.run_id,
        "owner": record.owner,
        "connection_id": record.connection_id,
        "state": record.state.value,
        "version": record.version,
        "next_poll_at": record.next_poll_at,
        "lease_owner": record.lease_owner,
        "lease_expires_at": record.lease_expires_at,
        "fencing_token": record.fencing_token,
        "cursors": [_cursor_payload(item) for item in record.cursors],
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "stopped_at": record.stopped_at,
        "last_error_code": record.last_error_code,
        "last_error_at": record.last_error_at,
    }


def _cursor_payload(cursor: RuntimeLogCursor) -> dict[str, Any]:
    return {
        "stream": cursor.stream,
        "generation": cursor.generation,
        "offset": cursor.offset,
        "source_size": cursor.source_size,
        "source_mtime": cursor.source_mtime,
        "source_file_identity": cursor.source_file_identity,
        "source_prefix_fingerprint": cursor.source_prefix_fingerprint,
        "decoder_remainder_base64": cursor.decoder_remainder_base64,
        "last_data_at": cursor.last_data_at,
        "last_checked_at": cursor.last_checked_at,
        "quiet_polls": cursor.quiet_polls,
        "version": cursor.version,
    }


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise ValueError(f"{label} is invalid")
    return value


def _digest(value: object, label: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise ValueError(f"{label} is invalid")
    return value


def _non_negative_integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def _bounded_text(value: object, label: str, *, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum or "\0" in value:
        raise ValueError(f"{label} is invalid")
    return value


def _timestamp(value: object, label: str) -> str:
    if not isinstance(value, str) or not 20 <= len(value) <= 64:
        raise ValueError(f"{label} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} is invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{label} must include a timezone")
    return value
