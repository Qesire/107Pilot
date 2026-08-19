"""Durable typed resource observations with explicit retention classes."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol, TypeVar, cast

from pilot107.core.schema_migrations import SchemaMigration, apply_schema_migrations
from pilot107.observability.model import (
    AccountPulse,
    ObservedMeasure,
    PlatformPulse,
    ResourceMeasureSet,
    RunResourceSample,
    RunResourceSummary,
)

OBSERVABILITY_MIGRATIONS = (
    SchemaMigration(
        migration_id="008a.001.resource_observations",
        statements=(
            """
            CREATE TABLE resource_observations (
                observation_id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                resolution TEXT NOT NULL,
                connection_id TEXT NOT NULL,
                owner TEXT,
                run_id TEXT,
                attempt INTEGER,
                captured_at TEXT NOT NULL,
                expires_at TEXT,
                fencing_token INTEGER NOT NULL,
                payload_sha256 TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                CHECK (resolution IN ('raw', 'minute', 'terminal')),
                CHECK (fencing_token >= 0)
            )
            """,
            "CREATE INDEX idx_resource_observations_run "
            "ON resource_observations(owner, run_id, resolution, captured_at)",
            "CREATE INDEX idx_resource_observations_expiry ON resource_observations(expires_at)",
            "CREATE UNIQUE INDEX idx_resource_terminal_summary_unique "
            "ON resource_observations(owner, run_id, attempt, kind) "
            "WHERE kind = 'run_resource_summary'",
        ),
    ),
)


class ObservabilityConflict(RuntimeError):
    """An observation ID was replayed with different immutable facts."""


class ObservabilityStore(Protocol):
    def save_run_sample(self, value: RunResourceSample) -> RunResourceSample: ...
    def save_minute_aggregate(self, value: RunResourceSample) -> RunResourceSample: ...
    def save_summary(self, value: RunResourceSummary) -> RunResourceSummary: ...
    def list_run_samples(self, run_id: str, *, owner: str) -> list[RunResourceSample]: ...
    def list_minute_aggregates(
        self, run_id: str, *, owner: str
    ) -> list[RunResourceSample]: ...
    def get_summary(self, run_id: str, *, owner: str) -> RunResourceSummary: ...
    def prune_expired(self) -> int: ...


T = TypeVar("T", PlatformPulse, AccountPulse, RunResourceSample, RunResourceSummary)


class SQLiteObservabilityStore:
    def __init__(
        self, db_path: Path, *, clock: Callable[[], datetime] | None = None
    ) -> None:
        self.db_path = db_path
        self._clock = clock or (lambda: datetime.now(UTC))
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            apply_schema_migrations(connection, OBSERVABILITY_MIGRATIONS)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def save_platform_pulse(self, value: PlatformPulse) -> PlatformPulse:
        return self._save(value, kind="platform_pulse", resolution="raw", hours=2)

    def save_account_pulse(self, value: AccountPulse) -> AccountPulse:
        return self._save(value, kind="account_pulse", resolution="raw", hours=2)

    def save_run_sample(self, value: RunResourceSample) -> RunResourceSample:
        return self._save(value, kind="run_resource_sample", resolution="raw", hours=2)

    def save_minute_aggregate(self, value: RunResourceSample) -> RunResourceSample:
        return self._save(value, kind="run_resource_sample", resolution="minute", hours=24)

    def save_summary(self, value: RunResourceSummary) -> RunResourceSummary:
        return self._save(value, kind="run_resource_summary", resolution="terminal", hours=None)

    def _save(self, value: T, *, kind: str, resolution: str, hours: int | None) -> T:
        payload = _payload(value, kind=kind)
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        expires = (
            None
            if hours is None
            else _timestamp(_parse(value.captured_at) + timedelta(hours=hours))
        )
        now = _timestamp(self._now())
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO resource_observations (observation_id, kind, resolution, "
                "connection_id, owner, run_id, attempt, captured_at, expires_at, fencing_token, "
                "payload_sha256, payload_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT DO NOTHING",
                (
                    value.observation_id, kind, resolution, value.connection_id,
                    value.owner, value.run_id, value.attempt, value.captured_at, expires,
                    value.fencing_token, digest, encoded, now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM resource_observations WHERE observation_id = ?",
                (value.observation_id,),
            ).fetchone()
        if row is None:
            if kind == "run_resource_summary":
                raise ObservabilityConflict("immutable Run summary already exists")
            raise RuntimeError("observation insert did not produce a row")
        if row["payload_sha256"] != digest or row["resolution"] != resolution:
            raise ObservabilityConflict("immutable observation replay conflicts")
        return value

    def list_run_samples(self, run_id: str, *, owner: str) -> list[RunResourceSample]:
        return self._list_samples(run_id, owner=owner, resolution="raw")

    def list_minute_aggregates(self, run_id: str, *, owner: str) -> list[RunResourceSample]:
        return self._list_samples(run_id, owner=owner, resolution="minute")

    def _list_samples(self, run_id: str, *, owner: str, resolution: str) -> list[RunResourceSample]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM resource_observations WHERE run_id = ? AND owner = ? "
                "AND kind = 'run_resource_sample' AND resolution = ? "
                "ORDER BY captured_at, observation_id",
                (run_id, owner, resolution),
            ).fetchall()
        return [_sample_from_payload(json.loads(row["payload_json"])) for row in rows]

    def get_summary(self, run_id: str, *, owner: str) -> RunResourceSummary:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM resource_observations WHERE run_id = ? AND owner = ? "
                "AND kind = 'run_resource_summary' ORDER BY captured_at DESC LIMIT 1",
                (run_id, owner),
            ).fetchone()
        if row is None:
            raise KeyError(run_id)
        return _summary_from_payload(json.loads(row["payload_json"]))

    def prune_expired(self) -> int:
        with self.connect() as connection:
            removed = connection.execute(
                "DELETE FROM resource_observations "
                "WHERE expires_at IS NOT NULL AND expires_at <= ?",
                (_timestamp(self._now()),),
            )
        return removed.rowcount

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            raise ValueError("observability clock must be timezone-aware")
        return value.astimezone(UTC)


def _payload(
    value: PlatformPulse | AccountPulse | RunResourceSample | RunResourceSummary,
    *,
    kind: str,
) -> dict[str, object]:
    common: dict[str, object] = {
        "schema_version": "pilot107.resource-observation/v1",
        "observation_id": value.observation_id,
        "kind": kind,
        "connection_id": value.connection_id,
        "owner": value.owner,
        "run_id": value.run_id,
        "attempt": value.attempt,
        "cycle_id": value.cycle_id,
        "captured_at": value.captured_at,
        "freshness": value.freshness,
        "partial": value.partial,
        "warnings": list(value.warnings),
        "fencing_token": value.fencing_token,
    }
    if isinstance(value, RunResourceSummary):
        common["used"] = _measure_set_payload(value.used)
        common["allocated"] = _measure_set_payload(value.allocated)
    else:
        common["measures"] = _measure_set_payload(value.measures)
    return common


def _measure_set_payload(value: ResourceMeasureSet) -> dict[str, object]:
    return {name: measure.__dict__ for name, measure in value.as_dict().items()}


def _measure_set(value: dict[str, Any]) -> ResourceMeasureSet:
    known = {item for item in ResourceMeasureSet.__dataclass_fields__ if item != "extras"}
    parsed = {name: ObservedMeasure(**cast(dict[str, Any], raw)) for name, raw in value.items()}
    return ResourceMeasureSet(
        **{name: measure for name, measure in parsed.items() if name in known},
        extras=tuple((name, measure) for name, measure in parsed.items() if name not in known),
    )


def _sample_from_payload(value: dict[str, Any]) -> RunResourceSample:
    return RunResourceSample(
        **_common(value),
        measures=_measure_set(cast(dict[str, Any], value["measures"])),
    )


def _summary_from_payload(value: dict[str, Any]) -> RunResourceSummary:
    return RunResourceSummary(
        **_common(value),
        used=_measure_set(cast(dict[str, Any], value["used"])),
        allocated=_measure_set(cast(dict[str, Any], value["allocated"])),
    )


def _common(value: dict[str, Any]) -> dict[str, Any]:
    return {key: value[key] for key in (
        "observation_id", "connection_id", "owner", "run_id", "attempt", "cycle_id",
        "captured_at", "freshness", "partial", "warnings", "fencing_token"
    )}


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
