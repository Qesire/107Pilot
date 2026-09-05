"""Credential-free SSH connection metadata and user-facing status service."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock

from pilot107.adapters.ssh_relay import (
    SshRelayCheck,
    SshRelayClient,
    SshRelayConfig,
    SshSessionState,
)


@dataclass(frozen=True)
class SshConnectionRecord:
    connection_id: str
    portal_owner: str
    slurm_user: str
    target_id: str
    state: SshSessionState
    status_code: str
    message: str
    authenticated_at: str | None
    expires_at: str | None
    checked_at: str | None
    revision: int

    def public_payload(self) -> dict[str, str | int | None]:
        return {
            "connection_id": self.connection_id,
            "target_id": self.target_id,
            "state": self.state.value,
            "owner": "current-user-only",
            "checked_at": self.checked_at,
            "expires_at": self.expires_at,
            "message": self.message,
            "status_code": self.status_code,
            "revision": self.revision,
        }


class SshConnectionStore:
    """SQLite M1 store containing metadata only, never SSH credentials."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._migrate()

    def get(self, connection_id: str) -> SshConnectionRecord:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT connection_id, portal_owner, slurm_user, target_id, state,
                       status_code, message, authenticated_at, expires_at,
                       checked_at, revision
                FROM ssh_connection_sessions
                WHERE connection_id = ?
                """,
                (connection_id,),
            ).fetchone()
        if row is None:
            raise KeyError(connection_id)
        return _row_to_record(row)

    def save_check(
        self,
        *,
        config: SshRelayConfig,
        check: SshRelayCheck,
        expires_at: str | None = None,
    ) -> SshConnectionRecord:
        with self._lock, self._connect() as connection:
            existing = connection.execute(
                """
                SELECT state, authenticated_at, revision
                FROM ssh_connection_sessions
                WHERE connection_id = ?
                """,
                (config.connection_id,),
            ).fetchone()
            if existing is not None and existing["state"] == SshSessionState.REVOKED.value:
                return self.get(config.connection_id)
            authenticated_at = (
                check.checked_at
                if check.state == SshSessionState.ACTIVE
                and (existing is None or existing["state"] != SshSessionState.ACTIVE.value)
                else (None if existing is None else existing["authenticated_at"])
            )
            revision = 1 if existing is None else int(existing["revision"]) + 1
            connection.execute(
                """
                INSERT INTO ssh_connection_sessions (
                    connection_id, portal_owner, slurm_user, target_id, state,
                    status_code, message, authenticated_at, expires_at,
                    checked_at, revision
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(connection_id) DO UPDATE SET
                    portal_owner = excluded.portal_owner,
                    slurm_user = excluded.slurm_user,
                    target_id = excluded.target_id,
                    state = excluded.state,
                    status_code = excluded.status_code,
                    message = excluded.message,
                    authenticated_at = excluded.authenticated_at,
                    expires_at = excluded.expires_at,
                    checked_at = excluded.checked_at,
                    revision = excluded.revision
                """,
                (
                    config.connection_id,
                    config.portal_owner,
                    config.slurm_user,
                    config.target_id,
                    check.state.value,
                    check.status_code,
                    check.message,
                    authenticated_at,
                    expires_at,
                    check.checked_at,
                    revision,
                ),
            )
            connection.commit()
        return self.get(config.connection_id)

    def mark_state(
        self,
        connection_id: str,
        *,
        state: SshSessionState,
        status_code: str,
        message: str,
    ) -> SshConnectionRecord:
        if state not in {SshSessionState.REVOKED, SshSessionState.EXPIRED}:
            raise ValueError("mark_state only accepts revoked or expired")
        checked_at = datetime.now(UTC).isoformat()
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE ssh_connection_sessions
                SET state = ?, status_code = ?, message = ?, checked_at = ?,
                    revision = revision + 1
                WHERE connection_id = ?
                """,
                (state.value, status_code, message, checked_at, connection_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(connection_id)
            connection.commit()
        return self.get(connection_id)

    def _migrate(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS ssh_connection_sessions (
                    connection_id TEXT PRIMARY KEY,
                    portal_owner TEXT NOT NULL,
                    slurm_user TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    status_code TEXT NOT NULL,
                    message TEXT NOT NULL,
                    authenticated_at TEXT,
                    expires_at TEXT,
                    checked_at TEXT,
                    revision INTEGER NOT NULL CHECK(revision > 0),
                    CHECK(state IN (
                        'active', 'auth_required', 'revoked', 'expired', 'unavailable'
                    ))
                )
                """
            )
            connection.commit()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection


class SshConnectionService:
    def __init__(
        self,
        *,
        config: SshRelayConfig,
        client: SshRelayClient,
        store: SshConnectionStore,
    ) -> None:
        if client.config != config:
            raise ValueError("SSH connection service and relay config differ")
        self.config = config
        self.client = client
        self.store = store

    def list_for_owner(self, owner: str) -> list[SshConnectionRecord]:
        if owner != self.config.portal_owner:
            return []
        try:
            record = self.store.get(self.config.connection_id)
        except KeyError:
            record = self.store.save_check(
                config=self.config,
                check=SshRelayCheck(
                    state=SshSessionState.AUTH_REQUIRED,
                    checked_at=datetime.now(UTC).isoformat(),
                    status_code="SSH.NOT_CHECKED",
                    message="尚未验证真实算力平台连接",
                ),
            )
        return [record]

    def check_for_owner(self, owner: str, connection_id: str) -> SshConnectionRecord:
        if owner != self.config.portal_owner or connection_id != self.config.connection_id:
            raise KeyError(connection_id)
        try:
            current = self.store.get(connection_id)
        except KeyError:
            current = None
        if current is not None and current.state == SshSessionState.REVOKED:
            return current
        return self.store.save_check(config=self.config, check=self.client.check())


def _row_to_record(row: sqlite3.Row) -> SshConnectionRecord:
    return SshConnectionRecord(
        connection_id=str(row["connection_id"]),
        portal_owner=str(row["portal_owner"]),
        slurm_user=str(row["slurm_user"]),
        target_id=str(row["target_id"]),
        state=SshSessionState(str(row["state"])),
        status_code=str(row["status_code"]),
        message=str(row["message"]),
        authenticated_at=(
            None if row["authenticated_at"] is None else str(row["authenticated_at"])
        ),
        expires_at=None if row["expires_at"] is None else str(row["expires_at"]),
        checked_at=None if row["checked_at"] is None else str(row["checked_at"]),
        revision=int(row["revision"]),
    )
