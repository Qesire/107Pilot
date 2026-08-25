"""Persistence and owner-scoped read models for platform observations."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pilot107.core.pagination import CursorPosition
from pilot107.core.platform_migrations import PLATFORM_MIGRATIONS
from pilot107.core.platform_snapshot import (
    ObservationSourceType,
    PlatformSnapshot,
    PlatformSnapshotScope,
)
from pilot107.core.schema_migrations import apply_schema_migrations

_OWNER = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_SNAPSHOT_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
VM_SLURM_SOURCE_NAME = "vm-slurm"

class PlatformSnapshotStoreError(ValueError):
    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


class SnapshotFreshness(StrEnum):
    FRESH = "fresh"
    STALE = "stale"
    UNKNOWN = "unknown"


class SnapshotCollectionStatus(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"


@dataclass(frozen=True)
class PlatformSnapshotRecord:
    snapshot_id: str
    owner: str
    scope: PlatformSnapshotScope
    source_type: ObservationSourceType
    source_name: str
    collector_version: str
    captured_at: str
    expires_at: str | None
    collection_status: SnapshotCollectionStatus
    payload: dict[str, Any]
    content_sha256: str
    created_at: str

    def freshness(self, *, at: datetime | None = None) -> SnapshotFreshness:
        if self.expires_at is None:
            return SnapshotFreshness.UNKNOWN
        now = (at or datetime.now(UTC)).astimezone(UTC)
        expires = _parse_timestamp(self.expires_at, field="expires_at")
        return SnapshotFreshness.FRESH if expires > now else SnapshotFreshness.STALE

    def summary_payload(self, *, at: datetime | None = None) -> dict[str, Any]:
        command_results = self.payload.get("command_results", [])
        partitions = self.payload.get("partitions", [])
        nodes = self.payload.get("nodes", [])
        jobs = self.payload.get("squeue_jobs", [])
        limitations = self.payload.get("limitations", [])
        return {
            "snapshot_id": self.snapshot_id,
            "owner": self.owner,
            "scope": self.scope.value,
            "source_type": self.source_type.value,
            "source_name": self.source_name,
            "collector_version": self.collector_version,
            "captured_at": self.captured_at,
            "expires_at": self.expires_at,
            "freshness": self.freshness(at=at).value,
            "collection_status": self.collection_status.value,
            "content_sha256": self.content_sha256,
            "counts": {
                "commands": len(command_results) if isinstance(command_results, list) else 0,
                "partitions": len(partitions) if isinstance(partitions, list) else 0,
                "nodes": len(nodes) if isinstance(nodes, list) else 0,
                "jobs": len(jobs) if isinstance(jobs, list) else 0,
                "limitations": len(limitations) if isinstance(limitations, list) else 0,
            },
        }

    def safe_payload(self, *, at: datetime | None = None) -> dict[str, Any]:
        payload = json.loads(json.dumps(self.payload))
        command_results = payload.get("command_results")
        if isinstance(command_results, list):
            for command in command_results:
                if isinstance(command, dict):
                    command.pop("argv", None)
                    command.pop("stdout", None)
                    command.pop("stderr", None)
        return {**self.summary_payload(at=at), "snapshot": payload}


@dataclass(frozen=True)
class AuthoritativeSnapshotSelection:
    record: PlatformSnapshotRecord
    authority_id: str
    warnings: tuple[str, ...]


class PlatformSnapshotStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            apply_schema_migrations(conn, PLATFORM_MIGRATIONS)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 10000")
        return conn

    def create(
        self,
        *,
        owner: str,
        snapshot: PlatformSnapshot,
        source_type: ObservationSourceType,
        source_name: str,
        expires_at: str | None,
        idempotent: bool = True,
    ) -> PlatformSnapshotRecord:
        _validate_owner(owner)
        _validate_snapshot_id(snapshot.snapshot_id)
        if not source_name.strip() or len(source_name) > 256 or "\x00" in source_name:
            raise PlatformSnapshotStoreError(
                "source_name is invalid", code="PLATFORM_SNAPSHOT.SOURCE_INVALID"
            )
        captured = _parse_timestamp(snapshot.captured_at, field="captured_at")
        normalized_captured = captured.isoformat()
        normalized_expiry = None
        if expires_at is not None:
            expiry = _parse_timestamp(expires_at, field="expires_at")
            if expiry <= captured:
                raise PlatformSnapshotStoreError(
                    "expires_at must be after captured_at",
                    code="PLATFORM_SNAPSHOT.EXPIRY_INVALID",
                )
            normalized_expiry = expiry.isoformat()
        normalized_snapshot = replace(snapshot, captured_at=normalized_captured)
        payload = normalized_snapshot.to_payload()
        encoded = _canonical_json(payload)
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        status = (
            SnapshotCollectionStatus.PARTIAL
            if snapshot.limitations
            or any(item.returncode != 0 or item.timed_out for item in snapshot.command_results)
            else SnapshotCollectionStatus.COMPLETE
        )
        created_at = datetime.now(UTC).isoformat()
        values = (
            snapshot.snapshot_id,
            owner,
            snapshot.scope.value,
            source_type.value,
            source_name.strip(),
            snapshot.collector_version,
            normalized_captured,
            normalized_expiry,
            status.value,
            encoded,
            digest,
            created_at,
        )
        with self.connect() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO platform_snapshots (
                        snapshot_id, owner, scope, source_type, source_name,
                        collector_version, captured_at, expires_at,
                        collection_status, payload_json, content_sha256, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    values,
                )
            except sqlite3.IntegrityError as exc:
                if not idempotent:
                    raise PlatformSnapshotStoreError(
                        "snapshot ID already exists",
                        code="PLATFORM_SNAPSHOT.ALREADY_EXISTS",
                    ) from exc
        record = self.get(snapshot.snapshot_id, owner=owner)
        if idempotent and (
            record.content_sha256 != digest
            or record.scope != snapshot.scope
            or record.source_type != source_type
            or record.source_name != source_name.strip()
            or record.expires_at != normalized_expiry
        ):
            raise PlatformSnapshotStoreError(
                "snapshot ID refers to different content or metadata",
                code="PLATFORM_SNAPSHOT.IDEMPOTENCY_CONFLICT",
            )
        return record

    def get(self, snapshot_id: str, *, owner: str) -> PlatformSnapshotRecord:
        _validate_owner(owner)
        _validate_snapshot_id(snapshot_id)
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM platform_snapshots WHERE snapshot_id = ? AND owner = ?",
                (snapshot_id, owner),
            ).fetchone()
        if row is None:
            raise KeyError(snapshot_id)
        return _row_to_record(row)

    def latest(
        self,
        *,
        owner: str,
        scope: PlatformSnapshotScope | None = None,
    ) -> PlatformSnapshotRecord | None:
        _validate_owner(owner)
        conditions = ["owner = ?"]
        values: list[object] = [owner]
        if scope is not None:
            conditions.append("scope = ?")
            values.append(scope.value)
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM platform_snapshots WHERE "
                + " AND ".join(conditions)
                + " ORDER BY captured_at DESC, snapshot_id DESC LIMIT 1",
                values,
            ).fetchone()
        return None if row is None else _row_to_record(row)

    def latest_usable(
        self,
        *,
        owner: str,
        source_name: str = VM_SLURM_SOURCE_NAME,
        at: datetime | None = None,
    ) -> AuthoritativeSnapshotSelection | None:
        _validate_owner(owner)
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM platform_snapshots "
                "WHERE owner = ? AND source_name = ? "
                "ORDER BY captured_at DESC, snapshot_id DESC",
                (owner, source_name),
            ).fetchall()
        for row in rows:
            record = _row_to_record(row)
            if not _has_healthy_slurm_capacity(record):
                continue
            warnings: list[str] = []
            if record.collection_status is SnapshotCollectionStatus.PARTIAL:
                warnings.append("partial_ancillary_facts")
            freshness = record.freshness(at=at)
            if freshness is SnapshotFreshness.STALE:
                warnings.append("stale_authoritative_snapshot")
            return AuthoritativeSnapshotSelection(
                record=record,
                authority_id=source_name,
                warnings=tuple(warnings),
            )
        return None

    def list_page(
        self,
        *,
        owner: str,
        scope: PlatformSnapshotScope | None = None,
        source_type: ObservationSourceType | None = None,
        freshness: SnapshotFreshness | None = None,
        at: datetime | None = None,
        cursor: CursorPosition | None = None,
        limit: int = 50,
    ) -> tuple[list[PlatformSnapshotRecord], CursorPosition | None]:
        _validate_owner(owner)
        if limit <= 0 or limit > 100:
            raise ValueError("page limit must be between 1 and 100")
        now = (at or datetime.now(UTC)).astimezone(UTC).isoformat()
        conditions = ["owner = ?"]
        values: list[object] = [owner]
        if scope is not None:
            conditions.append("scope = ?")
            values.append(scope.value)
        if source_type is not None:
            conditions.append("source_type = ?")
            values.append(source_type.value)
        if freshness == SnapshotFreshness.FRESH:
            conditions.append("expires_at IS NOT NULL AND expires_at > ?")
            values.append(now)
        elif freshness == SnapshotFreshness.STALE:
            conditions.append("expires_at IS NOT NULL AND expires_at <= ?")
            values.append(now)
        elif freshness == SnapshotFreshness.UNKNOWN:
            conditions.append("expires_at IS NULL")
        if cursor is not None:
            conditions.append(
                "(captured_at < ? OR (captured_at = ? AND snapshot_id < ?))"
            )
            values.extend([cursor.primary, cursor.primary, cursor.secondary])
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM platform_snapshots WHERE "
                + " AND ".join(conditions)
                + " ORDER BY captured_at DESC, snapshot_id DESC LIMIT ?",
                (*values, limit + 1),
            ).fetchall()
        selected = rows[:limit]
        items = [_row_to_record(row) for row in selected]
        next_position = None
        if len(rows) > limit and selected:
            last = selected[-1]
            next_position = CursorPosition(
                primary=str(last["captured_at"]),
                secondary=str(last["snapshot_id"]),
            )
        return items, next_position


def _has_healthy_slurm_capacity(record: PlatformSnapshotRecord) -> bool:
    command_results = record.payload.get("command_results")
    partitions = record.payload.get("partitions")
    nodes = record.payload.get("nodes")
    if (
        not isinstance(command_results, list)
        or not isinstance(partitions, list)
        or not partitions
        or not isinstance(nodes, list)
        or not nodes
    ):
        return False
    statuses = {
        item.get("name"): item.get("returncode")
        for item in command_results
        if isinstance(item, dict)
        and isinstance(item.get("name"), str)
        and isinstance(item.get("returncode"), int)
        and not isinstance(item.get("returncode"), bool)
    }
    return statuses.get("scontrol_show_part") == 0 and (
        statuses.get("scontrol_show_nodes") == 0 or statuses.get("sinfo_pipe") == 0
    )


def _row_to_record(row: sqlite3.Row) -> PlatformSnapshotRecord:
    payload = json.loads(str(row["payload_json"]))
    if not isinstance(payload, dict):
        raise PlatformSnapshotStoreError(
            "stored snapshot payload is not an object",
            code="PLATFORM_SNAPSHOT.STORED_PAYLOAD_INVALID",
        )
    return PlatformSnapshotRecord(
        snapshot_id=str(row["snapshot_id"]),
        owner=str(row["owner"]),
        scope=PlatformSnapshotScope(str(row["scope"])),
        source_type=ObservationSourceType(str(row["source_type"])),
        source_name=str(row["source_name"]),
        collector_version=str(row["collector_version"]),
        captured_at=str(row["captured_at"]),
        expires_at=None if row["expires_at"] is None else str(row["expires_at"]),
        collection_status=SnapshotCollectionStatus(str(row["collection_status"])),
        payload=payload,
        content_sha256=str(row["content_sha256"]),
        created_at=str(row["created_at"]),
    )


def _parse_timestamp(value: str, *, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise PlatformSnapshotStoreError(
            f"{field} must be an ISO 8601 timestamp",
            code="PLATFORM_SNAPSHOT.TIMESTAMP_INVALID",
        ) from exc
    if parsed.tzinfo is None:
        raise PlatformSnapshotStoreError(
            f"{field} must include a timezone",
            code="PLATFORM_SNAPSHOT.TIMESTAMP_INVALID",
        )
    return parsed.astimezone(UTC)


def _validate_owner(owner: str) -> None:
    if not _OWNER.fullmatch(owner):
        raise PlatformSnapshotStoreError(
            "owner is invalid", code="PLATFORM_SNAPSHOT.OWNER_INVALID"
        )


def _validate_snapshot_id(snapshot_id: str) -> None:
    if not _SNAPSHOT_ID.fullmatch(snapshot_id):
        raise PlatformSnapshotStoreError(
            "snapshot_id is invalid", code="PLATFORM_SNAPSHOT.ID_INVALID"
        )


def _canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
