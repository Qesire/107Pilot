from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from pilot107.api.runtime_watch_routes import RuntimeWatchRoutes
from pilot107.core.identity import UserIdentity
from pilot107.runtime_watch.model import RuntimeLogCursor, RuntimeLogSegmentDraft
from pilot107.runtime_watch.store import SQLiteRuntimeWatchStore


def _routes(tmp_path: Path) -> tuple[SQLiteRuntimeWatchStore, RuntimeWatchRoutes]:
    store = SQLiteRuntimeWatchStore(
        tmp_path / "watch.db", segment_root=tmp_path / "segments"
    )
    watch = store.create_watch(run_id="run1", owner="alice", connection_id="c1")
    lease = store.claim_watch(
        watch.watch_id,
        owner="alice",
        worker_id="worker1",
        lease_seconds=30,
    )
    assert lease is not None
    content = b"hello runtime\n"
    cursor = RuntimeLogCursor.initial(run_id="run1", owner="alice", stream="stdout")
    store.commit_segment(
        lease=lease,
        segment=RuntimeLogSegmentDraft(
            run_id="run1",
            owner="alice",
            stream="stdout",
            generation=0,
            start_offset=0,
            content=content,
        ),
        next_cursor=replace(
            cursor,
            offset=len(content),
            source_size=len(content),
            last_data_at="2026-08-19T00:00:00Z",
            last_checked_at="2026-08-19T00:00:00Z",
            version=1,
        ),
    )
    return store, RuntimeWatchRoutes(store)


def test_owner_scoped_summary_and_opaque_log_cursor(tmp_path: Path) -> None:
    _, routes = _routes(tmp_path)
    identity = UserIdentity(username="alice")

    summary = routes.handle_get(
        ["runs", "run1", "runtime-watch"], params={}, identity=identity
    )
    first = routes.handle_get(
        ["runs", "run1", "runtime-watch", "logs"],
        params={"stream": ["stdout"], "max_bytes": ["8"]},
        identity=identity,
    )
    assert summary is not None and summary.status == 200
    assert first is not None and first.status == 200
    assert first.payload["content"] == "hello ru"
    assert isinstance(first.payload["next_cursor"], str)
    assert "segment" not in first.payload["next_cursor"]

    second = routes.handle_get(
        ["runs", "run1", "runtime-watch", "logs"],
        params={
            "stream": ["stdout"],
            "max_bytes": ["64"],
            "cursor": [str(first.payload["next_cursor"])],
        },
        identity=identity,
    )
    assert second is not None and second.payload["content"] == "ntime\n"


def test_cross_owner_runtime_watch_is_masked_and_never_polls_cluster(tmp_path: Path) -> None:
    _, routes = _routes(tmp_path)
    response = routes.handle_get(
        ["runs", "run1", "runtime-watch"],
        params={},
        identity=UserIdentity(username="bob"),
    )

    assert response is not None and response.status == 404
