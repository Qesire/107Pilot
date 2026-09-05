from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from pilot107.runtime_watch.model import (
    RuntimeAlert,
    RuntimeLogCursor,
    RuntimeLogSegmentDraft,
    RuntimeWatchConflict,
    RuntimeWatchLease,
    runtime_watch_payload,
)
from pilot107.runtime_watch.postgres_store import (
    PostgresRuntimeWatchStore,
    _PostgresConnection,
)
from pilot107.runtime_watch.store import RuntimeWatchStore, SQLiteRuntimeWatchStore


class MutableClock:
    def __init__(self) -> None:
        self.current = datetime(2026, 8, 19, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.current

    def advance(self, seconds: int) -> None:
        self.current += timedelta(seconds=seconds)


def _cursor(
    *,
    offset: int,
    version: int,
    generation: int = 0,
    stream: str = "stdout",
) -> RuntimeLogCursor:
    return RuntimeLogCursor(
        run_id="run1",
        owner="alice",
        stream=stream,  # type: ignore[arg-type]
        generation=generation,
        offset=offset,
        source_size=offset,
        source_mtime=1.0,
        source_file_identity="inode:107",
        source_prefix_fingerprint="a" * 64,
        decoder_remainder_base64="",
        last_data_at="2026-08-19T00:00:00Z" if offset else None,
        last_checked_at="2026-08-19T00:00:00Z",
        quiet_polls=0,
        version=version,
    )


def _segment(
    content: bytes = b"line 1\n",
    *,
    start_offset: int = 0,
    generation: int = 0,
    stream: str = "stdout",
) -> RuntimeLogSegmentDraft:
    return RuntimeLogSegmentDraft(
        run_id="run1",
        owner="alice",
        stream=stream,  # type: ignore[arg-type]
        generation=generation,
        start_offset=start_offset,
        content=content,
    )


def _claimed(store: RuntimeWatchStore) -> RuntimeWatchLease:
    watch = store.create_watch(
        run_id="run1",
        owner="alice",
        connection_id="connection1",
    )
    lease = store.claim_watch(
        watch.watch_id,
        owner="alice",
        worker_id="worker1",
        lease_seconds=30,
    )
    assert lease is not None
    return lease


def exercise_store_contract(store: RuntimeWatchStore, clock: MutableClock) -> None:
    created = store.create_watch(
        run_id="run1",
        owner="alice",
        connection_id="connection1",
    )
    replayed = store.create_watch(
        run_id="run1",
        owner="alice",
        connection_id="connection1",
    )
    assert replayed == created
    assert [item.stream for item in created.cursors] == ["stdout", "stderr"]
    with pytest.raises(KeyError):
        store.get_watch(created.watch_id, owner="bob")

    lease = store.claim_watch(
        created.watch_id,
        owner="alice",
        worker_id="worker1",
        lease_seconds=30,
    )
    assert lease is not None
    assert (
        store.claim_watch(
            created.watch_id,
            owner="alice",
            worker_id="worker2",
            lease_seconds=30,
        )
        is None
    )

    draft = _segment()
    next_cursor = _cursor(offset=len(draft.content), version=1)
    first = store.commit_segment(
        lease=lease,
        segment=draft,
        next_cursor=next_cursor,
    )
    second = store.commit_segment(
        lease=lease,
        segment=draft,
        next_cursor=next_cursor,
    )

    expected_id = hashlib.sha256(
        b"\0".join(
            (
                b"run1",
                b"stdout",
                b"0",
                b"0",
                hashlib.sha256(draft.content).hexdigest().encode(),
            )
        )
    ).hexdigest()
    assert first.segment_id == f"segment-{expected_id}"
    assert second == first
    assert store.get_cursor("run1", "alice", "stdout").offset == len(draft.content)
    assert store.get_cursor("run1", "alice", "stdout").version == 1
    assert store.read_segment_content(first.segment_id, owner="alice") == draft.content
    assert store.list_segments("run1", owner="alice", stream="stdout") == [first]
    with pytest.raises(KeyError):
        store.read_segment_content(first.segment_id, owner="bob")

    alert = RuntimeAlert.create(
        watch_id=created.watch_id,
        run_id="run1",
        owner="alice",
        code="CUDA.OOM",
        severity="warning",
        summary="CUDA out of memory",
        segment_id=first.segment_id,
        generation=0,
        offset=0,
        created_at="2026-08-19T00:00:00Z",
    )
    assert store.save_alert(alert) == alert
    assert store.save_alert(alert) == alert
    assert store.list_alerts("run1", owner="alice") == [alert]
    assert store.list_alerts("run1", owner="bob") == []

    released = store.release_watch(
        lease,
        state="active",
        next_poll_at="2026-08-19T00:00:05Z",
    )
    assert released.lease_owner is None
    assert released.fencing_token == lease.fencing_token
    with pytest.raises(RuntimeWatchConflict, match="stale|fenced"):
        store.release_watch(lease, state="active", next_poll_at=None)

    clock.advance(31)
    second_lease = store.claim_watch(
        created.watch_id,
        owner="alice",
        worker_id="worker2",
        lease_seconds=30,
    )
    assert second_lease is not None
    assert second_lease.fencing_token > lease.fencing_token
    with pytest.raises(RuntimeWatchConflict, match="stale|fenced"):
        store.commit_segment(
            lease=lease,
            segment=_segment(b"stale\n", start_offset=len(draft.content)),
            next_cursor=_cursor(
                offset=len(draft.content) + len(b"stale\n"),
                version=2,
            ),
        )


def test_sqlite_store_satisfies_contract(tmp_path: Path) -> None:
    clock = MutableClock()
    exercise_store_contract(
        SQLiteRuntimeWatchStore(
            tmp_path / "watch.db",
            segment_root=tmp_path / "segments",
            clock=clock,
        ),
        clock,
    )


def test_commit_rejects_cursor_gaps_and_preserves_current_offset(tmp_path: Path) -> None:
    store = SQLiteRuntimeWatchStore(
        tmp_path / "watch.db",
        segment_root=tmp_path / "segments",
    )
    lease = _claimed(store)

    with pytest.raises(RuntimeWatchConflict, match="cursor"):
        store.commit_segment(
            lease=lease,
            segment=_segment(start_offset=4),
            next_cursor=_cursor(offset=11, version=1),
        )

    assert store.get_cursor("run1", "alice", "stdout").offset == 0
    assert store.list_segments("run1", owner="alice", stream="stdout") == []


def test_conflicting_content_cannot_replace_a_committed_segment_position(
    tmp_path: Path,
) -> None:
    store = SQLiteRuntimeWatchStore(
        tmp_path / "watch.db",
        segment_root=tmp_path / "segments",
    )
    lease = _claimed(store)
    original = _segment(b"first\n")
    store.commit_segment(
        lease=lease,
        segment=original,
        next_cursor=_cursor(offset=len(original.content), version=1),
    )

    with pytest.raises(RuntimeWatchConflict, match="position|cursor"):
        store.commit_segment(
            lease=lease,
            segment=_segment(b"different\n"),
            next_cursor=_cursor(offset=len(b"different\n"), version=1),
        )

    assert (
        store.read_segment_content(
            store.list_segments("run1", owner="alice", stream="stdout")[0].segment_id,
            owner="alice",
        )
        == original.content
    )


def test_rotation_advances_generation_and_resets_offset_atomically(tmp_path: Path) -> None:
    store = SQLiteRuntimeWatchStore(
        tmp_path / "watch.db",
        segment_root=tmp_path / "segments",
    )
    lease = _claimed(store)
    first = _segment(b"old\n")
    store.commit_segment(
        lease=lease,
        segment=first,
        next_cursor=_cursor(offset=len(first.content), version=1),
    )
    rotated = _segment(b"new\n", generation=1)

    stored = store.commit_segment(
        lease=lease,
        segment=rotated,
        next_cursor=_cursor(
            offset=len(rotated.content),
            version=2,
            generation=1,
        ),
    )

    cursor = store.get_cursor("run1", "alice", "stdout")
    assert (cursor.generation, cursor.offset, cursor.version) == (1, 4, 2)
    assert stored.generation == 1


def test_quiet_cursor_check_advances_once_without_creating_a_segment(
    tmp_path: Path,
) -> None:
    store = SQLiteRuntimeWatchStore(
        tmp_path / "watch.db",
        segment_root=tmp_path / "segments",
    )
    lease = _claimed(store)
    checked = replace(
        _cursor(offset=0, version=1),
        last_data_at=None,
        quiet_polls=1,
    )

    assert store.advance_cursor(lease=lease, next_cursor=checked) == checked
    assert store.advance_cursor(lease=lease, next_cursor=checked) == checked
    assert store.list_segments("run1", owner="alice", stream="stdout") == []


def test_segment_content_precedes_transaction_and_orphans_are_unreferenced(
    tmp_path: Path,
) -> None:
    def crash() -> None:
        raise RuntimeError("simulated crash")

    segment_root = tmp_path / "segments"
    store = SQLiteRuntimeWatchStore(
        tmp_path / "watch.db",
        segment_root=segment_root,
        before_segment_transaction=crash,
    )
    lease = _claimed(store)
    draft = _segment()

    with pytest.raises(RuntimeError, match="simulated crash"):
        store.commit_segment(
            lease=lease,
            segment=draft,
            next_cursor=_cursor(offset=len(draft.content), version=1),
        )

    assert list(segment_root.rglob(hashlib.sha256(draft.content).hexdigest()))
    assert store.list_segments("run1", owner="alice", stream="stdout") == []
    assert store.get_cursor("run1", "alice", "stdout").offset == 0


def test_sqlite_store_survives_reopen(tmp_path: Path) -> None:
    database = tmp_path / "watch.db"
    segment_root = tmp_path / "segments"
    first = SQLiteRuntimeWatchStore(database, segment_root=segment_root)
    lease = _claimed(first)
    draft = _segment()
    stored = first.commit_segment(
        lease=lease,
        segment=draft,
        next_cursor=_cursor(offset=len(draft.content), version=1),
    )

    reopened = SQLiteRuntimeWatchStore(database, segment_root=segment_root)

    assert reopened.get_cursor("run1", "alice", "stdout").offset == len(draft.content)
    assert reopened.read_segment_content(stored.segment_id, owner="alice") == draft.content


@pytest.mark.skipif(
    not os.environ.get("PILOT107_TEST_POSTGRES_DSN")
    or os.environ.get("PILOT107_TEST_POSTGRES_ALLOW_RESET") != "1",
    reason="set a dedicated PostgreSQL DSN and explicit reset opt-in",
)
def test_postgres_store_satisfies_contract(tmp_path: Path) -> None:
    dsn = os.environ["PILOT107_TEST_POSTGRES_DSN"]
    clock = MutableClock()
    store = PostgresRuntimeWatchStore(
        dsn,
        segment_root=tmp_path / "segments",
        clock=clock,
    )
    with store.connect() as connection:
        connection.execute("TRUNCATE runtime_watches CASCADE")

    exercise_store_contract(store, clock)


def test_cursor_model_rejects_backward_or_invalid_decoder_state() -> None:
    with pytest.raises(ValueError):
        replace(_cursor(offset=0, version=0), offset=-1)
    with pytest.raises(ValueError):
        replace(_cursor(offset=0, version=0), decoder_remainder_base64="***")


def test_runtime_watch_payload_matches_the_frozen_schema(tmp_path: Path) -> None:
    store = SQLiteRuntimeWatchStore(
        tmp_path / "watch.db",
        segment_root=tmp_path / "segments",
    )
    record = store.create_watch(
        run_id="run1",
        owner="alice",
        connection_id="connection1",
    )
    schema = json.loads(
        Path("schemas/runtime-watch/v1/runtime-watch.schema.json").read_text(encoding="utf-8")
    )

    Draft202012Validator(schema).validate(runtime_watch_payload(record))


def test_postgres_adapter_translates_binds_and_locks_the_fencing_record() -> None:
    calls: list[tuple[str, tuple[object, ...]]] = []

    class RawConnection:
        def execute(self, statement: str, parameters: tuple[object, ...] = ()):
            calls.append((statement, parameters))
            return self

    connection = _PostgresConnection(RawConnection())
    connection.execute("BEGIN IMMEDIATE")
    connection.execute(
        "SELECT 1 FROM runtime_watches WHERE watch_id = ? AND lease_owner = ?",
        ("watch1", "worker1"),
    )

    assert calls[0] == ("BEGIN", ())
    assert calls[1][0].endswith("FOR UPDATE")
    assert calls[1][0].count("%s") == 2
    assert calls[1][1] == ("watch1", "worker1")
