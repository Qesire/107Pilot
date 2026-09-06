"""Durable Launch planning and commit records.

Launch is intentionally a control/provenance layer over the existing Run
execution authority.  It records the reviewed candidate, the deterministic
preflight snapshot and the explicit user commit.  Slurm submission and runtime
state remain authoritative in :mod:`pilot107.core.run_service` and RunStore.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pilot107.core.identity import is_safe_username
from pilot107.core.postgres_domain_schema import initialize_postgres_domain_schema

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")
_PREFLIGHT_STATES = frozenset({"OK", "BLOCK"})

_MIGRATION_ID = "007a.001.launch_authority"
_MIGRATION_STATEMENTS = (
    """
    CREATE TABLE launch_candidates (
        candidate_id TEXT PRIMARY KEY,
        workarea_id TEXT NOT NULL REFERENCES workareas(workarea_id) ON DELETE CASCADE,
        owner TEXT NOT NULL,
        request_key TEXT NOT NULL,
        contract_id TEXT NOT NULL REFERENCES contracts(contract_id),
        title TEXT NOT NULL DEFAULT '',
        note TEXT NOT NULL DEFAULT '',
        candidate_digest TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL,
        UNIQUE(owner, request_key)
    )
    """,
    """
    CREATE INDEX idx_launch_candidates_workarea_updated
    ON launch_candidates(workarea_id, updated_at DESC, candidate_id DESC)
    """,
    """
    CREATE TABLE launch_preflights (
        preflight_id TEXT PRIMARY KEY,
        candidate_id TEXT NOT NULL REFERENCES launch_candidates(candidate_id) ON DELETE CASCADE,
        owner TEXT NOT NULL,
        candidate_digest TEXT NOT NULL,
        status TEXT NOT NULL,
        findings_json TEXT NOT NULL,
        effective_request_json TEXT NOT NULL,
        assessment_digest TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL,
        CHECK (status IN ('OK', 'BLOCK'))
    )
    """,
    """
    CREATE INDEX idx_launch_preflights_candidate_created
    ON launch_preflights(candidate_id, created_at DESC, preflight_id DESC)
    """,
    """
    CREATE TABLE launches (
        launch_id TEXT PRIMARY KEY,
        candidate_id TEXT NOT NULL UNIQUE REFERENCES launch_candidates(candidate_id),
        preflight_id TEXT NOT NULL REFERENCES launch_preflights(preflight_id),
        workarea_id TEXT NOT NULL REFERENCES workareas(workarea_id) ON DELETE CASCADE,
        owner TEXT NOT NULL,
        contract_id TEXT NOT NULL REFERENCES contracts(contract_id),
        request_key TEXT NOT NULL,
        candidate_digest TEXT NOT NULL,
        preflight_digest TEXT NOT NULL,
        committed_at TIMESTAMPTZ NOT NULL,
        submitted_at TIMESTAMPTZ,
        submit_error_json TEXT,
        UNIQUE(owner, request_key)
    )
    """,
    """
    CREATE INDEX idx_launches_workarea_committed
    ON launches(workarea_id, committed_at DESC, launch_id DESC)
    """,
    """
    CREATE TABLE launch_runs (
        launch_id TEXT NOT NULL REFERENCES launches(launch_id) ON DELETE CASCADE,
        run_id TEXT NOT NULL UNIQUE REFERENCES runs(run_id),
        ordinal INTEGER NOT NULL,
        linked_at TIMESTAMPTZ NOT NULL,
        PRIMARY KEY(launch_id, ordinal),
        CHECK (ordinal >= 0)
    )
    """,
    """
    CREATE INDEX idx_launch_runs_launch
    ON launch_runs(launch_id, ordinal, run_id)
    """,
)


class LaunchConflict(RuntimeError):
    """Raised when a retry no longer matches the reviewed Launch snapshot."""


@dataclass(frozen=True)
class LaunchCandidateRecord:
    candidate_id: str
    workarea_id: str
    owner: str
    request_key: str
    contract_id: str
    title: str
    note: str
    candidate_digest: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class LaunchPreflightRecord:
    preflight_id: str
    candidate_id: str
    owner: str
    candidate_digest: str
    status: str
    findings: tuple[dict[str, Any], ...]
    effective_request: dict[str, Any]
    assessment_digest: str
    created_at: str


@dataclass(frozen=True)
class LaunchRecord:
    launch_id: str
    candidate_id: str
    preflight_id: str
    workarea_id: str
    owner: str
    contract_id: str
    request_key: str
    candidate_digest: str
    preflight_digest: str
    committed_at: str
    submitted_at: str | None
    submit_error: dict[str, Any] | None
    run_ids: tuple[str, ...] = ()


class PostgresLaunchStore:
    """PostgreSQL authority for candidate/review/commit provenance."""

    def __init__(self, dsn: str) -> None:
        if not dsn or any(character in dsn for character in "\r\n\0"):
            raise ValueError("PostgreSQL DSN is invalid")
        self.dsn = dsn
        try:
            self._psycopg = importlib.import_module("psycopg")
            self._dict_row = importlib.import_module("psycopg.rows").dict_row
        except ModuleNotFoundError as exc:
            raise RuntimeError("install pilot107[postgres] to use Launch") from exc
        initialize_postgres_domain_schema(dsn)
        self._ensure_schema()

    def connect(self) -> Any:
        return self._psycopg.connect(self.dsn, row_factory=self._dict_row)

    def create_candidate(
        self,
        *,
        workarea_id: str,
        owner: str,
        request_key: str,
        contract_id: str,
        title: str = "",
        note: str = "",
    ) -> LaunchCandidateRecord:
        _owner(owner)
        _key(workarea_id, "workarea_id")
        _key(request_key, "request_key")
        _key(contract_id, "contract_id")
        title = _text(title, "title", 256)
        note = _text(note, "note", 8_000)
        payload = {
            "workarea_id": workarea_id,
            "owner": owner,
            "contract_id": contract_id,
            "title": title,
            "note": note,
        }
        digest = _digest(payload)
        candidate_id = f"launchcand-{hashlib.sha256(f'{owner}\0{request_key}'.encode()).hexdigest()[:24]}"
        now = datetime.now(UTC)
        with self.connect() as connection, connection.transaction():
            row = connection.execute(
                """
                INSERT INTO launch_candidates (
                    candidate_id, workarea_id, owner, request_key, contract_id,
                    title, note, candidate_digest, created_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (owner, request_key) DO NOTHING
                RETURNING *
                """,
                (
                    candidate_id, workarea_id, owner, request_key, contract_id,
                    title, note, digest, now, now,
                ),
            ).fetchone()
            if row is None:
                row = connection.execute(
                    "SELECT * FROM launch_candidates WHERE owner = %s AND request_key = %s",
                    (owner, request_key),
                ).fetchone()
            if row is None:
                raise RuntimeError("LaunchCandidate idempotent create disappeared")
            record = _candidate_from_row(row)
            if record.candidate_digest != digest:
                raise LaunchConflict("request_key refers to a different LaunchCandidate")
            return record

    def get_candidate(self, candidate_id: str, *, owner: str) -> LaunchCandidateRecord:
        _key(candidate_id, "candidate_id")
        _owner(owner)
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM launch_candidates WHERE candidate_id = %s AND owner = %s",
                (candidate_id, owner),
            ).fetchone()
        if row is None:
            raise KeyError(candidate_id)
        return _candidate_from_row(row)

    def list_candidates(
        self,
        *,
        workarea_id: str,
        owner: str,
        limit: int = 100,
    ) -> list[LaunchCandidateRecord]:
        _key(workarea_id, "workarea_id")
        _owner(owner)
        _limit(limit)
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM launch_candidates
                WHERE workarea_id = %s AND owner = %s
                ORDER BY updated_at DESC, candidate_id DESC LIMIT %s
                """,
                (workarea_id, owner, limit),
            ).fetchall()
        return [_candidate_from_row(row) for row in rows]

    def save_preflight(
        self,
        *,
        candidate: LaunchCandidateRecord,
        status: str,
        findings: list[dict[str, Any]] | tuple[dict[str, Any], ...],
        effective_request: dict[str, Any],
    ) -> LaunchPreflightRecord:
        if status not in _PREFLIGHT_STATES:
            raise ValueError("preflight status must be OK or BLOCK")
        canonical_findings = [dict(item) for item in findings]
        assessment_payload = {
            "candidate_digest": candidate.candidate_digest,
            "status": status,
            "findings": canonical_findings,
            "effective_request": effective_request,
        }
        assessment_digest = _digest(assessment_payload)
        preflight_id = f"preflight-{assessment_digest[:24]}"
        now = datetime.now(UTC)
        with self.connect() as connection, connection.transaction():
            row = connection.execute(
                """
                INSERT INTO launch_preflights (
                    preflight_id, candidate_id, owner, candidate_digest, status,
                    findings_json, effective_request_json, assessment_digest, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (preflight_id) DO NOTHING
                RETURNING *
                """,
                (
                    preflight_id,
                    candidate.candidate_id,
                    candidate.owner,
                    candidate.candidate_digest,
                    status,
                    _canonical(canonical_findings),
                    _canonical(effective_request),
                    assessment_digest,
                    now,
                ),
            ).fetchone()
            if row is None:
                row = connection.execute(
                    "SELECT * FROM launch_preflights WHERE preflight_id = %s",
                    (preflight_id,),
                ).fetchone()
            if row is None:
                raise RuntimeError("Launch preflight disappeared")
        return _preflight_from_row(row)

    def get_preflight(self, preflight_id: str, *, owner: str) -> LaunchPreflightRecord:
        _key(preflight_id, "preflight_id")
        _owner(owner)
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM launch_preflights WHERE preflight_id = %s AND owner = %s",
                (preflight_id, owner),
            ).fetchone()
        if row is None:
            raise KeyError(preflight_id)
        return _preflight_from_row(row)

    def latest_preflight(
        self,
        candidate_id: str,
        *,
        owner: str,
    ) -> LaunchPreflightRecord | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM launch_preflights
                WHERE candidate_id = %s AND owner = %s
                ORDER BY created_at DESC, preflight_id DESC LIMIT 1
                """,
                (candidate_id, owner),
            ).fetchone()
        return None if row is None else _preflight_from_row(row)

    def commit(
        self,
        *,
        candidate: LaunchCandidateRecord,
        preflight: LaunchPreflightRecord,
        request_key: str,
    ) -> LaunchRecord:
        _key(request_key, "request_key")
        if preflight.status != "OK":
            raise LaunchConflict("a blocked preflight cannot be committed")
        if preflight.candidate_id != candidate.candidate_id:
            raise LaunchConflict("preflight does not belong to candidate")
        if preflight.candidate_digest != candidate.candidate_digest:
            raise LaunchConflict("preflight is stale for candidate")
        launch_id = f"launch-{hashlib.sha256(f'{candidate.owner}\0{request_key}'.encode()).hexdigest()[:24]}"
        now = datetime.now(UTC)
        with self.connect() as connection, connection.transaction():
            row = connection.execute(
                """
                INSERT INTO launches (
                    launch_id, candidate_id, preflight_id, workarea_id, owner,
                    contract_id, request_key, candidate_digest, preflight_digest,
                    committed_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (owner, request_key) DO NOTHING
                RETURNING *
                """,
                (
                    launch_id,
                    candidate.candidate_id,
                    preflight.preflight_id,
                    candidate.workarea_id,
                    candidate.owner,
                    candidate.contract_id,
                    request_key,
                    candidate.candidate_digest,
                    preflight.assessment_digest,
                    now,
                ),
            ).fetchone()
            if row is None:
                row = connection.execute(
                    "SELECT * FROM launches WHERE owner = %s AND request_key = %s",
                    (candidate.owner, request_key),
                ).fetchone()
            if row is None:
                raise RuntimeError("Launch idempotent commit disappeared")
            record = _launch_from_row(row, ())
            if (
                record.candidate_id != candidate.candidate_id
                or record.preflight_digest != preflight.assessment_digest
            ):
                raise LaunchConflict("commit request_key refers to another reviewed Launch")
            return record

    def attach_run(self, launch_id: str, *, owner: str, run_id: str, ordinal: int = 0) -> None:
        launch = self.get(launch_id, owner=owner)
        _key(run_id, "run_id")
        if ordinal < 0:
            raise ValueError("ordinal must be >= 0")
        now = datetime.now(UTC)
        with self.connect() as connection, connection.transaction():
            run = connection.execute(
                "SELECT owner FROM runs WHERE run_id = %s",
                (run_id,),
            ).fetchone()
            if run is None:
                raise KeyError(run_id)
            if str(run["owner"]) != owner:
                raise LaunchConflict("Run owner does not match Launch")
            existing = connection.execute(
                "SELECT launch_id, ordinal FROM launch_runs WHERE run_id = %s",
                (run_id,),
            ).fetchone()
            if existing is not None:
                if str(existing["launch_id"]) != launch.launch_id:
                    raise LaunchConflict("Run is already attached to another Launch")
                return
            connection.execute(
                """
                INSERT INTO launch_runs (launch_id, run_id, ordinal, linked_at)
                VALUES (%s, %s, %s, %s)
                """,
                (launch_id, run_id, ordinal, now),
            )

    def mark_submitted(self, launch_id: str, *, owner: str) -> None:
        now = datetime.now(UTC)
        with self.connect() as connection, connection.transaction():
            result = connection.execute(
                """
                UPDATE launches SET submitted_at = COALESCE(submitted_at, %s), submit_error_json = NULL
                WHERE launch_id = %s AND owner = %s
                """,
                (now, launch_id, owner),
            )
            if result.rowcount != 1:
                raise KeyError(launch_id)

    def mark_submit_error(
        self,
        launch_id: str,
        *,
        owner: str,
        error: dict[str, Any],
    ) -> None:
        with self.connect() as connection, connection.transaction():
            result = connection.execute(
                """
                UPDATE launches SET submit_error_json = %s
                WHERE launch_id = %s AND owner = %s
                """,
                (_canonical(error), launch_id, owner),
            )
            if result.rowcount != 1:
                raise KeyError(launch_id)

    def get(self, launch_id: str, *, owner: str) -> LaunchRecord:
        _key(launch_id, "launch_id")
        _owner(owner)
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM launches WHERE launch_id = %s AND owner = %s",
                (launch_id, owner),
            ).fetchone()
            if row is None:
                raise KeyError(launch_id)
            run_rows = connection.execute(
                """
                SELECT run_id FROM launch_runs WHERE launch_id = %s
                ORDER BY ordinal, run_id
                """,
                (launch_id,),
            ).fetchall()
        return _launch_from_row(row, tuple(str(item["run_id"]) for item in run_rows))

    def list_for_workarea(
        self,
        *,
        workarea_id: str,
        owner: str,
        limit: int = 100,
    ) -> list[LaunchRecord]:
        _key(workarea_id, "workarea_id")
        _owner(owner)
        _limit(limit)
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM launches WHERE workarea_id = %s AND owner = %s
                ORDER BY committed_at DESC, launch_id DESC LIMIT %s
                """,
                (workarea_id, owner, limit),
            ).fetchall()
            result: list[LaunchRecord] = []
            for row in rows:
                run_rows = connection.execute(
                    "SELECT run_id FROM launch_runs WHERE launch_id = %s ORDER BY ordinal, run_id",
                    (row["launch_id"],),
                ).fetchall()
                result.append(
                    _launch_from_row(row, tuple(str(item["run_id"]) for item in run_rows))
                )
        return result

    def _ensure_schema(self) -> None:
        checksum = _migration_checksum(_MIGRATION_STATEMENTS)
        with self.connect() as connection, connection.transaction():
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                ("pilot107:migrations",),
            )
            existing = connection.execute(
                "SELECT checksum FROM schema_migrations WHERE migration_id = %s",
                (_MIGRATION_ID,),
            ).fetchone()
            if existing is not None:
                if str(existing["checksum"]) != checksum:
                    raise RuntimeError(f"migration checksum changed: {_MIGRATION_ID}")
                return
            for statement in _MIGRATION_STATEMENTS:
                connection.execute(statement)
            connection.execute(
                """
                INSERT INTO schema_migrations (migration_id, checksum, applied_at)
                VALUES (%s, %s, %s)
                """,
                (_MIGRATION_ID, checksum, datetime.now(UTC)),
            )


