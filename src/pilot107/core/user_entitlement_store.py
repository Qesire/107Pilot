"""Persistence for owner-scoped user entitlement snapshots."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pilot107.core.pagination import CursorPosition
from pilot107.core.platform_migrations import PLATFORM_MIGRATIONS
from pilot107.core.platform_snapshot import ObservationSourceType
from pilot107.core.platform_snapshot_store import SnapshotFreshness
from pilot107.core.schema_migrations import apply_schema_migrations
from pilot107.core.user_entitlement import EntitlementDataQuality, UserEntitlementSnapshot

_OWNER = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_SNAPSHOT_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


class UserEntitlementStoreError(ValueError):
    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class UserEntitlementRecord:
    snapshot_id: str
    owner: str
    source_type: ObservationSourceType
    source_name: str
    collector_version: str
    captured_at: str
    expires_at: str | None
    data_quality: EntitlementDataQuality
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
        associations = self.payload.get("associations")
        return {
            "snapshot_id": self.snapshot_id,
            "owner": self.owner,
            "source_type": self.source_type.value,
            "source_name": self.source_name,
            "collector_version": self.collector_version,
            "captured_at": self.captured_at,
            "expires_at": self.expires_at,
            "freshness": self.freshness(at=at).value,
            "data_quality": self.data_quality.value,
            "default_account": self.payload.get("default_account"),
            "content_sha256": self.content_sha256,
            "association_count": len(associations) if isinstance(associations, list) else 0,
        }

    def safe_payload(self, *, at: datetime | None = None) -> dict[str, Any]:
        payload = json.loads(json.dumps(self.payload))
        commands = payload.get("command_results")
        if isinstance(commands, list):
            for command in commands:
                if isinstance(command, dict):
                    command.pop("argv", None)
                    command.pop("stdout", None)
                    command.pop("stderr", None)
        return {**self.summary_payload(at=at), "snapshot": payload}


class UserEntitlementStore:
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
        snapshot: UserEntitlementSnapshot,
        source_type: ObservationSourceType,
        source_name: str,
        expires_at: str | None,
        idempotent: bool = True,
    ) -> UserEntitlementRecord:
        _validate_owner(owner)
        _validate_snapshot_id(snapshot.snapshot_id)
        _validate_source_name(source_name)
        captured = _parse_timestamp(snapshot.captured_at, field="captured_at")
        normalized_expiry = None
        if expires_at is not None:
            expiry = _parse_timestamp(expires_at, field="expires_at")
            if expiry <= captured:
                raise UserEntitlementStoreError(
                    "expires_at must be after captured_at",
                    code="USER_ENTITLEMENT.EXPIRY_INVALID",
                )
            normalized_expiry = expiry.isoformat()
        normalized = replace(snapshot, captured_at=captured.isoformat())
        encoded = _canonical_json(normalized.to_payload())
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        values = (
            snapshot.snapshot_id,
            owner,
            source_type.value,
            source_name.strip(),
            snapshot.collector_version,
            captured.isoformat(),
            normalized_expiry,
            snapshot.data_quality.value,
            encoded,
            digest,
            datetime.now(UTC).isoformat(),
        )
        with self.connect() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO user_entitlement_snapshots (
                        snapshot_id, owner, source_type, source_name, collector_version,
                        captured_at, expires_at, data_quality, payload_json,
                        content_sha256, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    values,
                )
            except sqlite3.IntegrityError as exc:
                if not idempotent:
                    raise UserEntitlementStoreError(
                        "snapshot ID already exists",
                        code="USER_ENTITLEMENT.ALREADY_EXISTS",
                    ) from exc
        record = self.get(snapshot.snapshot_id, owner=owner)
        if idempotent and (
            record.content_sha256 != digest
            or record.source_type != source_type
            or record.source_name != source_name.strip()
            or record.expires_at != normalized_expiry
        ):
            raise UserEntitlementStoreError(
                "snapshot ID refers to different content or metadata",
                code="USER_ENTITLEMENT.IDEMPOTENCY_CONFLICT",
            )
        return record

    def get(self, snapshot_id: str, *, owner: str) -> UserEntitlementRecord:
        _validate_owner(owner)
        _validate_snapshot_id(snapshot_id)
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM user_entitlement_snapshots WHERE snapshot_id = ? AND owner = ?",
                (snapshot_id, owner),
            ).fetchone()
        if row is None:
            raise KeyError(snapshot_id)
        return _row_to_record(row)

    def latest(self, *, owner: str) -> UserEntitlementRecord | None:
        _validate_owner(owner)
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM user_entitlement_snapshots WHERE owner = ? "
                "ORDER BY captured_at DESC, snapshot_id DESC LIMIT 1",
                (owner,),
            ).fetchone()
        return None if row is None else _row_to_record(row)

    def list_page(
        self,
        *,
        owner: str,
        freshness: SnapshotFreshness | None = None,
        at: datetime | None = None,
        cursor: CursorPosition | None = None,
        limit: int = 50,
    ) -> tuple[list[UserEntitlementRecord], CursorPosition | None]:
        _validate_owner(owner)
        if limit <= 0 or limit > 100:
            raise ValueError("page limit must be between 1 and 100")
        now = (at or datetime.now(UTC)).astimezone(UTC).isoformat()
        conditions = ["owner = ?"]
        values: list[object] = [owner]
        if freshness == SnapshotFreshness.FRESH:
            conditions.append("expires_at IS NOT NULL AND expires_at > ?")
            values.append(now)
        elif freshness == SnapshotFreshness.STALE:
            conditions.append("expires_at IS NOT NULL AND expires_at <= ?")
            values.append(now)
        elif freshness == SnapshotFreshness.UNKNOWN:
            conditions.append("expires_at IS NULL")
        if cursor is not None:
            conditions.append("(captured_at < ? OR (captured_at = ? AND snapshot_id < ?))")
            values.extend((cursor.primary, cursor.primary, cursor.secondary))
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM user_entitlement_snapshots WHERE "
                + " AND ".join(conditions)
                + " ORDER BY captured_at DESC, snapshot_id DESC LIMIT ?",
                (*values, limit + 1),
            ).fetchall()
        selected = rows[:limit]
        records = [_row_to_record(row) for row in selected]
        next_position = None
        if len(rows) > limit and selected:
            last = selected[-1]
            next_position = CursorPosition(
                primary=str(last["captured_at"]),
                secondary=str(last["snapshot_id"]),
            )
        return records, next_position


def _row_to_record(row: sqlite3.Row) -> UserEntitlementRecord:
    payload = json.loads(str(row["payload_json"]))
    if not isinstance(payload, dict):
        raise UserEntitlementStoreError(
            "stored payload is not an object",
            code="USER_ENTITLEMENT.STORED_PAYLOAD_INVALID",
        )
    return UserEntitlementRecord(
        snapshot_id=str(row["snapshot_id"]),
        owner=str(row["owner"]),
        source_type=ObservationSourceType(str(row["source_type"])),
        source_name=str(row["source_name"]),
        collector_version=str(row["collector_version"]),
        captured_at=str(row["captured_at"]),
        expires_at=None if row["expires_at"] is None else str(row["expires_at"]),
        data_quality=EntitlementDataQuality(str(row["data_quality"])),
        payload=payload,
        content_sha256=str(row["content_sha256"]),
        created_at=str(row["created_at"]),
    )


def _parse_timestamp(value: str, *, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise UserEntitlementStoreError(
            f"{field} must be an ISO 8601 timestamp",
            code="USER_ENTITLEMENT.TIMESTAMP_INVALID",
        ) from exc
    if parsed.tzinfo is None:
        raise UserEntitlementStoreError(
            f"{field} must include a timezone",
            code="USER_ENTITLEMENT.TIMESTAMP_INVALID",
        )
    return parsed.astimezone(UTC)


def _validate_owner(owner: str) -> None:
    if not _OWNER.fullmatch(owner):
        raise UserEntitlementStoreError("owner is invalid", code="USER_ENTITLEMENT.OWNER_INVALID")


def _validate_snapshot_id(snapshot_id: str) -> None:
    if not _SNAPSHOT_ID.fullmatch(snapshot_id):
        raise UserEntitlementStoreError(
            "snapshot_id is invalid", code="USER_ENTITLEMENT.ID_INVALID"
        )


def _validate_source_name(source_name: str) -> None:
    if not source_name.strip() or len(source_name) > 256 or "\x00" in source_name:
        raise UserEntitlementStoreError(
            "source_name is invalid", code="USER_ENTITLEMENT.SOURCE_INVALID"
        )


def _canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
