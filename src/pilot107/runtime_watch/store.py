"""SQLite Runtime Watch store with fenced cursor commits and segment CAS."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import tempfile
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from pilot107.core.schema_migrations import SchemaMigration, apply_schema_migrations
from pilot107.runtime_watch.model import (
    RuntimeAlert,
    RuntimeLogCursor,
    RuntimeLogSegment,
    RuntimeLogSegmentDraft,
    RuntimeLogStream,
    RuntimeWatchConflict,
    RuntimeWatchLease,
    RuntimeWatchRecord,
    RuntimeWatchState,
    parse_timestamp,
    timestamp,
)

RUNTIME_WATCH_MIGRATIONS = (
    SchemaMigration(
        migration_id="007a.001.runtime_watches",
        statements=(
            """
            CREATE TABLE runtime_watches (
                watch_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                owner TEXT NOT NULL,
                connection_id TEXT NOT NULL,
                state TEXT NOT NULL,
                version INTEGER NOT NULL,
                next_poll_at TEXT,
                lease_owner TEXT,
                lease_expires_at TEXT,
                fencing_token INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                stopped_at TEXT,
                last_error_code TEXT,
                last_error_at TEXT,
                UNIQUE (owner, run_id),
                CHECK (state IN (
                    'watching', 'waiting_for_log', 'active', 'quiet_backoff',
                    'degraded', 'finalizing', 'stopped'
                )),
                CHECK (version >= 0),
                CHECK (fencing_token >= 0),
                CHECK ((lease_owner IS NULL) = (lease_expires_at IS NULL))
            )
            """,
            """
            CREATE INDEX idx_runtime_watches_due
            ON runtime_watches(state, next_poll_at, lease_expires_at, watch_id)
            """,
        ),
    ),
    SchemaMigration(
        migration_id="007a.002.runtime_log_cursors",
        statements=(
            """
            CREATE TABLE runtime_log_cursors (
                watch_id TEXT NOT NULL REFERENCES runtime_watches(watch_id) ON DELETE CASCADE,
                run_id TEXT NOT NULL,
                owner TEXT NOT NULL,
                stream TEXT NOT NULL,
                generation INTEGER NOT NULL,
                offset_value INTEGER NOT NULL,
                source_size INTEGER NOT NULL,
                source_mtime REAL,
                source_file_identity TEXT,
                source_prefix_fingerprint TEXT,
                decoder_remainder_base64 TEXT NOT NULL,
                last_data_at TEXT,
                last_checked_at TEXT,
                quiet_polls INTEGER NOT NULL,
                version INTEGER NOT NULL,
                PRIMARY KEY (watch_id, stream),
                UNIQUE (owner, run_id, stream),
                CHECK (stream IN ('stdout', 'stderr')),
                CHECK (generation >= 0 AND offset_value >= 0 AND source_size >= 0),
                CHECK (quiet_polls >= 0 AND version >= 0)
            )
            """,
        ),
    ),
    SchemaMigration(
        migration_id="007a.003.runtime_log_segments",
        statements=(
            """
            CREATE TABLE runtime_log_segments (
                segment_id TEXT PRIMARY KEY,
                watch_id TEXT NOT NULL REFERENCES runtime_watches(watch_id) ON DELETE CASCADE,
                run_id TEXT NOT NULL,
                owner TEXT NOT NULL,
                stream TEXT NOT NULL,
                generation INTEGER NOT NULL,
                start_offset INTEGER NOT NULL,
                end_offset INTEGER NOT NULL,
                content_sha256 TEXT NOT NULL,
                content_size INTEGER NOT NULL,
                content_ref TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE (owner, run_id, stream, generation, start_offset),
                CHECK (stream IN ('stdout', 'stderr')),
                CHECK (generation >= 0 AND start_offset >= 0),
                CHECK (end_offset > start_offset),
                CHECK (content_size = end_offset - start_offset)
            )
            """,
            """
            CREATE INDEX idx_runtime_log_segments_run_stream
            ON runtime_log_segments(owner, run_id, stream, generation, start_offset)
            """,
        ),
    ),
    SchemaMigration(
        migration_id="007a.004.runtime_alerts",
        statements=(
            """
            CREATE TABLE runtime_alerts (
                alert_id TEXT PRIMARY KEY,
                watch_id TEXT NOT NULL REFERENCES runtime_watches(watch_id) ON DELETE CASCADE,
                run_id TEXT NOT NULL,
                owner TEXT NOT NULL,
                code TEXT NOT NULL,
                severity TEXT NOT NULL,
                summary TEXT NOT NULL,
                segment_id TEXT REFERENCES runtime_log_segments(segment_id),
                generation INTEGER NOT NULL,
                offset_value INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                CHECK (severity IN ('info', 'warning', 'critical')),
                CHECK (generation >= 0 AND offset_value >= 0)
            )
            """,
            """
            CREATE INDEX idx_runtime_alerts_run_created
            ON runtime_alerts(owner, run_id, created_at, alert_id)
            """,
        ),
    ),
    SchemaMigration(
        migration_id="007a.005.runtime_terminal_handoff",
        statements=(
            "ALTER TABLE runtime_watches ADD COLUMN terminal_handoff_at TEXT",
            "CREATE INDEX idx_runtime_watches_terminal_handoff "
            "ON runtime_watches(state, terminal_handoff_at, stopped_at)",
        ),
    ),
)


class RuntimeWatchStore(Protocol):
    def create_watch(
        self, *, run_id: str, owner: str, connection_id: str
    ) -> RuntimeWatchRecord: ...

    def get_watch(self, watch_id: str, *, owner: str) -> RuntimeWatchRecord: ...

    def get_watch_for_run(self, run_id: str, *, owner: str) -> RuntimeWatchRecord: ...

    def get_cursor(self, run_id: str, owner: str, stream: RuntimeLogStream) -> RuntimeLogCursor: ...

    def claim_watch(
        self,
        watch_id: str,
        *,
        owner: str,
        worker_id: str,
        lease_seconds: int,
    ) -> RuntimeWatchLease | None: ...

    def list_due_watches(self, *, limit: int = 100) -> list[RuntimeWatchRecord]: ...

    def list_stopped_watches(self, *, limit: int = 100) -> list[RuntimeWatchRecord]: ...

    def renew_watch(self, lease: RuntimeWatchLease, *, lease_seconds: int) -> RuntimeWatchLease: ...

    def schedule_terminal_drain(self, run_id: str, *, owner: str) -> bool: ...

    def acknowledge_terminal_handoff(self, watch_id: str, *, owner: str) -> bool: ...

    def commit_segment(
        self,
        *,
        lease: RuntimeWatchLease,
        segment: RuntimeLogSegmentDraft,
        next_cursor: RuntimeLogCursor,
    ) -> RuntimeLogSegment: ...

    def advance_cursor(
        self,
        *,
        lease: RuntimeWatchLease,
        next_cursor: RuntimeLogCursor,
    ) -> RuntimeLogCursor: ...

    def read_segment_content(self, segment_id: str, *, owner: str) -> bytes: ...

    def list_segments(
        self,
        run_id: str,
        *,
        owner: str,
        stream: RuntimeLogStream,
        limit: int = 100,
    ) -> list[RuntimeLogSegment]: ...

    def list_segments_from(
        self,
        run_id: str,
        *,
        owner: str,
        stream: RuntimeLogStream,
        generation: int,
        offset: int,
        limit: int = 100,
    ) -> list[RuntimeLogSegment]: ...

    def get_previous_segment(
        self,
        run_id: str,
        *,
        owner: str,
        stream: RuntimeLogStream,
        generation: int,
        start_offset: int,
    ) -> RuntimeLogSegment | None: ...

    def save_alert(self, alert: RuntimeAlert) -> RuntimeAlert: ...

    def list_alerts(self, run_id: str, *, owner: str, limit: int = 100) -> list[RuntimeAlert]: ...

    def release_watch(
        self,
        lease: RuntimeWatchLease,
        *,
        state: RuntimeWatchState | str,
        next_poll_at: str | None,
        last_error_code: str | None = None,
    ) -> RuntimeWatchRecord: ...


class SQLiteRuntimeWatchStore:
    def __init__(
        self,
        db_path: Path,
        *,
        segment_root: Path,
        clock: Callable[[], datetime] | None = None,
        before_segment_transaction: Callable[[], None] | None = None,
    ) -> None:
        self.db_path = db_path
        self.segment_root = segment_root.resolve()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._before_segment_transaction = before_segment_transaction
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        if segment_root.is_symlink():
            raise ValueError("Runtime segment root cannot be a symlink")
        self.segment_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connect() as connection:
            apply_schema_migrations(connection, RUNTIME_WATCH_MIGRATIONS)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def create_watch(self, *, run_id: str, owner: str, connection_id: str) -> RuntimeWatchRecord:
        stdout = RuntimeLogCursor.initial(run_id=run_id, owner=owner, stream="stdout")
        stderr = RuntimeLogCursor.initial(run_id=run_id, owner=owner, stream="stderr")
        watch_id = _watch_id(run_id, owner)
        now = self._now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO runtime_watches (
                    watch_id, run_id, owner, connection_id, state, version,
                    next_poll_at, lease_owner, lease_expires_at, fencing_token,
                    created_at, updated_at, stopped_at, last_error_code, last_error_at
                ) VALUES (?, ?, ?, ?, 'watching', 0, ?, NULL, NULL, 0, ?, ?, NULL, NULL, NULL)
                ON CONFLICT (owner, run_id) DO NOTHING
                """,
                (watch_id, run_id, owner, connection_id, now, now, now),
            )
            row = connection.execute(
                "SELECT * FROM runtime_watches WHERE owner = ? AND run_id = ?",
                (owner, run_id),
            ).fetchone()
            if row is None:
                raise RuntimeError("Runtime Watch insert did not produce a row")
            if row["connection_id"] != connection_id:
                raise RuntimeWatchConflict("Run already has a Runtime Watch for another connection")
            for cursor in (stdout, stderr):
                connection.execute(
                    """
                    INSERT INTO runtime_log_cursors (
                        watch_id, run_id, owner, stream, generation, offset_value,
                        source_size, source_mtime, source_file_identity,
                        source_prefix_fingerprint, decoder_remainder_base64,
                        last_data_at, last_checked_at, quiet_polls, version
                    ) VALUES (?, ?, ?, ?, 0, 0, 0, NULL, NULL, NULL, '', NULL, NULL, 0, 0)
                    ON CONFLICT (watch_id, stream) DO NOTHING
                    """,
                    (watch_id, run_id, owner, cursor.stream),
                )
        return self.get_watch(watch_id, owner=owner)

    def get_watch(self, watch_id: str, *, owner: str) -> RuntimeWatchRecord:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM runtime_watches WHERE watch_id = ? AND owner = ?",
                (watch_id, owner),
            ).fetchone()
            if row is None:
                raise KeyError(watch_id)
            cursor_rows = connection.execute(
                "SELECT * FROM runtime_log_cursors WHERE watch_id = ? AND owner = ? "
                "ORDER BY CASE stream WHEN 'stdout' THEN 0 ELSE 1 END",
                (watch_id, owner),
            ).fetchall()
        return _watch_from_rows(row, cursor_rows)

    def get_watch_for_run(self, run_id: str, *, owner: str) -> RuntimeWatchRecord:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT watch_id FROM runtime_watches WHERE run_id = ? AND owner = ?",
                (run_id, owner),
            ).fetchone()
        if row is None:
            raise KeyError(run_id)
        return self.get_watch(str(row["watch_id"]), owner=owner)

    def get_cursor(self, run_id: str, owner: str, stream: RuntimeLogStream) -> RuntimeLogCursor:
        _validate_stream(stream)
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM runtime_log_cursors WHERE run_id = ? AND owner = ? AND stream = ?",
                (run_id, owner, stream),
            ).fetchone()
        if row is None:
            raise KeyError((run_id, stream))
        return _cursor_from_row(row)

    def claim_watch(
        self,
        watch_id: str,
        *,
        owner: str,
        worker_id: str,
        lease_seconds: int,
    ) -> RuntimeWatchLease | None:
        _bounded_lease(lease_seconds)
        _id(worker_id, "worker_id")
        now = self._clock_value()
        now_text = timestamp(now)
        expires_at = timestamp(now + timedelta(seconds=lease_seconds))
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                UPDATE runtime_watches
                SET lease_owner = ?, lease_expires_at = ?,
                    fencing_token = fencing_token + 1,
                    version = version + 1, updated_at = ?
                WHERE watch_id = ? AND owner = ? AND state != 'stopped'
                  AND (lease_owner IS NULL OR lease_expires_at <= ?)
                RETURNING *
                """,
                (worker_id, expires_at, now_text, watch_id, owner, now_text),
            ).fetchone()
            if row is None:
                exists = connection.execute(
                    "SELECT 1 FROM runtime_watches WHERE watch_id = ? AND owner = ?",
                    (watch_id, owner),
                ).fetchone()
                if exists is None:
                    raise KeyError(watch_id)
                return None
        return RuntimeWatchLease(
            watch_id=str(row["watch_id"]),
            run_id=str(row["run_id"]),
            owner=str(row["owner"]),
            worker_id=worker_id,
            version=int(row["version"]),
            fencing_token=int(row["fencing_token"]),
            expires_at=expires_at,
        )

    def list_due_watches(self, *, limit: int = 100) -> list[RuntimeWatchRecord]:
        _limit(limit)
        now = self._now()
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT watch_id, owner FROM runtime_watches "
                "WHERE state != 'stopped' AND (next_poll_at IS NULL OR next_poll_at <= ?) "
                "AND (lease_owner IS NULL OR lease_expires_at <= ?) "
                "ORDER BY COALESCE(next_poll_at, created_at), watch_id LIMIT ?",
                (now, now, limit),
            ).fetchall()
        return [self.get_watch(str(row["watch_id"]), owner=str(row["owner"])) for row in rows]

    def list_stopped_watches(self, *, limit: int = 100) -> list[RuntimeWatchRecord]:
        _limit(limit)
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT watch_id, owner FROM runtime_watches WHERE state = 'stopped' "
                "AND terminal_handoff_at IS NULL "
                "ORDER BY stopped_at, watch_id LIMIT ?",
                (limit,),
            ).fetchall()
        return [self.get_watch(str(row["watch_id"]), owner=str(row["owner"])) for row in rows]

    def renew_watch(self, lease: RuntimeWatchLease, *, lease_seconds: int) -> RuntimeWatchLease:
        _bounded_lease(lease_seconds)
        now = self._clock_value()
        now_text = timestamp(now)
        expires_at = timestamp(now + timedelta(seconds=lease_seconds))
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            updated = connection.execute(
                "UPDATE runtime_watches SET lease_expires_at = ?, updated_at = ? "
                "WHERE watch_id = ? AND run_id = ? AND owner = ? AND lease_owner = ? "
                "AND lease_expires_at > ? AND fencing_token = ? AND version = ?",
                (
                    expires_at,
                    now_text,
                    lease.watch_id,
                    lease.run_id,
                    lease.owner,
                    lease.worker_id,
                    now_text,
                    lease.fencing_token,
                    lease.version,
                ),
            )
            if updated.rowcount != 1:
                raise RuntimeWatchConflict("Runtime Watch renewal is stale or fenced")
        return replace(lease, expires_at=expires_at)

    def schedule_terminal_drain(self, run_id: str, *, owner: str) -> bool:
        now = self._now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            updated = connection.execute(
                "UPDATE runtime_watches SET state = 'finalizing', next_poll_at = ?, "
                "version = version + 1, updated_at = ?, terminal_handoff_at = NULL "
                "WHERE run_id = ? AND owner = ? AND state != 'stopped'",
                (now, now, run_id, owner),
            )
        return updated.rowcount == 1

    def acknowledge_terminal_handoff(self, watch_id: str, *, owner: str) -> bool:
        now = self._now()
        with self.connect() as connection:
            updated = connection.execute(
                "UPDATE runtime_watches SET terminal_handoff_at = ?, updated_at = ? "
                "WHERE watch_id = ? AND owner = ? AND state = 'stopped' "
                "AND terminal_handoff_at IS NULL",
                (now, now, watch_id, owner),
            )
        return updated.rowcount == 1

    def commit_segment(
        self,
        *,
        lease: RuntimeWatchLease,
        segment: RuntimeLogSegmentDraft,
        next_cursor: RuntimeLogCursor,
    ) -> RuntimeLogSegment:
        if not isinstance(lease, RuntimeWatchLease):
            raise TypeError("lease must be RuntimeWatchLease")
        if not isinstance(segment, RuntimeLogSegmentDraft):
            raise TypeError("segment must be RuntimeLogSegmentDraft")
        if not isinstance(next_cursor, RuntimeLogCursor):
            raise TypeError("next_cursor must be RuntimeLogCursor")
        content_path = self._write_content(segment.content_sha256, segment.content)
        if self._before_segment_transaction is not None:
            self._before_segment_transaction()
        now = self._now()
        candidate = RuntimeLogSegment(
            segment_id=segment.segment_id,
            watch_id=lease.watch_id,
            run_id=segment.run_id,
            owner=segment.owner,
            stream=segment.stream,
            generation=segment.generation,
            start_offset=segment.start_offset,
            end_offset=segment.end_offset,
            content_sha256=segment.content_sha256,
            content_size=len(segment.content),
            content_ref=f"sha256:{segment.content_sha256}",
            created_at=now,
        )
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_lease(connection, lease)
            existing_row = connection.execute(
                "SELECT * FROM runtime_log_segments WHERE segment_id = ? AND owner = ?",
                (candidate.segment_id, candidate.owner),
            ).fetchone()
            cursor_row = connection.execute(
                "SELECT * FROM runtime_log_cursors WHERE watch_id = ? AND owner = ? "
                "AND run_id = ? AND stream = ?",
                (lease.watch_id, lease.owner, lease.run_id, segment.stream),
            ).fetchone()
            if cursor_row is None:
                raise RuntimeWatchConflict("Runtime cursor is missing")
            current = _cursor_from_row(cursor_row)
            if existing_row is not None:
                existing = _segment_from_row(existing_row)
                if not _same_segment(existing, candidate):
                    raise RuntimeWatchConflict("Runtime segment replay conflicts")
                if current != next_cursor:
                    raise RuntimeWatchConflict("Runtime segment replay cursor conflicts")
                return existing
            position_row = connection.execute(
                "SELECT segment_id FROM runtime_log_segments "
                "WHERE owner = ? AND run_id = ? AND stream = ? "
                "AND generation = ? AND start_offset = ?",
                (
                    segment.owner,
                    segment.run_id,
                    segment.stream,
                    segment.generation,
                    segment.start_offset,
                ),
            ).fetchone()
            if position_row is not None:
                raise RuntimeWatchConflict("Runtime segment position already has different content")
            _validate_segment_transition(lease, current, segment, next_cursor)
            connection.execute(
                """
                INSERT INTO runtime_log_segments (
                    segment_id, watch_id, run_id, owner, stream, generation,
                    start_offset, end_offset, content_sha256, content_size,
                    content_ref, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    candidate.segment_id,
                    candidate.watch_id,
                    candidate.run_id,
                    candidate.owner,
                    candidate.stream,
                    candidate.generation,
                    candidate.start_offset,
                    candidate.end_offset,
                    candidate.content_sha256,
                    candidate.content_size,
                    candidate.content_ref,
                    candidate.created_at,
                ),
            )
            updated = connection.execute(
                """
                UPDATE runtime_log_cursors
                SET generation = ?, offset_value = ?, source_size = ?,
                    source_mtime = ?, source_file_identity = ?,
                    source_prefix_fingerprint = ?, decoder_remainder_base64 = ?,
                    last_data_at = ?, last_checked_at = ?, quiet_polls = ?, version = ?
                WHERE watch_id = ? AND owner = ? AND run_id = ? AND stream = ?
                  AND version = ?
                """,
                _cursor_update_values(
                    next_cursor,
                    watch_id=lease.watch_id,
                    expected_version=current.version,
                ),
            )
            if updated.rowcount != 1:
                raise RuntimeWatchConflict("Runtime cursor update was fenced")
        if content_path.stat().st_size != candidate.content_size:
            raise RuntimeError("Runtime segment content size changed after commit")
        return candidate

    def read_segment_content(self, segment_id: str, *, owner: str) -> bytes:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM runtime_log_segments WHERE segment_id = ? AND owner = ?",
                (segment_id, owner),
            ).fetchone()
        if row is None:
            raise KeyError(segment_id)
        segment = _segment_from_row(row)
        path = self._content_path(segment.content_sha256)
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise RuntimeError("Runtime segment content is unavailable") from exc
        if len(content) != segment.content_size or hashlib.sha256(content).hexdigest() != (
            segment.content_sha256
        ):
            raise RuntimeError("Runtime segment content failed integrity validation")
        return content

    def advance_cursor(
        self,
        *,
        lease: RuntimeWatchLease,
        next_cursor: RuntimeLogCursor,
    ) -> RuntimeLogCursor:
        if not isinstance(lease, RuntimeWatchLease):
            raise TypeError("lease must be RuntimeWatchLease")
        if not isinstance(next_cursor, RuntimeLogCursor):
            raise TypeError("next_cursor must be RuntimeLogCursor")
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_lease(connection, lease)
            row = connection.execute(
                "SELECT * FROM runtime_log_cursors WHERE watch_id = ? AND owner = ? "
                "AND run_id = ? AND stream = ?",
                (
                    lease.watch_id,
                    lease.owner,
                    lease.run_id,
                    next_cursor.stream,
                ),
            ).fetchone()
            if row is None:
                raise RuntimeWatchConflict("Runtime cursor is missing")
            current = _cursor_from_row(row)
            if current == next_cursor:
                return current
            _validate_cursor_advance(lease, current, next_cursor)
            updated = connection.execute(
                """
                UPDATE runtime_log_cursors
                SET generation = ?, offset_value = ?, source_size = ?,
                    source_mtime = ?, source_file_identity = ?,
                    source_prefix_fingerprint = ?, decoder_remainder_base64 = ?,
                    last_data_at = ?, last_checked_at = ?, quiet_polls = ?, version = ?
                WHERE watch_id = ? AND owner = ? AND run_id = ? AND stream = ?
                  AND version = ?
                """,
                _cursor_update_values(
                    next_cursor,
                    watch_id=lease.watch_id,
                    expected_version=current.version,
                ),
            )
            if updated.rowcount != 1:
                raise RuntimeWatchConflict("Runtime cursor update was fenced")
        return next_cursor

    def list_segments(
        self,
        run_id: str,
        *,
        owner: str,
        stream: RuntimeLogStream,
        limit: int = 100,
    ) -> list[RuntimeLogSegment]:
        _validate_stream(stream)
        _limit(limit)
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM runtime_log_segments "
                "WHERE run_id = ? AND owner = ? AND stream = ? "
                "ORDER BY generation, start_offset LIMIT ?",
                (run_id, owner, stream, limit),
            ).fetchall()
        return [_segment_from_row(row) for row in rows]

    def list_segments_from(
        self,
        run_id: str,
        *,
        owner: str,
        stream: RuntimeLogStream,
        generation: int,
        offset: int,
        limit: int = 100,
    ) -> list[RuntimeLogSegment]:
        _validate_stream(stream)
        _non_negative(generation, "generation")
        _non_negative(offset, "offset")
        _limit(limit)
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM runtime_log_segments WHERE run_id = ? AND owner = ? "
                "AND stream = ? AND (generation > ? OR (generation = ? AND end_offset > ?)) "
                "ORDER BY generation, start_offset LIMIT ?",
                (run_id, owner, stream, generation, generation, offset, limit),
            ).fetchall()
        return [_segment_from_row(row) for row in rows]

    def get_previous_segment(
        self,
        run_id: str,
        *,
        owner: str,
        stream: RuntimeLogStream,
        generation: int,
        start_offset: int,
    ) -> RuntimeLogSegment | None:
        _validate_stream(stream)
        _non_negative(generation, "generation")
        _non_negative(start_offset, "start_offset")
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM runtime_log_segments WHERE run_id = ? AND owner = ? "
                "AND stream = ? AND generation = ? AND end_offset <= ? "
                "ORDER BY end_offset DESC LIMIT 1",
                (run_id, owner, stream, generation, start_offset),
            ).fetchone()
        return None if row is None else _segment_from_row(row)

    def save_alert(self, alert: RuntimeAlert) -> RuntimeAlert:
        if not isinstance(alert, RuntimeAlert):
            raise TypeError("alert must be RuntimeAlert")
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            watch = connection.execute(
                "SELECT 1 FROM runtime_watches WHERE watch_id = ? AND run_id = ? AND owner = ?",
                (alert.watch_id, alert.run_id, alert.owner),
            ).fetchone()
            if watch is None:
                raise KeyError(alert.watch_id)
            if alert.segment_id is not None:
                segment = connection.execute(
                    "SELECT 1 FROM runtime_log_segments "
                    "WHERE segment_id = ? AND run_id = ? AND owner = ?",
                    (alert.segment_id, alert.run_id, alert.owner),
                ).fetchone()
                if segment is None:
                    raise KeyError(alert.segment_id)
            connection.execute(
                """
                INSERT INTO runtime_alerts (
                    alert_id, watch_id, run_id, owner, code, severity, summary,
                    segment_id, generation, offset_value, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (alert_id) DO NOTHING
                """,
                _alert_values(alert),
            )
            row = connection.execute(
                "SELECT * FROM runtime_alerts WHERE alert_id = ? AND owner = ?",
                (alert.alert_id, alert.owner),
            ).fetchone()
        if row is None:
            raise RuntimeError("Runtime alert insert did not produce a row")
        stored = _alert_from_row(row)
        if stored != alert:
            raise RuntimeWatchConflict("Runtime alert replay conflicts")
        return stored

    def list_alerts(self, run_id: str, *, owner: str, limit: int = 100) -> list[RuntimeAlert]:
        _limit(limit)
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM runtime_alerts WHERE run_id = ? AND owner = ? "
                "ORDER BY created_at, alert_id LIMIT ?",
                (run_id, owner, limit),
            ).fetchall()
        return [_alert_from_row(row) for row in rows]

    def release_watch(
        self,
        lease: RuntimeWatchLease,
        *,
        state: RuntimeWatchState | str,
        next_poll_at: str | None,
        last_error_code: str | None = None,
    ) -> RuntimeWatchRecord:
        normalized_state = RuntimeWatchState(state)
        normalized_next_poll_at = (
            None if next_poll_at is None else timestamp(parse_timestamp(next_poll_at))
        )
        if last_error_code is not None:
            _id(last_error_code, "last_error_code")
        now = self._now()
        stopped_at = now if normalized_state == RuntimeWatchState.STOPPED else None
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            updated = connection.execute(
                """
                UPDATE runtime_watches
                SET state = ?, version = version + 1, next_poll_at = ?,
                    lease_owner = NULL, lease_expires_at = NULL, updated_at = ?,
                    stopped_at = ?, last_error_code = ?, last_error_at = ?
                WHERE watch_id = ? AND run_id = ? AND owner = ?
                  AND lease_owner = ? AND lease_expires_at > ?
                  AND fencing_token = ? AND version = ?
                """,
                (
                    normalized_state.value,
                    normalized_next_poll_at,
                    now,
                    stopped_at,
                    last_error_code,
                    now if last_error_code is not None else None,
                    lease.watch_id,
                    lease.run_id,
                    lease.owner,
                    lease.worker_id,
                    now,
                    lease.fencing_token,
                    lease.version,
                ),
            )
            if updated.rowcount != 1:
                raise RuntimeWatchConflict("Runtime Watch release is stale or fenced")
        return self.get_watch(lease.watch_id, owner=lease.owner)

    def _assert_lease(self, connection: sqlite3.Connection, lease: RuntimeWatchLease) -> None:
        row = connection.execute(
            """
            SELECT 1 FROM runtime_watches
            WHERE watch_id = ? AND run_id = ? AND owner = ?
              AND lease_owner = ? AND lease_expires_at > ?
              AND fencing_token = ? AND version = ?
            """,
            (
                lease.watch_id,
                lease.run_id,
                lease.owner,
                lease.worker_id,
                self._now(),
                lease.fencing_token,
                lease.version,
            ),
        ).fetchone()
        if row is None:
            raise RuntimeWatchConflict("Runtime Watch lease is stale or fenced")

    def _write_content(self, digest: str, content: bytes) -> Path:
        destination = self._content_path(digest)
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if destination.exists():
            if (
                destination.stat().st_size != len(content)
                or hashlib.sha256(destination.read_bytes()).hexdigest() != digest
            ):
                raise RuntimeError("Runtime segment CAS contains conflicting content")
            return destination
        temporary_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=destination.parent,
                prefix=f".{digest}.",
                delete=False,
            ) as handle:
                temporary_name = handle.name
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary_name, 0o600)
            os.replace(temporary_name, destination)
            temporary_name = None
        finally:
            if temporary_name is not None:
                Path(temporary_name).unlink(missing_ok=True)
        return destination

    def _content_path(self, digest: str) -> Path:
        return self.segment_root / digest[:2] / digest

    def _clock_value(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            raise ValueError("Runtime Watch clock must be timezone-aware")
        return value.astimezone(UTC)

    def _now(self) -> str:
        return timestamp(self._clock_value())


def _watch_id(run_id: str, owner: str) -> str:
    _id(run_id, "run_id")
    _id(owner, "owner")
    digest = hashlib.sha256(f"{owner}\0{run_id}".encode()).hexdigest()
    return f"watch-{digest[:24]}"


def _validate_segment_transition(
    lease: RuntimeWatchLease,
    current: RuntimeLogCursor,
    segment: RuntimeLogSegmentDraft,
    next_cursor: RuntimeLogCursor,
) -> None:
    if (
        segment.run_id != lease.run_id
        or segment.owner != lease.owner
        or next_cursor.run_id != lease.run_id
        or next_cursor.owner != lease.owner
        or next_cursor.stream != segment.stream
    ):
        raise RuntimeWatchConflict("Runtime segment binding conflicts with its lease")
    same_generation = (
        segment.generation == current.generation and segment.start_offset == current.offset
    )
    rotated = segment.generation == current.generation + 1 and segment.start_offset == 0
    if not same_generation and not rotated:
        raise RuntimeWatchConflict("Runtime segment cursor is non-contiguous")
    if (
        next_cursor.generation != segment.generation
        or next_cursor.offset != segment.end_offset
        or next_cursor.version != current.version + 1
        or next_cursor.source_size < next_cursor.offset
    ):
        raise RuntimeWatchConflict("Runtime next cursor is invalid")


def _validate_cursor_advance(
    lease: RuntimeWatchLease,
    current: RuntimeLogCursor,
    next_cursor: RuntimeLogCursor,
) -> None:
    if (
        next_cursor.run_id != lease.run_id
        or next_cursor.owner != lease.owner
        or next_cursor.stream != current.stream
        or next_cursor.version != current.version + 1
    ):
        raise RuntimeWatchConflict("Runtime cursor advance binding is invalid")
    same_position = (
        next_cursor.generation == current.generation and next_cursor.offset == current.offset
    )
    empty_rotation = next_cursor.generation == current.generation + 1 and next_cursor.offset == 0
    if not same_position and not empty_rotation:
        raise RuntimeWatchConflict("Runtime cursor advance would skip log content")


def _watch_from_rows(row: sqlite3.Row, cursor_rows: list[sqlite3.Row]) -> RuntimeWatchRecord:
    return RuntimeWatchRecord(
        watch_id=str(row["watch_id"]),
        run_id=str(row["run_id"]),
        owner=str(row["owner"]),
        connection_id=str(row["connection_id"]),
        state=RuntimeWatchState(str(row["state"])),
        version=int(row["version"]),
        next_poll_at=_optional_timestamp(row["next_poll_at"]),
        lease_owner=_optional_text(row["lease_owner"]),
        lease_expires_at=_optional_timestamp(row["lease_expires_at"]),
        fencing_token=int(row["fencing_token"]),
        cursors=tuple(_cursor_from_row(item) for item in cursor_rows),
        created_at=_timestamp_value(row["created_at"]),
        updated_at=_timestamp_value(row["updated_at"]),
        stopped_at=_optional_timestamp(row["stopped_at"]),
        last_error_code=_optional_text(row["last_error_code"]),
        last_error_at=_optional_timestamp(row["last_error_at"]),
    )


def _cursor_from_row(row: sqlite3.Row) -> RuntimeLogCursor:
    return RuntimeLogCursor(
        run_id=str(row["run_id"]),
        owner=str(row["owner"]),
        stream=str(row["stream"]),  # type: ignore[arg-type]
        generation=int(row["generation"]),
        offset=int(row["offset_value"]),
        source_size=int(row["source_size"]),
        source_mtime=(None if row["source_mtime"] is None else float(row["source_mtime"])),
        source_file_identity=_optional_text(row["source_file_identity"]),
        source_prefix_fingerprint=_optional_text(row["source_prefix_fingerprint"]),
        decoder_remainder_base64=str(row["decoder_remainder_base64"]),
        last_data_at=_optional_timestamp(row["last_data_at"]),
        last_checked_at=_optional_timestamp(row["last_checked_at"]),
        quiet_polls=int(row["quiet_polls"]),
        version=int(row["version"]),
    )


def _segment_from_row(row: sqlite3.Row) -> RuntimeLogSegment:
    return RuntimeLogSegment(
        segment_id=str(row["segment_id"]),
        watch_id=str(row["watch_id"]),
        run_id=str(row["run_id"]),
        owner=str(row["owner"]),
        stream=str(row["stream"]),  # type: ignore[arg-type]
        generation=int(row["generation"]),
        start_offset=int(row["start_offset"]),
        end_offset=int(row["end_offset"]),
        content_sha256=str(row["content_sha256"]),
        content_size=int(row["content_size"]),
        content_ref=str(row["content_ref"]),
        created_at=_timestamp_value(row["created_at"]),
    )


def _alert_from_row(row: sqlite3.Row) -> RuntimeAlert:
    return RuntimeAlert(
        alert_id=str(row["alert_id"]),
        watch_id=str(row["watch_id"]),
        run_id=str(row["run_id"]),
        owner=str(row["owner"]),
        code=str(row["code"]),
        severity=str(row["severity"]),  # type: ignore[arg-type]
        summary=str(row["summary"]),
        segment_id=_optional_text(row["segment_id"]),
        generation=int(row["generation"]),
        offset=int(row["offset_value"]),
        created_at=_timestamp_value(row["created_at"]),
    )


def _same_segment(left: RuntimeLogSegment, right: RuntimeLogSegment) -> bool:
    return (
        left.segment_id == right.segment_id
        and left.watch_id == right.watch_id
        and left.run_id == right.run_id
        and left.owner == right.owner
        and left.stream == right.stream
        and left.generation == right.generation
        and left.start_offset == right.start_offset
        and left.end_offset == right.end_offset
        and left.content_sha256 == right.content_sha256
        and left.content_size == right.content_size
        and left.content_ref == right.content_ref
    )


def _cursor_update_values(
    cursor: RuntimeLogCursor, *, watch_id: str, expected_version: int
) -> tuple[object, ...]:
    return (
        cursor.generation,
        cursor.offset,
        cursor.source_size,
        cursor.source_mtime,
        cursor.source_file_identity,
        cursor.source_prefix_fingerprint,
        cursor.decoder_remainder_base64,
        cursor.last_data_at,
        cursor.last_checked_at,
        cursor.quiet_polls,
        cursor.version,
        watch_id,
        cursor.owner,
        cursor.run_id,
        cursor.stream,
        expected_version,
    )


def _alert_values(alert: RuntimeAlert) -> tuple[object, ...]:
    return (
        alert.alert_id,
        alert.watch_id,
        alert.run_id,
        alert.owner,
        alert.code,
        alert.severity,
        alert.summary,
        alert.segment_id,
        alert.generation,
        alert.offset,
        alert.created_at,
    )


def _optional_text(value: object) -> str | None:
    return None if value is None else str(value)


def _optional_timestamp(value: object) -> str | None:
    return None if value is None else _timestamp_value(value)


def _timestamp_value(value: object) -> str:
    if isinstance(value, datetime):
        return timestamp(value)
    return str(value)


def _validate_stream(value: object) -> None:
    if value not in {"stdout", "stderr"}:
        raise ValueError("Runtime stream is invalid")


def _bounded_lease(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 300:
        raise ValueError("lease_seconds must be between 1 and 300")
    return value


def _limit(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 1000:
        raise ValueError("limit must be between 1 and 1000")
    return value


def _non_negative(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def _id(value: object, label: str) -> str:
    from pilot107.runtime_watch.model import _identifier

    return _identifier(value, label)