def candidate_payload(record: LaunchCandidateRecord, preflight: LaunchPreflightRecord | None = None) -> dict[str, Any]:
    return {
        "candidate_id": record.candidate_id,
        "workarea_id": record.workarea_id,
        "owner": record.owner,
        "contract_id": record.contract_id,
        "title": record.title,
        "note": record.note,
        "candidate_digest": record.candidate_digest,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "preflight": None if preflight is None else preflight_payload(preflight),
    }


def preflight_payload(record: LaunchPreflightRecord) -> dict[str, Any]:
    return {
        "preflight_id": record.preflight_id,
        "candidate_id": record.candidate_id,
        "candidate_digest": record.candidate_digest,
        "status": record.status,
        "findings": list(record.findings),
        "effective_request": record.effective_request,
        "assessment_digest": record.assessment_digest,
        "created_at": record.created_at,
    }


def launch_payload(record: LaunchRecord) -> dict[str, Any]:
    return {
        "launch_id": record.launch_id,
        "candidate_id": record.candidate_id,
        "preflight_id": record.preflight_id,
        "workarea_id": record.workarea_id,
        "owner": record.owner,
        "contract_id": record.contract_id,
        "candidate_digest": record.candidate_digest,
        "preflight_digest": record.preflight_digest,
        "committed_at": record.committed_at,
        "submitted_at": record.submitted_at,
        "submit_error": record.submit_error,
        "run_ids": list(record.run_ids),
    }


