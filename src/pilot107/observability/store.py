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
    ObservationCycle,
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
    SchemaMigration(
        migration_id="008a.002.observation_cycles_targets",
        statements=(
            """
            CREATE TABLE observation_cycles (
                cycle_id TEXT PRIMARY KEY,
                connection_id TEXT NOT NULL,
                lane TEXT NOT NULL,
                fencing_token INTEGER NOT NULL,
                scheduled_at TEXT NOT NULL,
                started_at TEXT NOT NULL,
                completed_at TEXT NOT NULL,
                command_count INTEGER NOT NULL,
                status TEXT NOT NULL,
                warnings_json TEXT NOT NULL,
                payload_sha256 TEXT NOT NULL,
                CHECK (fencing_token > 0),
                CHECK (command_count >= 0),
                CHECK (status IN ('complete', 'partial', 'failed', 'skipped_budget'))
            )
            """,
            "CREATE INDEX idx_observation_cycles_due "
            "ON observation_cycles(connection_id, lane, completed_at)",
            """
            CREATE TABLE observation_run_targets (
                connection_id TEXT NOT NULL,
                owner TEXT NOT NULL,
                run_id TEXT NOT NULL,
                job_id TEXT NOT NULL,
                attempt INTEGER NOT NULL,
                run_state TEXT NOT NULL,
                finalized INTEGER NOT NULL DEFAULT 0,
                last_observed_at TEXT,
                terminal_digest TEXT,
                terminal_stable_observations INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (owner, run_id),
                CHECK (attempt >= 0),
                CHECK (finalized IN (0, 1)),
                CHECK (terminal_stable_observations >= 0)
            )
            """,
            "CREATE INDEX idx_observation_targets_connection "
            "ON observation_run_targets(connection_id, finalized, last_observed_at, updated_at)",
        ),
    ),
)


class ObservabilityConflict(RuntimeError):
    """An observation ID was replayed with different immutable facts."""


