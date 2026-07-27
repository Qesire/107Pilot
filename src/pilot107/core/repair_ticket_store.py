"""SQLite persistence for repair tickets and artifact manifests."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pilot107.core.repair_ticket import (
    ArtifactManifest,
    RepairTicket,
    RepairTicketConflict,
    RepairTicketState,
    assert_ticket_transition,
)
from pilot107.core.repair_ticket_migrations import REPAIR_TICKET_MIGRATIONS
from pilot107.core.schema_migrations import apply_schema_migrations


def _now() -> str:
    return datetime.now(UTC).isoformat()


class RepairTicketStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            apply_schema_migrations(conn, REPAIR_TICKET_MIGRATIONS)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    # ------------------------------------------------------------------
    # ArtifactManifest
    # ------------------------------------------------------------------

    def create_manifest(self, manifest: ArtifactManifest) -> ArtifactManifest:
        now = manifest.created_at or _now()
        final = ArtifactManifest(
            manifest_id=manifest.manifest_id,
            owner=manifest.owner,
            run_id=manifest.run_id,
            revision=manifest.revision,
            dirty_diff_digest=manifest.dirty_diff_digest,
            bundle_digest=manifest.bundle_digest,
            remote_workdir=manifest.remote_workdir,
            local_test_summary=manifest.local_test_summary,
            disclosure=manifest.disclosure,
            created_at=now,
        )
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO artifact_manifests (
                    manifest_id, owner, run_id, revision, dirty_diff_digest,
                    bundle_digest, remote_workdir, local_test_summary,
                    disclosure, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    final.manifest_id,
                    final.owner,
                    final.run_id,
                    final.revision,
                    final.dirty_diff_digest,
                    final.bundle_digest,
                    final.remote_workdir,
                    final.local_test_summary,
                    final.disclosure,
                    final.created_at,
                ),
            )
        return final

    def get_manifest(self, manifest_id: str) -> ArtifactManifest:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM artifact_manifests WHERE manifest_id = ?",
                (manifest_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"artifact manifest not found: {manifest_id}")
        return _manifest_from_row(row)

    def list_manifests_for_run(self, run_id: str) -> list[ArtifactManifest]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM artifact_manifests WHERE run_id = ? ORDER BY created_at DESC",
                (run_id,),
            ).fetchall()
        return [_manifest_from_row(row) for row in rows]

    def bind_manifest_to_run(self, manifest_id: str, run_id: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE artifact_manifests SET run_id = ? WHERE manifest_id = ?",
                (run_id, manifest_id),
            )

    # ------------------------------------------------------------------
    # RepairTicket
    # ------------------------------------------------------------------

    def create_ticket(self, ticket: RepairTicket) -> RepairTicket:
        now = ticket.created_at or _now()
        final = RepairTicket(
            ticket_id=ticket.ticket_id,
            owner=ticket.owner,
            state=RepairTicketState.OPEN,
            source_run_id=ticket.source_run_id,
            source_contract_id=ticket.source_contract_id,
            session_id=ticket.session_id,
            diagnosis_ids=ticket.diagnosis_ids,
            cited_facts=ticket.cited_facts,
            code_context=ticket.code_context,
            requested_change=ticket.requested_change,
            no_go_constraints=ticket.no_go_constraints,
            created_at=now,
            updated_at=now,
        )
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO repair_tickets (
                    ticket_id, owner, state, source_run_id, source_contract_id,
                    session_id, diagnosis_ids_json, cited_facts_json,
                    code_context_json, requested_change, no_go_constraints_json,
                    resolution_manifest_id, resolution_run_id,
                    resolution_comparison_json, abandon_reason,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, ?, ?)
                """,
                (
                    final.ticket_id,
                    final.owner,
                    final.state.value,
                    final.source_run_id,
                    final.source_contract_id,
                    final.session_id,
                    json.dumps(list(final.diagnosis_ids)),
                    json.dumps(list(final.cited_facts)),
                    json.dumps(final.code_context) if final.code_context else None,
                    final.requested_change,
                    json.dumps(list(final.no_go_constraints)),
                    final.created_at,
                    final.updated_at,
                ),
            )
        return final

    def get_ticket(self, ticket_id: str) -> RepairTicket:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM repair_tickets WHERE ticket_id = ?",
                (ticket_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"repair ticket not found: {ticket_id}")
        return _ticket_from_row(row)

    def list_tickets_page(
        self,
        *,
        owner: str,
        states: frozenset[RepairTicketState] | None = None,
        session_id: str | None = None,
        before: tuple[str, str] | None = None,
        limit: int = 20,
    ) -> tuple[list[RepairTicket], tuple[str, str] | None]:
        """Paginated ticket listing with cursor (updated_at, ticket_id)."""
        conditions = ["owner = ?"]
        params: list[Any] = [owner]
        if states:
            placeholders = ",".join("?" for _ in states)
            conditions.append(f"state IN ({placeholders})")
            params.extend(s.value for s in states)
        if session_id:
            conditions.append("session_id = ?")
            params.append(session_id)
        if before:
            conditions.append("(updated_at < ? OR (updated_at = ? AND ticket_id < ?))")
            params.extend([before[0], before[0], before[1]])
        where = " AND ".join(conditions)
        query = (
            f"SELECT * FROM repair_tickets WHERE {where} "
            f"ORDER BY updated_at DESC, ticket_id DESC LIMIT ?"
        )
        params.append(limit + 1)
        with self.connect() as conn:
            rows = conn.execute(query, params).fetchall()
        tickets = [_ticket_from_row(row) for row in rows[: limit]]
        next_position: tuple[str, str] | None = None
        if len(rows) > limit:
            last = tickets[-1]
            next_position = (last.updated_at, last.ticket_id)
        return tickets, next_position

    def transition_ticket(
        self,
        ticket_id: str,
        *,
        target_state: RepairTicketState,
        resolution_manifest_id: str | None = None,
        resolution_run_id: str | None = None,
        resolution_comparison: dict[str, Any] | None = None,
        abandon_reason: str | None = None,
    ) -> RepairTicket:
        ticket = self.get_ticket(ticket_id)
        assert_ticket_transition(ticket.state, target_state)
        now = _now()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE repair_tickets
                SET state = ?,
                    resolution_manifest_id = COALESCE(?, resolution_manifest_id),
                    resolution_run_id = COALESCE(?, resolution_run_id),
                    resolution_comparison_json = COALESCE(?, resolution_comparison_json),
                    abandon_reason = COALESCE(?, abandon_reason),
                    updated_at = ?
                WHERE ticket_id = ? AND state = ?
                """,
                (
                    target_state.value,
                    resolution_manifest_id,
                    resolution_run_id,
                    json.dumps(resolution_comparison) if resolution_comparison else None,
                    abandon_reason,
                    now,
                    ticket_id,
                    ticket.state.value,
                ),
            )
            if cursor.rowcount == 0:
                raise RepairTicketConflict(
                    f"repair ticket {ticket_id} state changed concurrently"
                )
        return self.get_ticket(ticket_id)

    def list_tickets_for_session(self, session_id: str) -> list[RepairTicket]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM repair_tickets WHERE session_id = ? ORDER BY created_at DESC",
                (session_id,),
            ).fetchall()
        return [_ticket_from_row(row) for row in rows]


# ---------------------------------------------------------------------------
# Row mappers
# ---------------------------------------------------------------------------


def _manifest_from_row(row: sqlite3.Row) -> ArtifactManifest:
    return ArtifactManifest(
        manifest_id=row["manifest_id"],
        owner=row["owner"],
        run_id=row["run_id"],
        revision=row["revision"],
        dirty_diff_digest=row["dirty_diff_digest"],
        bundle_digest=row["bundle_digest"],
        remote_workdir=row["remote_workdir"],
        local_test_summary=row["local_test_summary"],
        disclosure=row["disclosure"],
        created_at=row["created_at"],
    )


def _ticket_from_row(row: sqlite3.Row) -> RepairTicket:
    return RepairTicket(
        ticket_id=row["ticket_id"],
        owner=row["owner"],
        state=RepairTicketState(row["state"]),
        source_run_id=row["source_run_id"],
        source_contract_id=row["source_contract_id"],
        session_id=row["session_id"],
        diagnosis_ids=tuple(json.loads(row["diagnosis_ids_json"])),
        cited_facts=tuple(json.loads(row["cited_facts_json"])),
        code_context=json.loads(row["code_context_json"]) if row["code_context_json"] else None,
        requested_change=row["requested_change"],
        no_go_constraints=tuple(json.loads(row["no_go_constraints_json"])),
        resolution_manifest_id=row["resolution_manifest_id"],
        resolution_run_id=row["resolution_run_id"],
        resolution_comparison=(
            json.loads(row["resolution_comparison_json"])
            if row["resolution_comparison_json"]
            else None
        ),
        abandon_reason=row["abandon_reason"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
