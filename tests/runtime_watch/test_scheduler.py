from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from pilot107.core.paths import SafePath
from pilot107.runtime_watch.reader import RuntimeLogSource
from pilot107.runtime_watch.service import RuntimeWatchPolicy, RuntimeWatchService
from pilot107.runtime_watch.store import SQLiteRuntimeWatchStore
from pilot107.worker.evidence import AuthorizedFilesystemEvidenceTransport


class MutableClock:
    def __init__(self) -> None:
        self.current = datetime(2026, 8, 19, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.current

    def advance(self, seconds: int) -> None:
        self.current += timedelta(seconds=seconds)


class SourceResolver:
    def __init__(self, root: Path) -> None:
        self.root = root

    def resolve(self, *, run_id: str, owner: str, connection_id: str) -> RuntimeLogSource:
        return RuntimeLogSource(
            run_id=run_id,
            owner=owner,
            stdout_path=SafePath(
                original=str(self.root / f"{run_id}.out"),
                resolved=self.root / f"{run_id}.out",
                root=self.root,
            ),
            stderr_path=SafePath(
                original=str(self.root / f"{run_id}.err"),
                resolved=self.root / f"{run_id}.err",
                root=self.root,
            ),
        )


def _service(
    tmp_path: Path, clock: MutableClock, *, budget: int = 1024
) -> tuple[SQLiteRuntimeWatchStore, RuntimeWatchService]:
    log_root = tmp_path / "logs"
    log_root.mkdir(exist_ok=True)
    store = SQLiteRuntimeWatchStore(
        tmp_path / "watch.db", segment_root=tmp_path / "segments", clock=clock
    )
    transport = AuthorizedFilesystemEvidenceTransport(allowed_roots=[log_root])
    service = RuntimeWatchService(
        store=store,
        transport_for_connection=lambda connection_id: transport,
        source_resolver=SourceResolver(log_root),
        worker_id="worker1",
        clock=clock,
        policy=RuntimeWatchPolicy(
            max_connections_per_tick=1,
            max_bytes_per_connection_tick=budget,
            max_bytes_per_watch_stream=64,
        ),
    )
    return store, service


def test_scheduler_is_fair_across_one_hundred_active_watches(tmp_path: Path) -> None:
    clock = MutableClock()
    store, service = _service(tmp_path, clock)
    log_root = tmp_path / "logs"
    for index in range(100):
        run_id = f"run{index}"
        (log_root / f"{run_id}.out").write_bytes(b"x" * 4096)
        (log_root / f"{run_id}.err").write_bytes(b"")
        store.create_watch(run_id=run_id, owner="alice", connection_id="c1")

    result = service.tick()

    assert result.bytes_read <= 1024
    assert result.watches_with_data > 1
    assert (
        max(store.get_cursor(f"run{index}", "alice", "stdout").offset for index in range(100)) <= 64
    )


def test_restart_resumes_without_duplicate_segment_positions(tmp_path: Path) -> None:
    clock = MutableClock()
    store, first_service = _service(tmp_path, clock, budget=4)
    log_root = tmp_path / "logs"
    (log_root / "run1.out").write_bytes(b"abcdefgh")
    (log_root / "run1.err").write_bytes(b"")
    store.create_watch(run_id="run1", owner="alice", connection_id="c1")

    first_service.tick()
    clock.advance(5)
    reopened_store, second_service = _service(tmp_path, clock, budget=4)
    second_service.tick()

    segments = reopened_store.list_segments("run1", owner="alice", stream="stdout")
    assert [(item.start_offset, item.end_offset) for item in segments] == [(0, 4), (4, 8)]
    assert reopened_store.get_cursor("run1", "alice", "stdout").offset == 8


def test_second_worker_cannot_process_a_watch_before_next_poll(tmp_path: Path) -> None:
    clock = MutableClock()
    store, first_service = _service(tmp_path, clock)
    log_root = tmp_path / "logs"
    (log_root / "run1.out").write_bytes(b"data")
    (log_root / "run1.err").write_bytes(b"")
    store.create_watch(run_id="run1", owner="alice", connection_id="c1")
    first_service.tick()

    second_service = RuntimeWatchService(
        store=store,
        transport_for_connection=first_service.transport_for_connection,
        source_resolver=SourceResolver(log_root),
        worker_id="worker2",
        clock=clock,
    )

    assert second_service.tick().watches_checked == 0
