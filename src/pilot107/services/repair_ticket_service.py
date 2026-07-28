"""Service layer for M2 repair ticket handoff."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from pilot107.core.repair_ticket import (
    ArtifactManifest,
    RepairTicket,
    RepairTicketState,
)
from pilot107.core.repair_ticket_store import RepairTicketStore

if TYPE_CHECKING:
    from pilot107.core.remediation_store import RemediationStore
    from pilot107.core.run_store import RunStore


class RepairTicketServiceError(RuntimeError):
    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


class RepairTicketService:
    def __init__(
        self,
        *,
        run_store: RunStore,
        repair_ticket_store: RepairTicketStore,
        remediation_store: RemediationStore | None = None,
    ) -> None:
        self.run_store = run_store
        self.repair_ticket_store = repair_ticket_store
        self.remediation_store = remediation_store

    # ------------------------------------------------------------------
    # Ticket creation
    # ------------------------------------------------------------------

    def create_from_session(
        self,
        session_id: str,
        *,
        owner: str,
        request_key: str,
    ) -> tuple[RepairTicket, bool]:
        """Create a repair ticket from a remediation session's diagnoses.

        Idempotent on (owner, request_key): returns existing ticket if the
        deterministic ID already exists.
        """
        if self.remediation_store is None:
            raise RepairTicketServiceError(
                "remediation store not available",
                code="REPAIR_TICKET.STORE_UNAVAILABLE",
            )
        try:
            session = self.remediation_store.get_session(session_id)
        except KeyError as exc:
            raise RepairTicketServiceError(
                "remediation session not found",
                code="REPAIR_TICKET.SESSION_NOT_FOUND",
            ) from exc
        if session.owner != owner:
            raise RepairTicketServiceError(
                "session belongs to another owner",
                code="AUTH.FORBIDDEN",
            )
        ticket_id = "rticket_" + hashlib.sha256(
            f"{owner}\0{request_key}".encode()
        ).hexdigest()[:32]
        # Idempotency: return existing if already created.
        try:
            existing = self.repair_ticket_store.get_ticket(ticket_id)
            return existing, False
        except KeyError:
            pass
        # Gather diagnoses from the source run.
        diagnoses = self.run_store.list_diagnoses(session.source_run_id)
        diagnosis_ids = tuple(d.diagnosis_id for d in diagnoses)
        cited_facts = tuple(
            {
                "rule_id": d.rule_id,
                "severity": d.severity,
                "summary": d.summary,
                "evidence_refs": d.evidence_refs,
            }
            for d in diagnoses
        )
        # Build requested_change from diagnosis summaries.
        requested_change = _build_requested_change(diagnoses)
        # Carry the code-context window captured by the Agent so the ticket
        # is a self-contained handoff (source snippet + Slurm error evidence).
        code_context = self._code_context_for_session(session_id)
        ticket = RepairTicket(
            ticket_id=ticket_id,
            owner=owner,
            state=RepairTicketState.OPEN,
            source_run_id=session.source_run_id,
            source_contract_id=session.source_contract_id,
            session_id=session_id,
            diagnosis_ids=diagnosis_ids,
            cited_facts=cited_facts,
            code_context=code_context,
            requested_change=requested_change,
            no_go_constraints=(
                "不得上传完整仓库",
                "不得执行任意 shell",
                "不得访问他人工作目录",
            ),
        )
        created = self.repair_ticket_store.create_ticket(ticket)
        return created, True

    def _code_context_for_session(self, session_id: str) -> dict[str, Any] | None:
        """Extract the code-context bundle from the session's latest advice.

        The Agent stores ``code_context`` in the advice payload when the
        explain service captured a source window. We surface it on the ticket
        so the repair handoff does not depend on re-deriving the context.
        Returns None when no advice / code context is available.
        """
        if self.remediation_store is None:
            return None
        advice_ids = [
            turn.advice_id
            for turn in self.remediation_store.list_turns(session_id)
            if turn.advice_id
        ]
        for advice_id in reversed(advice_ids):
            try:
                advice = self.run_store.get_agent_advice(advice_id)
            except KeyError:
                continue
            code_context = advice.payload.get("code_context")
            if isinstance(code_context, dict) and code_context:
                return code_context
        return None

    def create_direct(
        self,
        *,
        owner: str,
        source_run_id: str,
        request_key: str,
        diagnosis_ids: tuple[str, ...] = (),
        requested_change: str | None = None,
    ) -> tuple[RepairTicket, bool]:
        """Create a repair ticket directly from a run (without session)."""
        try:
            run = self.run_store.get_run(source_run_id)
        except KeyError as exc:
            raise RepairTicketServiceError(
                "source run not found",
                code="REPAIR_TICKET.RUN_NOT_FOUND",
            ) from exc
        if run.owner != owner:
            raise RepairTicketServiceError(
                "run belongs to another owner",
                code="AUTH.FORBIDDEN",
            )
        ticket_id = "rticket_" + hashlib.sha256(
            f"{owner}\0{request_key}".encode()
        ).hexdigest()[:32]
        try:
            existing = self.repair_ticket_store.get_ticket(ticket_id)
            return existing, False
        except KeyError:
            pass
        diagnoses = self.run_store.list_diagnoses(source_run_id)
        if not diagnosis_ids:
            diagnosis_ids = tuple(d.diagnosis_id for d in diagnoses)
        cited_facts = tuple(
            {
                "rule_id": d.rule_id,
                "severity": d.severity,
                "summary": d.summary,
                "evidence_refs": d.evidence_refs,
            }
            for d in diagnoses
            if d.diagnosis_id in set(diagnosis_ids)
        )
        ticket = RepairTicket(
            ticket_id=ticket_id,
            owner=owner,
            state=RepairTicketState.OPEN,
            source_run_id=source_run_id,
            source_contract_id=run.contract_id,
            diagnosis_ids=diagnosis_ids,
            cited_facts=cited_facts,
            requested_change=requested_change or _build_requested_change(diagnoses),
            no_go_constraints=(
                "不得上传完整仓库",
                "不得执行任意 shell",
                "不得访问他人工作目录",
            ),
        )
        created = self.repair_ticket_store.create_ticket(ticket)
        return created, True

    # ------------------------------------------------------------------
    # ArtifactManifest
    # ------------------------------------------------------------------

    def create_manifest(
        self,
        *,
        owner: str,
        revision: str,
        run_id: str | None = None,
        dirty_diff_digest: str | None = None,
        bundle_digest: str | None = None,
        remote_workdir: str | None = None,
        local_test_summary: str | None = None,
        disclosure: str = "metadata_only",
    ) -> ArtifactManifest:
        manifest_id = "manifest_" + hashlib.sha256(
            f"{owner}\0{revision}\0{run_id or ''}\0{datetime.now(UTC).isoformat()}".encode()
        ).hexdigest()[:32]
        manifest = ArtifactManifest(
            manifest_id=manifest_id,
            owner=owner,
            run_id=run_id,
            revision=revision,
            dirty_diff_digest=dirty_diff_digest,
            bundle_digest=bundle_digest,
            remote_workdir=remote_workdir,
            local_test_summary=local_test_summary,
            disclosure=disclosure,
        )
        return self.repair_ticket_store.create_manifest(manifest)

    # ------------------------------------------------------------------
    # Ticket lifecycle
    # ------------------------------------------------------------------

    def resolve(
        self,
        ticket_id: str,
        *,
        owner: str,
        manifest_id: str,
        derived_run_id: str,
    ) -> RepairTicket:
        """Resolve a ticket by binding a manifest and derived run."""
        ticket = self._get_owned_ticket(ticket_id, owner)
        # Validate manifest exists and belongs to owner.
        try:
            manifest = self.repair_ticket_store.get_manifest(manifest_id)
        except KeyError as exc:
            raise RepairTicketServiceError(
                "artifact manifest not found",
                code="REPAIR_TICKET.MANIFEST_NOT_FOUND",
            ) from exc
        if manifest.owner != owner:
            raise RepairTicketServiceError(
                "manifest belongs to another owner",
                code="AUTH.FORBIDDEN",
            )
        # Validate derived run exists.
        try:
            self.run_store.get_run(derived_run_id)
        except KeyError as exc:
            raise RepairTicketServiceError(
                "derived run not found",
                code="REPAIR_TICKET.RUN_NOT_FOUND",
            ) from exc
        # Build comparison between source and derived runs.
        comparison = self._build_comparison(ticket.source_run_id, derived_run_id)
        # Bind manifest to derived run.
        self.repair_ticket_store.bind_manifest_to_run(manifest_id, derived_run_id)
        return self.repair_ticket_store.transition_ticket(
            ticket_id,
            target_state=RepairTicketState.RESOLVED,
            resolution_manifest_id=manifest_id,
            resolution_run_id=derived_run_id,
            resolution_comparison=comparison,
        )

    def abandon(
        self,
        ticket_id: str,
        *,
        owner: str,
        reason: str | None = None,
    ) -> RepairTicket:
        self._get_owned_ticket(ticket_id, owner)
        return self.repair_ticket_store.transition_ticket(
            ticket_id,
            target_state=RepairTicketState.ABANDONED,
            abandon_reason=reason,
        )

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def detail(self, ticket_id: str, *, owner: str) -> dict[str, Any]:
        ticket = self._get_owned_ticket(ticket_id, owner)
        return ticket.to_payload()

    def _get_owned_ticket(self, ticket_id: str, owner: str) -> RepairTicket:
        try:
            ticket = self.repair_ticket_store.get_ticket(ticket_id)
        except KeyError as exc:
            raise RepairTicketServiceError(
                "repair ticket not found",
                code="REPAIR_TICKET.NOT_FOUND",
            ) from exc
        if ticket.owner != owner:
            raise RepairTicketServiceError(
                "ticket belongs to another owner",
                code="AUTH.FORBIDDEN",
            )
        return ticket

    def _build_comparison(
        self, source_run_id: str, derived_run_id: str
    ) -> dict[str, Any]:
        """Compare source and derived runs for the resolution payload."""
        try:
            source_run = self.run_store.get_run(source_run_id)
        except KeyError:
            source_run = None
        try:
            derived_run = self.run_store.get_run(derived_run_id)
        except KeyError:
            derived_run = None
        comparison: dict[str, Any] = {
            "source_run_id": source_run_id,
            "derived_run_id": derived_run_id,
        }
        if source_run is not None:
            comparison["source_state"] = source_run.state
            comparison["source_exit_code"] = source_run.exit_code
        if derived_run is not None:
            comparison["derived_state"] = derived_run.state
            comparison["derived_exit_code"] = derived_run.exit_code
        # Diagnosis diff: source diagnoses vs derived diagnoses.
        source_diagnoses = self.run_store.list_diagnoses(source_run_id)
        derived_diagnoses = self.run_store.list_diagnoses(derived_run_id)
        comparison["source_diagnosis_count"] = len(source_diagnoses)
        comparison["derived_diagnosis_count"] = len(derived_diagnoses)
        comparison["source_diagnosis_rules"] = sorted(
            {d.rule_id for d in source_diagnoses}
        )
        comparison["derived_diagnosis_rules"] = sorted(
            {d.rule_id for d in derived_diagnoses}
        )
        # Improvement signal.
        if source_run is not None and derived_run is not None:
            comparison["improved"] = (
                source_run.state == "FAILED" and derived_run.state == "SUCCEEDED"
            )
        return comparison


def _build_requested_change(diagnoses: list[Any]) -> str | None:
    """Synthesize a human-readable requested change from diagnoses."""
    if not diagnoses:
        return None
    parts = [d.summary for d in diagnoses if d.summary]
    if not parts:
        return None
    return "；".join(parts)