class ObservabilityStore(Protocol):
    def save_platform_pulse(self, value: PlatformPulse) -> PlatformPulse: ...
    def save_account_pulse(self, value: AccountPulse) -> AccountPulse: ...
    def save_run_sample(self, value: RunResourceSample) -> RunResourceSample: ...
    def save_minute_aggregate(self, value: RunResourceSample) -> RunResourceSample: ...
    def save_summary(self, value: RunResourceSummary) -> RunResourceSummary: ...
    def get_latest_platform_pulse(
        self, connection_id: str, *, lane: str
    ) -> PlatformPulse: ...
    def get_latest_account_pulse(
        self, connection_id: str, *, owner: str
    ) -> AccountPulse: ...
    def list_account_pulses(
        self, connection_id: str, *, owner: str, limit: int
    ) -> list[AccountPulse]: ...
    def list_run_samples(self, run_id: str, *, owner: str) -> list[RunResourceSample]: ...
    def list_minute_aggregates(
        self, run_id: str, *, owner: str
    ) -> list[RunResourceSample]: ...
    def get_summary(self, run_id: str, *, owner: str) -> RunResourceSummary: ...
    def save_cycle(self, value: ObservationCycle) -> ObservationCycle: ...
    def latest_cycle(self, connection_id: str, *, lane: str) -> ObservationCycle | None: ...
    def count_cycle_commands_since(self, connection_id: str, *, since: str) -> int: ...
    def upsert_run_target(
        self, target: Any, *, state: str, observed_at: str
    ) -> None: ...
    def list_run_targets(self, connection_id: str) -> list[tuple[Any, str]]: ...
    def mark_run_target_observed(
        self, run_id: str, *, owner: str, observed_at: str
    ) -> None: ...
    def mark_run_target_finalized(
        self, run_id: str, *, owner: str, observed_at: str
    ) -> None: ...
    def record_terminal_observation(
        self,
        run_id: str,
        *,
        owner: str,
        digest: str,
        observed_at: str,
    ) -> int: ...
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

    def save_cycle(self, value: ObservationCycle) -> ObservationCycle:
        payload = {
            "cycle_id": value.cycle_id,
            "connection_id": value.connection_id,
            "lane": value.lane,
            "fencing_token": value.fencing_token,
            "scheduled_at": value.scheduled_at,
            "started_at": value.started_at,
            "completed_at": value.completed_at,
            "command_count": value.command_count,
            "status": value.status,
            "warnings": list(value.warnings),
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO observation_cycles (cycle_id, connection_id, lane, "
                "fencing_token, scheduled_at, started_at, completed_at, command_count, "
                "status, warnings_json, payload_sha256) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT DO NOTHING",
                (
                    value.cycle_id,
                    value.connection_id,
                    value.lane,
                    value.fencing_token,
                    value.scheduled_at,
                    value.started_at,
                    value.completed_at,
                    value.command_count,
                    value.status,
                    json.dumps(list(value.warnings), separators=(",", ":")),
                    digest,
                ),
            )
            row = connection.execute(
                "SELECT payload_sha256 FROM observation_cycles WHERE cycle_id = ?",
                (value.cycle_id,),
            ).fetchone()
        if row is None or str(row["payload_sha256"]) != digest:
            raise ObservabilityConflict("immutable observation cycle replay conflicts")
        return value

    def latest_cycle(self, connection_id: str, *, lane: str) -> ObservationCycle | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM observation_cycles WHERE connection_id = ? AND lane = ? "
                "ORDER BY completed_at DESC, cycle_id DESC LIMIT 1",
                (connection_id, lane),
            ).fetchone()
        return None if row is None else _cycle_from_row(row)

    def count_cycle_commands_since(self, connection_id: str, *, since: str) -> int:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT COALESCE(SUM(command_count), 0) AS commands "
                "FROM observation_cycles WHERE connection_id = ? AND completed_at >= ?",
                (connection_id, since),
            ).fetchone()
        return int(row["commands"]) if row is not None else 0

    def upsert_run_target(self, target: Any, *, state: str, observed_at: str) -> None:
        from pilot107.observability.adapters import RunObservationTarget

        if not isinstance(target, RunObservationTarget):
            raise TypeError("target must be RunObservationTarget")
        terminal = state in {
            "SUCCEEDED", "FAILED", "CANCELLED", "SUBMIT_FAILED",
            "COLLECTION_FAILED", "AUTH_REQUIRED", "ORPHANED",
        }
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO observation_run_targets (
                    connection_id, owner, run_id, job_id, attempt, run_state,
                    finalized, last_observed_at, terminal_digest,
                    terminal_stable_observations, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 0, NULL, NULL, 0, ?)
                ON CONFLICT(owner, run_id) DO UPDATE SET
                    connection_id = excluded.connection_id,
                    job_id = excluded.job_id,
                    attempt = excluded.attempt,
                    run_state = CASE
                        WHEN observation_run_targets.finalized = 1
                            THEN observation_run_targets.run_state
                        WHEN observation_run_targets.run_state IN (
                            'SUCCEEDED','FAILED','CANCELLED','SUBMIT_FAILED',
                            'COLLECTION_FAILED','AUTH_REQUIRED','ORPHANED'
                        ) AND excluded.run_state NOT IN (
                            'SUCCEEDED','FAILED','CANCELLED','SUBMIT_FAILED',
                            'COLLECTION_FAILED','AUTH_REQUIRED','ORPHANED'
                        ) THEN observation_run_targets.run_state
                        ELSE excluded.run_state
                    END,
                    updated_at = excluded.updated_at
                """,
                (
                    target.connection_id,
                    target.owner,
                    target.run_id,
                    target.job_id,
                    target.attempt,
                    state,
                    observed_at,
                ),
            )
        del terminal

    def list_run_targets(self, connection_id: str) -> list[tuple[Any, str]]:
        from pilot107.observability.adapters import RunObservationTarget

        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM observation_run_targets WHERE connection_id = ? "
                "AND finalized = 0 ORDER BY last_observed_at IS NOT NULL, "
                "last_observed_at, updated_at, owner, run_id",
                (connection_id,),
            ).fetchall()
        return [
            (
                RunObservationTarget(
                    connection_id=str(row["connection_id"]),
                    owner=str(row["owner"]),
                    run_id=str(row["run_id"]),
                    job_id=str(row["job_id"]),
                    attempt=int(row["attempt"]),
                ),
                str(row["run_state"]),
            )
            for row in rows
        ]

    def mark_run_target_observed(
        self, run_id: str, *, owner: str, observed_at: str
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE observation_run_targets SET last_observed_at = ?, updated_at = ? "
                "WHERE owner = ? AND run_id = ? AND finalized = 0",
                (observed_at, observed_at, owner, run_id),
            )

    def mark_run_target_finalized(
        self, run_id: str, *, owner: str, observed_at: str
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE observation_run_targets SET finalized = 1, "
                "last_observed_at = ?, updated_at = ? WHERE owner = ? AND run_id = ?",
                (observed_at, observed_at, owner, run_id),
            )

    def record_terminal_observation(
        self,
        run_id: str,
        *,
        owner: str,
        digest: str,
        observed_at: str,
    ) -> int:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT terminal_digest, terminal_stable_observations "
                "FROM observation_run_targets WHERE owner = ? AND run_id = ? "
                "AND finalized = 0",
                (owner, run_id),
            ).fetchone()
            if row is None:
                raise KeyError(run_id)
            stable = (
                int(row["terminal_stable_observations"]) + 1
                if row["terminal_digest"] == digest
                else 1
            )
            connection.execute(
                "UPDATE observation_run_targets SET terminal_digest = ?, "
                "terminal_stable_observations = ?, last_observed_at = ?, updated_at = ? "
                "WHERE owner = ? AND run_id = ? AND finalized = 0",
                (digest, stable, observed_at, observed_at, owner, run_id),
            )
        return stable

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

    def get_latest_platform_pulse(
        self, connection_id: str, *, lane: str
    ) -> PlatformPulse:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM resource_observations "
                "WHERE kind = 'platform_pulse' AND connection_id = ? "
                "ORDER BY captured_at DESC, observation_id DESC",
                (connection_id,),
            ).fetchall()
            for row in rows:
                payload = json.loads(row["payload_json"])
                match = connection.execute(
                    "SELECT cycle_id FROM observation_cycles "
                    "WHERE cycle_id = ? AND lane = ?",
                    (payload["cycle_id"], lane),
                ).fetchone()
                if match is not None:
                    return _platform_from_payload(payload)
        raise KeyError(connection_id)

    def get_latest_account_pulse(
        self, connection_id: str, *, owner: str
    ) -> AccountPulse:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM resource_observations "
                "WHERE kind = 'account_pulse' AND connection_id = ? AND owner = ? "
                "ORDER BY captured_at DESC, observation_id DESC LIMIT 1",
                (connection_id, owner),
            ).fetchone()
        if row is None:
            raise KeyError(connection_id)
        return _account_from_payload(json.loads(row["payload_json"]))

    def list_account_pulses(
        self, connection_id: str, *, owner: str, limit: int
    ) -> list[AccountPulse]:
        if isinstance(limit, bool) or not 1 <= limit <= 1000:
            raise ValueError("account pulse limit is invalid")
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM resource_observations "
                "WHERE kind = 'account_pulse' AND connection_id = ? AND owner = ? "
                "ORDER BY captured_at DESC, observation_id DESC LIMIT ?",
                (connection_id, owner, limit),
            ).fetchall()
        return [
            _account_from_payload(json.loads(row["payload_json"]))
            for row in reversed(rows)
        ]

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


def _platform_from_payload(value: dict[str, Any]) -> PlatformPulse:
    return PlatformPulse(
        **_common(value),
        measures=_measure_set(cast(dict[str, Any], value["measures"])),
    )


def _account_from_payload(value: dict[str, Any]) -> AccountPulse:
    return AccountPulse(
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


def _cycle_from_row(row: sqlite3.Row) -> ObservationCycle:
    return ObservationCycle(
        cycle_id=str(row["cycle_id"]),
        connection_id=str(row["connection_id"]),
        lane=str(row["lane"]),
        fencing_token=int(row["fencing_token"]),
        scheduled_at=str(row["scheduled_at"]),
        started_at=str(row["started_at"]),
        completed_at=str(row["completed_at"]),
        command_count=int(row["command_count"]),
        status=cast(Any, str(row["status"])),
        warnings=tuple(cast(list[str], json.loads(str(row["warnings_json"])))),
    )


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
