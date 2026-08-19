"""Leased, fair scheduling for incremental Runtime Watch collection."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from pilot107.core.paths import SafePath
from pilot107.core.run_store import RunStore
from pilot107.runtime_watch.model import (
    RuntimeLogSegmentDraft,
    RuntimeWatchConflict,
    RuntimeWatchRecord,
    RuntimeWatchState,
    timestamp,
)
from pilot107.runtime_watch.reader import IncrementalLogReader, RuntimeLogSource
from pilot107.runtime_watch.store import RuntimeWatchStore
from pilot107.worker.evidence import EvidenceTransport


class RuntimeLogSourceResolver(Protocol):
    def resolve(self, *, run_id: str, owner: str, connection_id: str) -> RuntimeLogSource: ...


class RunStoreRuntimeLogSourceResolver:
    """Resolve canonical Slurm output paths from persisted Run facts only."""

    def __init__(self, *, run_store: RunStore, allowed_roots: tuple[str, ...]) -> None:
        if not allowed_roots:
            raise ValueError("Runtime Watch requires at least one authorized root")
        self.run_store = run_store
        self.allowed_roots = tuple(Path(root) for root in allowed_roots)

    def resolve(self, *, run_id: str, owner: str, connection_id: str) -> RuntimeLogSource:
        run = self.run_store.get_run(run_id)
        if run.owner != owner:
            raise PermissionError("Runtime Watch Run owner mismatch")
        if run.job_id is None or re.fullmatch(r"[A-Za-z0-9_.-]+", run.job_id) is None:
            raise RuntimeError("Runtime Watch Run has no safe Slurm job ID")
        workdir = Path(run.workdir)
        if not workdir.is_absolute() or ".." in workdir.parts:
            raise PermissionError("Runtime Watch workdir is outside authorized roots")
        root = next(
            (
                candidate
                for candidate in self.allowed_roots
                if workdir == candidate or workdir.is_relative_to(candidate)
            ),
            None,
        )
        if root is None:
            raise PermissionError("Runtime Watch workdir is outside authorized roots")

        def safe(suffix: str) -> SafePath:
            path = workdir / f"slurm-{run.job_id}.{suffix}"
            return SafePath(original=str(path), resolved=path, root=root)

        return RuntimeLogSource(
            run_id=run_id,
            owner=owner,
            stdout_path=safe("out"),
            stderr_path=safe("err"),
        )


@dataclass(frozen=True)
class RuntimeWatchPolicy:
    active_poll_seconds: int = 5
    quiet_poll_seconds: int = 15
    lease_seconds: int = 30
    max_connections_per_tick: int = 4
    max_watches_per_tick: int = 1000
    max_bytes_per_connection_tick: int = 4 * 1024 * 1024
    max_bytes_per_watch_stream: int = 64 * 1024

    def __post_init__(self) -> None:
        values = (
            self.active_poll_seconds,
            self.quiet_poll_seconds,
            self.lease_seconds,
            self.max_connections_per_tick,
            self.max_watches_per_tick,
            self.max_bytes_per_connection_tick,
            self.max_bytes_per_watch_stream,
        )
        if any(isinstance(value, bool) or value <= 0 for value in values):
            raise ValueError("Runtime Watch policy values must be positive")
        if self.lease_seconds > 300:
            raise ValueError("Runtime Watch lease cannot exceed 300 seconds")
        if self.max_bytes_per_watch_stream > 256 * 1024:
            raise ValueError("Runtime Watch stream quantum cannot exceed 256 KiB")


@dataclass(frozen=True)
class RuntimeWatchTickResult:
    watches_checked: int = 0
    watches_with_data: int = 0
    bytes_read: int = 0
    errors: tuple[str, ...] = ()


class RuntimeWatchService:
    def __init__(
        self,
        *,
        store: RuntimeWatchStore,
        transport_for_connection: Callable[[str], EvidenceTransport],
        source_resolver: RuntimeLogSourceResolver,
        worker_id: str,
        clock: Callable[[], datetime] | None = None,
        policy: RuntimeWatchPolicy | None = None,
    ) -> None:
        self.store = store
        self.transport_for_connection = transport_for_connection
        self.source_resolver = source_resolver
        self.worker_id = worker_id
        self._clock = clock or (lambda: datetime.now(UTC))
        self.policy = policy or RuntimeWatchPolicy()

    def tick(self) -> RuntimeWatchTickResult:
        grouped: dict[str, list[RuntimeWatchRecord]] = defaultdict(list)
        for watch in self.store.list_due_watches(limit=self.policy.max_watches_per_tick):
            grouped[watch.connection_id].append(watch)
        checked = with_data = total_bytes = 0
        errors: list[str] = []
        for connection_id in sorted(grouped)[: self.policy.max_connections_per_tick]:
            budget = self.policy.max_bytes_per_connection_tick
            transport = self.transport_for_connection(connection_id)
            reader = IncrementalLogReader(transport=transport, clock=self._clock)
            for watch in grouped[connection_id]:
                if budget <= 0:
                    break
                lease = self.store.claim_watch(
                    watch.watch_id,
                    owner=watch.owner,
                    worker_id=self.worker_id,
                    lease_seconds=self.policy.lease_seconds,
                )
                if lease is None:
                    continue
                checked += 1
                watch_bytes = 0
                had_available_source = False
                try:
                    source = self.source_resolver.resolve(
                        run_id=watch.run_id,
                        owner=watch.owner,
                        connection_id=watch.connection_id,
                    )
                    for stream in ("stdout", "stderr"):
                        if budget <= 0:
                            break
                        lease = self.store.renew_watch(
                            lease, lease_seconds=self.policy.lease_seconds
                        )
                        current = self.store.get_cursor(watch.run_id, watch.owner, stream)
                        read = reader.read_next(
                            source,
                            stream,
                            current,
                            max_bytes=min(
                                budget,
                                self.policy.max_bytes_per_watch_stream,
                            ),
                        )
                        had_available_source = had_available_source or read.available
                        if read.content:
                            self.store.commit_segment(
                                lease=lease,
                                segment=RuntimeLogSegmentDraft(
                                    run_id=watch.run_id,
                                    owner=watch.owner,
                                    stream=stream,
                                    generation=read.next_cursor.generation,
                                    start_offset=(0 if read.rotated else current.offset),
                                    content=read.content,
                                ),
                                next_cursor=read.next_cursor,
                            )
                            amount = len(read.content)
                            budget -= amount
                            total_bytes += amount
                            watch_bytes += amount
                        else:
                            self.store.advance_cursor(
                                lease=lease,
                                next_cursor=read.next_cursor,
                            )
                    if watch_bytes:
                        with_data += 1
                    delay = (
                        self.policy.active_poll_seconds
                        if watch_bytes
                        else self.policy.quiet_poll_seconds
                    )
                    state = (
                        RuntimeWatchState.ACTIVE
                        if watch_bytes
                        else RuntimeWatchState.QUIET_BACKOFF
                        if had_available_source
                        else RuntimeWatchState.WAITING_FOR_LOG
                    )
                    self.store.release_watch(
                        lease,
                        state=state,
                        next_poll_at=timestamp(self._now() + timedelta(seconds=delay)),
                    )
                except RuntimeWatchConflict:
                    continue
                except Exception as exc:
                    errors.append(f"{watch.run_id}:{type(exc).__name__}")
                    with suppress(RuntimeWatchConflict):
                        self.store.release_watch(
                            lease,
                            state=RuntimeWatchState.DEGRADED,
                            next_poll_at=timestamp(
                                self._now() + timedelta(seconds=self.policy.quiet_poll_seconds)
                            ),
                            last_error_code="RUNTIME_WATCH_READ_ERROR",
                        )
        return RuntimeWatchTickResult(
            watches_checked=checked,
            watches_with_data=with_data,
            bytes_read=total_bytes,
            errors=tuple(errors),
        )

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            raise ValueError("Runtime Watch clock must be timezone-aware")
        return value.astimezone(UTC)