def _candidate_from_row(row: Any) -> LaunchCandidateRecord:
    return LaunchCandidateRecord(
        candidate_id=str(row["candidate_id"]),
        workarea_id=str(row["workarea_id"]),
        owner=str(row["owner"]),
        request_key=str(row["request_key"]),
        contract_id=str(row["contract_id"]),
        title=str(row["title"]),
        note=str(row["note"]),
        candidate_digest=str(row["candidate_digest"]),
        created_at=_timestamp(row["created_at"]),
        updated_at=_timestamp(row["updated_at"]),
    )


def _preflight_from_row(row: Any) -> LaunchPreflightRecord:
    return LaunchPreflightRecord(
        preflight_id=str(row["preflight_id"]),
        candidate_id=str(row["candidate_id"]),
        owner=str(row["owner"]),
        candidate_digest=str(row["candidate_digest"]),
        status=str(row["status"]),
        findings=tuple(json.loads(str(row["findings_json"]))),
        effective_request=json.loads(str(row["effective_request_json"])),
        assessment_digest=str(row["assessment_digest"]),
        created_at=_timestamp(row["created_at"]),
    )


def _launch_from_row(row: Any, run_ids: tuple[str, ...]) -> LaunchRecord:
    raw_error = row["submit_error_json"]
    return LaunchRecord(
        launch_id=str(row["launch_id"]),
        candidate_id=str(row["candidate_id"]),
        preflight_id=str(row["preflight_id"]),
        workarea_id=str(row["workarea_id"]),
        owner=str(row["owner"]),
        contract_id=str(row["contract_id"]),
        request_key=str(row["request_key"]),
        candidate_digest=str(row["candidate_digest"]),
        preflight_digest=str(row["preflight_digest"]),
        committed_at=_timestamp(row["committed_at"]),
        submitted_at=None if row["submitted_at"] is None else _timestamp(row["submitted_at"]),
        submit_error=None if raw_error is None else json.loads(str(raw_error)),
        run_ids=run_ids,
    )


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _migration_checksum(statements: tuple[str, ...]) -> str:
    digest = hashlib.sha256()
    for statement in statements:
        digest.update(statement.strip().encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def _timestamp(value: Any) -> str:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return str(value)


def _owner(value: str) -> None:
    if not is_safe_username(value):
        raise ValueError("owner is invalid")


def _key(value: str, field: str) -> None:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{field} is invalid")


def _text(value: str, field: str, limit: int) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string")
    value = value.strip()
    if "\0" in value or len(value) > limit:
        raise ValueError(f"{field} is invalid")
    return value


def _limit(value: int) -> None:
    if value <= 0 or value > 500:
        raise ValueError("limit must be between 1 and 500")


__all__ = [
    "LaunchCandidateRecord",
    "LaunchConflict",
    "LaunchPreflightRecord",
    "LaunchRecord",
    "PostgresLaunchStore",
    "candidate_payload",
    "launch_payload",
    "preflight_payload",
]
