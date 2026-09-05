"""Durable strong branches for Agent-mediated market lifecycles."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from copy import deepcopy
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from pilot107.core.contracts import ContractService
from pilot107.core.run_publications import (
    RunPublicationRecord,
    RunPublicationShareManifest,
    RunPublicationStore,
)
from pilot107.core.run_store import RunRecord, RunStore, utc_now_iso
from pilot107.core.states import RunState
from pilot107.core.template_market import (
    TemplateMarketStore,
    TemplateReviewState,
    TemplateVisibility,
    authorize_template_release,
)
from pilot107.core.template_policy import TemplateReviewerPrincipal
from pilot107.services.project_agent_service import ProjectAgentService


class MarketApplicationError(ValueError):
    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


class TemplatePublicationError(ValueError):
    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


class MarketApplicationSourceKind(StrEnum):
    CURATED_TEMPLATE = "curated_template"
    RUN_PUBLICATION = "run_publication"


class MarketAssurance(StrEnum):
    CURATED = "curated"
    REFERENCE_ONLY = "reference_only"


@dataclass(frozen=True)
class MarketApplicationSession:
    session_id: str
    owner: str
    request_key: str
    source_kind: MarketApplicationSourceKind
    source_item_id: str
    source_digest: str
    assurance: MarketAssurance
    user_intent: str
    state: str
    version: int
    project_id: str | None
    workspace_id: str | None
    change_set_id: str | None
    target_contract_id: str | None
    adoption_id: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class ReferenceAdaptationSession:
    application: MarketApplicationSession
    source_run_id: str
    source_contract_id: str
    share_manifest_digest: str
    target_contract_payload: dict[str, object]
    plan_digest: str
    confirmation_digest: str
    change_set_digest: str | None


@dataclass(frozen=True)
class TemplateApplicationSession:
    application: MarketApplicationSession
    release_id: str
    template_id: str
    release_version: str
    bundle_digest: str
    target_contract_payload: dict[str, object]
    plan_digest: str
    confirmation_digest: str
    change_set_digest: str | None


@dataclass(frozen=True)
class TemplatePublicationSession:
    session_id: str
    owner: str
    request_key: str
    source_run_id: str
    source_contract_id: str
    source_digest: str
    bundle_digest: str
    draft_id: str | None
    state: str
    version: int
    reproduction_evidence_ref: str | None
    reproduction_evidence_digest: str | None
    reproduction_environment: str | None
    confirmation_digest: str | None
    review_id: str | None
    release_id: str | None
    release_version: str | None
    verification_id: str | None
    created_at: str
    updated_at: str


class SQLiteMarketSessionStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS template_publication_sessions (
                    session_id TEXT PRIMARY KEY,
                    owner TEXT NOT NULL,
                    request_key TEXT NOT NULL,
                    source_run_id TEXT NOT NULL,
                    source_contract_id TEXT NOT NULL,
                    source_digest TEXT NOT NULL,
                    bundle_digest TEXT NOT NULL,
                    draft_id TEXT,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    reproduction_evidence_ref TEXT,
                    reproduction_evidence_digest TEXT,
                    reproduction_environment TEXT,
                    confirmation_digest TEXT,
                    review_id TEXT,
                    release_id TEXT,
                    release_version TEXT,
                    verification_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(owner, source_run_id),
                    UNIQUE(owner, request_key)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS market_application_sessions (
                    session_id TEXT PRIMARY KEY,
                    owner TEXT NOT NULL,
                    request_key TEXT NOT NULL,
                    source_kind TEXT NOT NULL,
                    source_item_id TEXT NOT NULL,
                    source_digest TEXT NOT NULL,
                    assurance TEXT NOT NULL,
                    user_intent TEXT NOT NULL,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    project_id TEXT,
                    workspace_id TEXT,
                    change_set_id TEXT,
                    target_contract_id TEXT,
                    adoption_id TEXT,
                    detail_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(owner, request_key),
                    CHECK (source_kind IN ('curated_template', 'run_publication')),
                    CHECK (assurance IN ('curated', 'reference_only'))
                )
                """
            )

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def list_template_publications(
        self,
        *,
        owner: str,
        source_run_id: str | None = None,
    ) -> list[TemplatePublicationSession]:
        conditions = ["owner = ?"]
        values: list[object] = [owner]
        if source_run_id is not None:
            conditions.append("source_run_id = ?")
            values.append(source_run_id)
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM template_publication_sessions WHERE "
                + " AND ".join(conditions)
                + " ORDER BY created_at, session_id",
                values,
            ).fetchall()
        return [_template_publication_from_row(row) for row in rows]

    def get_template_publication_by_request(
        self,
        *,
        owner: str,
        request_key: str,
    ) -> TemplatePublicationSession | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM template_publication_sessions "
                "WHERE owner = ? AND request_key = ?",
                (owner, request_key),
            ).fetchone()
        return None if row is None else _template_publication_from_row(row)

    def get_template_publication(
        self,
        session_id: str,
        *,
        owner: str,
    ) -> TemplatePublicationSession:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM template_publication_sessions "
                "WHERE session_id = ? AND owner = ?",
                (session_id, owner),
            ).fetchone()
        if row is None:
            raise KeyError(session_id)
        return _template_publication_from_row(row)

    def create_template_publication(
        self,
        *,
        owner: str,
        request_key: str,
        source_run_id: str,
        source_contract_id: str,
        source_digest: str,
        bundle_digest: str,
        draft_id: str,
    ) -> TemplatePublicationSession:
        session_id = _template_publication_session_id(owner=owner, request_key=request_key)
        now = utc_now_iso()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM template_publication_sessions "
                "WHERE owner = ? AND request_key = ?",
                (owner, request_key),
            ).fetchone()
            if existing is not None:
                record = _template_publication_from_row(existing)
                if (
                    record.source_run_id != source_run_id
                    or record.source_contract_id != source_contract_id
                    or record.source_digest != source_digest
                    or record.bundle_digest != bundle_digest
                    or record.draft_id != draft_id
                ):
                    raise TemplatePublicationError(
                        "request_key was used for another template publication",
                        code="TEMPLATE.PUBLICATION_CONFLICT",
                    )
                return record
            try:
                connection.execute(
                    """
                    INSERT INTO template_publication_sessions (
                        session_id, owner, request_key, source_run_id,
                        source_contract_id, source_digest, bundle_digest, draft_id,
                        state, version, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'awaiting_reproduction', 1, ?, ?)
                    """,
                    (
                        session_id,
                        owner,
                        request_key,
                        source_run_id,
                        source_contract_id,
                        source_digest,
                        bundle_digest,
                        draft_id,
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise TemplatePublicationError(
                    "source Run already has a TemplatePublicationSession",
                    code="TEMPLATE.PUBLICATION_CONFLICT",
                ) from exc
            row = connection.execute(
                "SELECT * FROM template_publication_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            raise KeyError(session_id)
        return _template_publication_from_row(row)

    def create_equivalent_template_verification(
        self,
        *,
        owner: str,
        request_key: str,
        source_run_id: str,
        source_contract_id: str,
        source_digest: str,
        bundle_digest: str,
        release_id: str,
        release_version: str,
        verification_id: str,
        evidence_ref: str,
        evidence_digest: str,
        environment: str,
    ) -> TemplatePublicationSession:
        session_id = _template_publication_session_id(owner=owner, request_key=request_key)
        now = utc_now_iso()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM template_publication_sessions "
                "WHERE owner = ? AND request_key = ?",
                (owner, request_key),
            ).fetchone()
            if existing is not None:
                record = _template_publication_from_row(existing)
                if (
                    record.source_run_id != source_run_id
                    or record.source_contract_id != source_contract_id
                    or record.source_digest != source_digest
                    or record.bundle_digest != bundle_digest
                    or record.release_id != release_id
                    or record.verification_id != verification_id
                ):
                    raise TemplatePublicationError(
                        "request_key was used for another template publication",
                        code="TEMPLATE.PUBLICATION_CONFLICT",
                    )
                return record
            try:
                connection.execute(
                    """
                    INSERT INTO template_publication_sessions (
                        session_id, owner, request_key, source_run_id,
                        source_contract_id, source_digest, bundle_digest, draft_id,
                        state, version, reproduction_evidence_ref,
                        reproduction_evidence_digest, reproduction_environment,
                        release_id, release_version, verification_id,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 'completed', 1,
                              ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        owner,
                        request_key,
                        source_run_id,
                        source_contract_id,
                        source_digest,
                        bundle_digest,
                        evidence_ref,
                        evidence_digest,
                        environment,
                        release_id,
                        release_version,
                        verification_id,
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise TemplatePublicationError(
                    "source Run already has a TemplatePublicationSession",
                    code="TEMPLATE.PUBLICATION_CONFLICT",
                ) from exc
            row = connection.execute(
                "SELECT * FROM template_publication_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            raise KeyError(session_id)
        return _template_publication_from_row(row)

    def record_template_reproduction(
        self,
        session_id: str,
        *,
        owner: str,
        expected_version: int,
        evidence_ref: str,
        evidence_digest: str,
        environment: str,
        release_version: str,
        confirmation_digest: str,
    ) -> TemplatePublicationSession:
        return self._transition_template_publication(
            session_id,
            owner=owner,
            expected_version=expected_version,
            expected_state="awaiting_reproduction",
            target_state="awaiting_confirmation",
            assignments={
                "reproduction_evidence_ref": evidence_ref,
                "reproduction_evidence_digest": evidence_digest,
                "reproduction_environment": environment,
                "release_version": release_version,
                "confirmation_digest": confirmation_digest,
            },
        )

    def mark_template_review_submitted(
        self,
        session_id: str,
        *,
        owner: str,
        expected_version: int,
        review_id: str,
    ) -> TemplatePublicationSession:
        return self._transition_template_publication(
            session_id,
            owner=owner,
            expected_version=expected_version,
            expected_state="awaiting_confirmation",
            target_state="submitted",
            assignments={"review_id": review_id},
        )

    def complete_template_publication(
        self,
        session_id: str,
        *,
        owner: str,
        expected_version: int,
        release_id: str,
    ) -> TemplatePublicationSession:
        return self._transition_template_publication(
            session_id,
            owner=owner,
            expected_version=expected_version,
            expected_state="submitted",
            target_state="completed",
            assignments={"release_id": release_id},
        )

    def _transition_template_publication(
        self,
        session_id: str,
        *,
        owner: str,
        expected_version: int,
        expected_state: str,
        target_state: str,
        assignments: dict[str, object],
    ) -> TemplatePublicationSession:
        allowed_columns = {
            "reproduction_evidence_ref",
            "reproduction_evidence_digest",
            "reproduction_environment",
            "confirmation_digest",
            "review_id",
            "release_id",
            "release_version",
            "verification_id",
        }
        if not assignments or set(assignments) - allowed_columns:
            raise ValueError("template publication transition assignments are invalid")
        now = utc_now_iso()
        columns = [*sorted(assignments), "state", "version", "updated_at"]
        set_clause = ", ".join(
            f"{column} = ?" if column != "version" else "version = version + 1"
            for column in columns
        )
        values = [assignments[column] for column in sorted(assignments)]
        values.extend([target_state, now, session_id, owner, expected_version, expected_state])
        with self.connect() as connection:
            result = connection.execute(
                f"UPDATE template_publication_sessions SET {set_clause} "
                "WHERE session_id = ? AND owner = ? AND version = ? AND state = ?",
                values,
            )
            if result.rowcount != 1:
                row = connection.execute(
                    "SELECT * FROM template_publication_sessions "
                    "WHERE session_id = ? AND owner = ?",
                    (session_id, owner),
                ).fetchone()
                if row is None:
                    raise KeyError(session_id)
                record = _template_publication_from_row(row)
                if record.state == target_state and all(
                    getattr(record, key) == value for key, value in assignments.items()
                ):
                    return record
                raise TemplatePublicationError(
                    "template publication version or state changed",
                    code="TEMPLATE.PUBLICATION_CONFLICT",
                )
            row = connection.execute(
                "SELECT * FROM template_publication_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            raise KeyError(session_id)
        return _template_publication_from_row(row)

    def list_market_applications(self, *, owner: str) -> list[MarketApplicationSession]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM market_application_sessions WHERE owner = ? "
                "ORDER BY created_at, session_id",
                (owner,),
            ).fetchall()
        return [_market_application_from_row(row) for row in rows]

    def get_market_application(
        self,
        session_id: str,
        *,
        owner: str,
    ) -> MarketApplicationSession:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM market_application_sessions "
                "WHERE session_id = ? AND owner = ?",
                (session_id, owner),
            ).fetchone()
        if row is None:
            raise KeyError(session_id)
        return _market_application_from_row(row)

    def create_market_application(
        self,
        *,
        owner: str,
        request_key: str,
        source_kind: MarketApplicationSourceKind,
        source_item_id: str,
        source_digest: str,
        assurance: MarketAssurance,
        user_intent: str,
        detail: dict[str, object],
        project_id: str | None = None,
        workspace_id: str | None = None,
        change_set_id: str | None = None,
    ) -> MarketApplicationSession:
        session_id = _market_session_id(owner=owner, request_key=request_key)
        now = utc_now_iso()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM market_application_sessions "
                "WHERE owner = ? AND request_key = ?",
                (owner, request_key),
            ).fetchone()
            if existing is not None:
                record = _market_application_from_row(existing)
                if (
                    record.source_kind is not source_kind
                    or record.source_item_id != source_item_id
                    or record.source_digest != source_digest
                    or record.user_intent != user_intent
                    or record.project_id != project_id
                    or record.workspace_id != workspace_id
                    or record.change_set_id != change_set_id
                ):
                    raise MarketApplicationError(
                        "request_key was used for another market application",
                        code="MARKET.APPLICATION_CONFLICT",
                    )
                return record
            connection.execute(
                """
                INSERT INTO market_application_sessions (
                    session_id, owner, request_key, source_kind, source_item_id,
                    source_digest, assurance, user_intent, state, version,
                    project_id, workspace_id, change_set_id, target_contract_id,
                    adoption_id, detail_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'awaiting_confirmation', 1,
                          ?, ?, ?, NULL, NULL, ?, ?, ?)
                """,
                (
                    session_id,
                    owner,
                    request_key,
                    source_kind.value,
                    source_item_id,
                    source_digest,
                    assurance.value,
                    user_intent,
                    project_id,
                    workspace_id,
                    change_set_id,
                    _canonical_json(detail),
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM market_application_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            raise KeyError(session_id)
        return _market_application_from_row(row)

    def market_application_detail(self, session_id: str, *, owner: str) -> dict[str, object]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT detail_json FROM market_application_sessions "
                "WHERE session_id = ? AND owner = ?",
                (session_id, owner),
            ).fetchone()
        if row is None:
            raise KeyError(session_id)
        value = json.loads(str(row["detail_json"]))
        if not isinstance(value, dict):
            raise ValueError("market application detail is invalid")
        return value

    def complete_market_application(
        self,
        session_id: str,
        *,
        owner: str,
        expected_version: int,
        target_contract_id: str,
        adoption_id: str,
    ) -> MarketApplicationSession:
        now = utc_now_iso()
        with self.connect() as connection:
            result = connection.execute(
                """
                UPDATE market_application_sessions
                SET state = 'completed', version = version + 1,
                    target_contract_id = ?, adoption_id = ?, updated_at = ?
                WHERE session_id = ? AND owner = ? AND version = ?
                  AND state = 'awaiting_confirmation'
                """,
                (
                    target_contract_id,
                    adoption_id,
                    now,
                    session_id,
                    owner,
                    expected_version,
                ),
            )
            if result.rowcount != 1:
                current = connection.execute(
                    "SELECT * FROM market_application_sessions "
                    "WHERE session_id = ? AND owner = ?",
                    (session_id, owner),
                ).fetchone()
                if current is None:
                    raise KeyError(session_id)
                record = _market_application_from_row(current)
                if (
                    record.state == "completed"
                    and record.target_contract_id == target_contract_id
                    and record.adoption_id == adoption_id
                ):
                    return record
                raise MarketApplicationError(
                    "market application version or state changed",
                    code="MARKET.APPLICATION_CONFLICT",
                )
            row = connection.execute(
                "SELECT * FROM market_application_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            raise KeyError(session_id)
        return _market_application_from_row(row)


class MarketApplicationService:
    def __init__(
        self,
        *,
        store: SQLiteMarketSessionStore,
        contract_service: ContractService,
        run_publications: RunPublicationStore,
        template_market: TemplateMarketStore | None,
        project_service: ProjectAgentService | None,
    ) -> None:
        self.store = store
        self.contract_service = contract_service
        self.run_publications = run_publications
        self.template_market = template_market
        self.project_service = project_service

    def start_reference_adaptation(
        self,
        *,
        owner: str,
        publication_id: str,
        user_intent: str,
        request_key: str,
        course_scopes: frozenset[str] = frozenset(),
    ) -> ReferenceAdaptationSession:
        publication = self.run_publications.get_visible(
            publication_id=publication_id,
            actor=owner,
            course_scopes=course_scopes,
        )
        if (
            publication.source_contract_id is None
            or publication.share_manifest.get("contract_for_adaptation") is not True
        ):
            raise MarketApplicationError(
                "RunPublication did not share its Contract for adaptation",
                code="MARKET.SOURCE_NOT_ADAPTABLE",
            )
        detail: dict[str, object] = {
            "source_run_id": publication.source_run_id,
            "source_contract_id": publication.source_contract_id,
            "share_manifest_digest": publication.share_manifest_digest,
        }
        source_contract = self.contract_service.get(publication.source_contract_id)
        target_payload = _rebase_contract_workdir(source_contract.payload, owner=owner)
        validation = self.contract_service.validate(target_payload)
        if validation.status == "BLOCK":
            raise MarketApplicationError(
                "shared Contract cannot be adapted under current policy",
                code="MARKET.SOURCE_NOT_ADAPTABLE",
            )
        project_id, workspace_id, change_set_id, change_set_digest = (
            self._application_project(
                owner=owner,
                source_item_id=publication.publication_id,
                user_intent=user_intent,
                request_key=request_key,
                target_payload=target_payload,
            )
        )
        session_id = _market_session_id(owner=owner, request_key=request_key)
        plan_digest = _digest(
            {
                "source_digest": publication.share_manifest_digest,
                "assurance": MarketAssurance.REFERENCE_ONLY.value,
                "target_contract": target_payload,
                "change_set_digest": change_set_digest,
            }
        )
        confirmation_digest = _digest(
            {
                "owner": owner,
                "session_id": session_id,
                "source_digest": publication.share_manifest_digest,
                "plan_digest": plan_digest,
            }
        )
        detail.update(
            {
                "target_contract_payload": target_payload,
                "plan_digest": plan_digest,
                "confirmation_digest": confirmation_digest,
                "change_set_digest": change_set_digest,
            }
        )
        application = self.store.create_market_application(
            owner=owner,
            request_key=request_key,
            source_kind=MarketApplicationSourceKind.RUN_PUBLICATION,
            source_item_id=publication.publication_id,
            source_digest=publication.share_manifest_digest,
            assurance=MarketAssurance.REFERENCE_ONLY,
            user_intent=user_intent,
            detail=detail,
            project_id=project_id,
            workspace_id=workspace_id,
            change_set_id=change_set_id,
        )
        persisted = self.store.market_application_detail(
            application.session_id,
            owner=owner,
        )
        persisted_target = persisted.get("target_contract_payload")
        if not isinstance(persisted_target, dict):
            raise ValueError("reference target Contract payload is invalid")
        return ReferenceAdaptationSession(
            application=application,
            source_run_id=str(persisted["source_run_id"]),
            source_contract_id=str(persisted["source_contract_id"]),
            share_manifest_digest=str(persisted["share_manifest_digest"]),
            target_contract_payload=persisted_target,
            plan_digest=str(persisted["plan_digest"]),
            confirmation_digest=str(persisted["confirmation_digest"]),
            change_set_digest=(
                None
                if persisted.get("change_set_digest") is None
                else str(persisted["change_set_digest"])
            ),
        )

    def start_template_application(
        self,
        *,
        owner: str,
        release_id: str,
        user_intent: str,
        request_key: str,
        course_scopes: frozenset[str] = frozenset(),
    ) -> TemplateApplicationSession:
        if self.template_market is None:
            raise MarketApplicationError(
                "curated template market is unavailable",
                code="MARKET.SOURCE_NOT_ADAPTABLE",
            )
        release = self.template_market.get_release(release_id)
        authorize_template_release(
            release,
            actor=owner,
            course_scopes=course_scopes,
        )
        if release.withdrawn_at is not None:
            raise MarketApplicationError(
                "curated source is withdrawn",
                code="MARKET.SOURCE_WITHDRAWN",
            )
        target_payload = _rebase_contract_workdir(release.payload, owner=owner)
        validation = self.contract_service.validate(target_payload)
        if validation.status == "BLOCK":
            raise MarketApplicationError(
                "curated release cannot be applied under current policy",
                code="MARKET.SOURCE_NOT_ADAPTABLE",
            )
        source_digest = release.content_sha256
        project_id, workspace_id, change_set_id, change_set_digest = (
            self._application_project(
                owner=owner,
                source_item_id=release.release_id,
                user_intent=user_intent,
                request_key=request_key,
                target_payload=target_payload,
            )
        )
        session_id = _market_session_id(owner=owner, request_key=request_key)
        plan_digest = _digest(
            {
                "source_digest": source_digest,
                "assurance": MarketAssurance.CURATED.value,
                "target_contract": target_payload,
                "change_set_digest": change_set_digest,
            }
        )
        confirmation_digest = _digest(
            {
                "owner": owner,
                "session_id": session_id,
                "source_digest": source_digest,
                "plan_digest": plan_digest,
            }
        )
        detail: dict[str, object] = {
            "release_id": release.release_id,
            "template_id": release.template_id,
            "release_version": release.release_version,
            "bundle_digest": source_digest,
            "target_contract_payload": target_payload,
            "plan_digest": plan_digest,
            "confirmation_digest": confirmation_digest,
            "change_set_digest": change_set_digest,
        }
        application = self.store.create_market_application(
            owner=owner,
            request_key=request_key,
            source_kind=MarketApplicationSourceKind.CURATED_TEMPLATE,
            source_item_id=release.release_id,
            source_digest=source_digest,
            assurance=MarketAssurance.CURATED,
            user_intent=user_intent,
            detail=detail,
            project_id=project_id,
            workspace_id=workspace_id,
            change_set_id=change_set_id,
        )
        return _template_session(
            application,
            self.store.market_application_detail(application.session_id, owner=owner),
        )

    def finalize_reference_adaptation(
        self,
        *,
        session_id: str,
        owner: str,
        expected_version: int,
        confirmation_digest: str,
        request_key: str,
        course_scopes: frozenset[str] = frozenset(),
    ) -> ReferenceAdaptationSession:
        application = self.store.get_market_application(session_id, owner=owner)
        detail = self.store.market_application_detail(session_id, owner=owner)
        if application.source_kind is not MarketApplicationSourceKind.RUN_PUBLICATION:
            raise MarketApplicationError(
                "curated applications require their typed finalizer",
                code="MARKET.ASSURANCE_MISMATCH",
            )
        reference = _reference_session(application, detail)
        if confirmation_digest != reference.confirmation_digest:
            raise MarketApplicationError(
                "market application confirmation is stale",
                code="MARKET.CONFIRMATION_STALE",
            )
        publication = self.run_publications.get_visible(
            publication_id=application.source_item_id,
            actor=owner,
            course_scopes=course_scopes,
        )
        if (
            publication.share_manifest_digest != application.source_digest
            or publication.source_contract_id != reference.source_contract_id
        ):
            raise MarketApplicationError(
                "market application source changed",
                code="MARKET.SOURCE_DIGEST_CHANGED",
            )
        adoption = self.run_publications.adopt(
            publication_id=publication.publication_id,
            adopter=owner,
            request_key=request_key,
            course_scopes=course_scopes,
            target_payload=reference.target_contract_payload,
            market_application_session_id=session_id,
        )
        if adoption.target_contract_id is None:
            raise MarketApplicationError(
                "reference adaptation did not create a Contract",
                code="MARKET.SOURCE_NOT_ADAPTABLE",
            )
        completed = self.store.complete_market_application(
            session_id,
            owner=owner,
            expected_version=expected_version,
            target_contract_id=adoption.target_contract_id,
            adoption_id=adoption.adoption_id,
        )
        return _reference_session(completed, detail)

    def finalize_template_application(
        self,
        *,
        session_id: str,
        owner: str,
        expected_version: int,
        confirmation_digest: str,
        request_key: str,
        course_scopes: frozenset[str] = frozenset(),
    ) -> TemplateApplicationSession:
        application = self.store.get_market_application(session_id, owner=owner)
        detail = self.store.market_application_detail(session_id, owner=owner)
        if application.source_kind is not MarketApplicationSourceKind.CURATED_TEMPLATE:
            raise MarketApplicationError(
                "run references require their typed finalizer",
                code="MARKET.ASSURANCE_MISMATCH",
            )
        template = _template_session(application, detail)
        if confirmation_digest != template.confirmation_digest:
            raise MarketApplicationError(
                "market application confirmation is stale",
                code="MARKET.CONFIRMATION_STALE",
            )
        if self.template_market is None:
            raise MarketApplicationError(
                "curated template market is unavailable",
                code="MARKET.SOURCE_NOT_ADAPTABLE",
            )
        release = self.template_market.get_release(template.release_id)
        authorize_template_release(
            release,
            actor=owner,
            course_scopes=course_scopes,
        )
        if release.withdrawn_at is not None:
            raise MarketApplicationError(
                "curated source is withdrawn",
                code="MARKET.SOURCE_WITHDRAWN",
            )
        if release.content_sha256 != application.source_digest:
            raise MarketApplicationError(
                "curated source changed",
                code="MARKET.SOURCE_DIGEST_CHANGED",
            )
        adoption = self.template_market.adopt_release(
            release.release_id,
            adopter=owner,
            request_key=request_key,
            course_scopes=course_scopes,
            target_payload=template.target_contract_payload,
            market_application_session_id=session_id,
        )
        if adoption.target_contract_id is None:
            raise MarketApplicationError(
                "curated application did not create a Contract",
                code="MARKET.SOURCE_NOT_ADAPTABLE",
            )
        completed = self.store.complete_market_application(
            session_id,
            owner=owner,
            expected_version=expected_version,
            target_contract_id=adoption.target_contract_id,
            adoption_id=adoption.adoption_id,
        )
        return _template_session(completed, detail)

    def _application_project(
        self,
        *,
        owner: str,
        source_item_id: str,
        user_intent: str,
        request_key: str,
        target_payload: dict[str, object],
    ) -> tuple[str | None, str | None, str | None, str | None]:
        if self.project_service is None:
            return None, None, None, None
        view = self.project_service.create_market_application_project(
            owner=owner,
            source_item_id=source_item_id,
            goal=user_intent,
            request_key=f"market:{request_key}",
            contract_payload=target_payload,
        )
        if len(view.change_sets) != 1:
            raise MarketApplicationError(
                "market application Project must have one ChangeSet",
                code="MARKET.APPLICATION_CONFLICT",
            )
        change_set = view.change_sets[0]
        return (
            view.project.project_id,
            view.workspace.workspace_id,
            change_set.change_set_id,
            change_set.digest,
        )


class TemplatePublicationService:
    def __init__(
        self,
        *,
        store: SQLiteMarketSessionStore,
        run_store: RunStore,
        contract_service: ContractService,
        run_publications: RunPublicationStore,
        template_market: TemplateMarketStore | None,
    ) -> None:
        self.store = store
        self.run_store = run_store
        self.contract_service = contract_service
        self.run_publications = run_publications
        self.template_market = template_market

    def observe_successful_run(self, run: RunRecord) -> None:
        """Successful Runs remain private until an explicit share manifest exists."""

        del run
        return None

    def publish_run_reference(
        self,
        *,
        source_run_id: str,
        owner: str,
        request_key: str,
        manifest: RunPublicationShareManifest,
        description: str = "",
        reproduction_note: str = "",
    ) -> RunPublicationRecord:
        return self.run_publications.publish(
            source_run_id=source_run_id,
            owner=owner,
            title=manifest.title,
            description=description if manifest.description else "",
            visibility=manifest.visibility,
            scope_key=manifest.scope_key,
            reproduction_note=reproduction_note,
            request_key=request_key,
            confirmed=True,
            share_manifest=manifest,
        )

    def start_template_publication(
        self,
        *,
        owner: str,
        source_run_id: str,
        request_key: str,
        title: str,
        description: str,
        visibility: TemplateVisibility,
        scope_key: str | None,
        compatibility: dict[str, object],
        publication_metadata: dict[str, object],
        source_evidence_ref: str | None = None,
        source_evidence_digest: str | None = None,
        environment: str = "docker",
        base_release_id: str | None = None,
    ) -> TemplatePublicationSession:
        if self.template_market is None:
            raise TemplatePublicationError(
                "template market is unavailable",
                code="TEMPLATE.PUBLICATION_SOURCE_INELIGIBLE",
            )
        existing = self.store.get_template_publication_by_request(
            owner=owner,
            request_key=request_key,
        )
        if existing is not None:
            return existing
        run = self.run_store.get_run(source_run_id)
        if (
            run.owner != owner
            or run.state is not RunState.SUCCEEDED
            or not (run.exit_code or "").startswith("0:")
            or run.contract_id is None
        ):
            raise TemplatePublicationError(
                "source Run is not eligible for curated publication",
                code="TEMPLATE.PUBLICATION_SOURCE_INELIGIBLE",
            )
        contract = self.contract_service.get(run.contract_id)
        sanitized_payload = _sanitize_template_value(
            contract.payload,
            owner=owner,
            source_run_id=run.run_id,
            source_job_id=run.job_id,
        )
        if not isinstance(sanitized_payload, dict):
            raise TemplatePublicationError(
                "sanitized Contract payload is invalid",
                code="TEMPLATE.SANITIZATION_BLOCKED",
            )
        gate = self.template_market.publication_gate
        if gate is None:
            raise TemplatePublicationError(
                "template publication gate is unavailable",
                code="TEMPLATE.SANITIZATION_BLOCKED",
            )
        gate_result = gate.validate(
            payload=sanitized_payload,
            compatibility=compatibility,
            publication=publication_metadata,
        )
        if gate_result.status == "BLOCK":
            raise TemplatePublicationError(
                "sanitized bundle does not pass the publication gate",
                code="TEMPLATE.SANITIZATION_BLOCKED",
            )
        bundle_digest = _digest(
            {
                "schema_version": "pilot107.template-bundle/v1",
                "contract": sanitized_payload,
                "compatibility": compatibility,
                "publication": publication_metadata,
            }
        )
        equivalent_release = self.template_market.find_active_release_by_bundle_digest(
            bundle_digest
        )
        if equivalent_release is not None:
            _validate_reproduction_evidence(
                evidence_ref=source_evidence_ref,
                evidence_digest=source_evidence_digest,
                environment=environment,
            )
            assert source_evidence_ref is not None
            assert source_evidence_digest is not None
            verification = self.template_market.create_verification(
                release_id=equivalent_release.release_id,
                run_id=run.run_id,
                environment=environment,
                status="passed",
                evidence_ref=source_evidence_ref,
                evidence_sha256=source_evidence_digest,
                verified_by=owner,
                request_key=f"equivalent:{request_key}",
                detail={
                    "verification_kind": "equivalent_bundle",
                    "source_contract_id": contract.contract_id,
                    "bundle_digest": bundle_digest,
                },
            )
            return self.store.create_equivalent_template_verification(
                owner=owner,
                request_key=request_key,
                source_run_id=run.run_id,
                source_contract_id=contract.contract_id,
                source_digest=contract.digest,
                bundle_digest=bundle_digest,
                release_id=equivalent_release.release_id,
                release_version=equivalent_release.release_version,
                verification_id=verification.verification_id,
                evidence_ref=source_evidence_ref,
                evidence_digest=source_evidence_digest,
                environment=environment,
            )
        session_id = _template_publication_session_id(
            owner=owner,
            request_key=request_key,
        )
        draft_id = f"draft_pub_{session_id.removeprefix('templatepub_')}"
        base_release = None
        if base_release_id is not None:
            try:
                base_release = self.template_market.get_release(base_release_id)
            except KeyError as exc:
                raise TemplatePublicationError(
                    "base release does not exist",
                    code="TEMPLATE.PUBLICATION_SOURCE_INELIGIBLE",
                ) from exc
            if base_release.publisher != owner:
                raise TemplatePublicationError(
                    "only the publisher may create a new release version",
                    code="TEMPLATE.PUBLICATION_SOURCE_INELIGIBLE",
                )
        template_id = f"template_pub_{session_id.removeprefix('templatepub_')}"
        release_publication_metadata = {
            **publication_metadata,
            "bundle_digest": bundle_digest,
        }
        if base_release is not None:
            release_publication_metadata["supersedes_release_id"] = (
                base_release.release_id
            )
            release_publication_metadata["template_family_id"] = base_release.template_id
        draft = self.template_market.create_draft(
            owner=owner,
            title=title,
            description=description,
            visibility=visibility,
            scope_key=scope_key,
            payload=sanitized_payload,
            compatibility=compatibility,
            publication=release_publication_metadata,
            draft_id=draft_id,
            template_id=template_id,
        )
        return self.store.create_template_publication(
            owner=owner,
            request_key=request_key,
            source_run_id=run.run_id,
            source_contract_id=contract.contract_id,
            source_digest=contract.digest,
            bundle_digest=bundle_digest,
            draft_id=draft.draft_id,
        )

    def record_template_reproduction(
        self,
        *,
        session_id: str,
        owner: str,
        expected_version: int,
        evidence_ref: str,
        evidence_digest: str,
        environment: str,
        release_version: str,
    ) -> TemplatePublicationSession:
        session = self.store.get_template_publication(session_id, owner=owner)
        _validate_reproduction_evidence(
            evidence_ref=evidence_ref,
            evidence_digest=evidence_digest,
            environment=environment,
        )
        confirmation_digest = _digest(
            {
                "owner": owner,
                "session_id": session.session_id,
                "bundle_digest": session.bundle_digest,
                "reproduction_evidence_digest": evidence_digest,
                "release_version": release_version,
            }
        )
        return self.store.record_template_reproduction(
            session_id,
            owner=owner,
            expected_version=expected_version,
            evidence_ref=evidence_ref,
            evidence_digest=evidence_digest,
            environment=environment,
            release_version=release_version,
            confirmation_digest=confirmation_digest,
        )

    def submit_template_publication_review(
        self,
        *,
        session_id: str,
        owner: str,
        expected_version: int,
        confirmation_digest: str,
    ) -> TemplatePublicationSession:
        if self.template_market is None:
            raise TemplatePublicationError(
                "template market is unavailable",
                code="TEMPLATE.PUBLICATION_SOURCE_INELIGIBLE",
            )
        session = self.store.get_template_publication(session_id, owner=owner)
        if session.reproduction_evidence_digest is None:
            raise TemplatePublicationError(
                "reproduction Evidence is required before review",
                code="TEMPLATE.REPRODUCTION_EVIDENCE_MISSING",
            )
        if session.confirmation_digest != confirmation_digest:
            raise TemplatePublicationError(
                "template publication confirmation is stale",
                code="TEMPLATE.PUBLICATION_CONFIRMATION_STALE",
            )
        if session.draft_id is None:
            raise TemplatePublicationError(
                "template publication draft is missing",
                code="TEMPLATE.PUBLICATION_SOURCE_STALE",
            )
        review_id = "review_pub_" + hashlib.sha256(session_id.encode()).hexdigest()[:32]
        try:
            review = self.template_market.get_review(review_id)
        except KeyError:
            draft = self.template_market.get_draft(session.draft_id, owner=owner)
            review = self.template_market.submit_review(
                draft.draft_id,
                owner=owner,
                expected_version=draft.version,
                review_id=review_id,
            )
        return self.store.mark_template_review_submitted(
            session_id,
            owner=owner,
            expected_version=expected_version,
            review_id=review.review_id,
        )

    def approve_and_publish_template(
        self,
        *,
        session_id: str,
        owner: str,
        expected_version: int,
        reviewer: TemplateReviewerPrincipal,
        release_version: str,
        request_key: str,
    ) -> TemplatePublicationSession:
        if self.template_market is None:
            raise TemplatePublicationError(
                "template market is unavailable",
                code="TEMPLATE.PUBLICATION_SOURCE_INELIGIBLE",
            )
        session = self.store.get_template_publication(session_id, owner=owner)
        if session.review_id is None or session.release_version != release_version:
            raise TemplatePublicationError(
                "review or release version does not match confirmation",
                code="TEMPLATE.PUBLICATION_CONFIRMATION_STALE",
            )
        review = self.template_market.get_review(session.review_id)
        if review.state is TemplateReviewState.PENDING:
            self.template_market.decide_review(
                review.review_id,
                principal=reviewer,
                expected_version=review.version,
                approve=True,
            )
        release = self.template_market.publish(
            review.review_id,
            owner=owner,
            release_version=release_version,
            request_key=request_key,
        )
        return self.store.complete_template_publication(
            session_id,
            owner=owner,
            expected_version=expected_version,
            release_id=release.release_id,
        )


def _template_publication_from_row(row: sqlite3.Row) -> TemplatePublicationSession:
    return TemplatePublicationSession(
        session_id=str(row["session_id"]),
        owner=str(row["owner"]),
        request_key=str(row["request_key"]),
        source_run_id=str(row["source_run_id"]),
        source_contract_id=str(row["source_contract_id"]),
        source_digest=str(row["source_digest"]),
        bundle_digest=str(row["bundle_digest"]),
        draft_id=None if row["draft_id"] is None else str(row["draft_id"]),
        state=str(row["state"]),
        version=int(row["version"]),
        reproduction_evidence_ref=(
            None
            if row["reproduction_evidence_ref"] is None
            else str(row["reproduction_evidence_ref"])
        ),
        reproduction_evidence_digest=(
            None
            if row["reproduction_evidence_digest"] is None
            else str(row["reproduction_evidence_digest"])
        ),
        reproduction_environment=(
            None
            if row["reproduction_environment"] is None
            else str(row["reproduction_environment"])
        ),
        confirmation_digest=(
            None if row["confirmation_digest"] is None else str(row["confirmation_digest"])
        ),
        review_id=None if row["review_id"] is None else str(row["review_id"]),
        release_id=None if row["release_id"] is None else str(row["release_id"]),
        release_version=(
            None if row["release_version"] is None else str(row["release_version"])
        ),
        verification_id=(
            None if row["verification_id"] is None else str(row["verification_id"])
        ),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _market_application_from_row(row: sqlite3.Row) -> MarketApplicationSession:
    return MarketApplicationSession(
        session_id=str(row["session_id"]),
        owner=str(row["owner"]),
        request_key=str(row["request_key"]),
        source_kind=MarketApplicationSourceKind(str(row["source_kind"])),
        source_item_id=str(row["source_item_id"]),
        source_digest=str(row["source_digest"]),
        assurance=MarketAssurance(str(row["assurance"])),
        user_intent=str(row["user_intent"]),
        state=str(row["state"]),
        version=int(row["version"]),
        project_id=None if row["project_id"] is None else str(row["project_id"]),
        workspace_id=None if row["workspace_id"] is None else str(row["workspace_id"]),
        change_set_id=None if row["change_set_id"] is None else str(row["change_set_id"]),
        target_contract_id=(
            None if row["target_contract_id"] is None else str(row["target_contract_id"])
        ),
        adoption_id=None if row["adoption_id"] is None else str(row["adoption_id"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode()).hexdigest()


def _validate_reproduction_evidence(
    *,
    evidence_ref: str | None,
    evidence_digest: str | None,
    environment: str,
) -> None:
    if environment not in {"docker", "real107_cpu", "real107_gpu"}:
        raise TemplatePublicationError(
            "reproduction environment is invalid",
            code="TEMPLATE.REPRODUCTION_FAILED",
        )
    if (
        evidence_ref is None
        or evidence_digest is None
        or not evidence_ref.startswith("evidence://")
        or len(evidence_digest) != 64
        or any(character not in "0123456789abcdef" for character in evidence_digest)
    ):
        raise TemplatePublicationError(
            "reproduction Evidence is invalid",
            code="TEMPLATE.REPRODUCTION_EVIDENCE_MISSING",
        )


def _market_session_id(*, owner: str, request_key: str) -> str:
    return "marketsession_" + hashlib.sha256(
        f"{owner}\0{request_key}".encode()
    ).hexdigest()[:32]


def _template_publication_session_id(*, owner: str, request_key: str) -> str:
    return "templatepub_" + hashlib.sha256(
        f"{owner}\0{request_key}".encode()
    ).hexdigest()[:32]


def _sanitize_template_value(
    value: object,
    *,
    owner: str,
    source_run_id: str,
    source_job_id: str | None,
) -> object:
    if isinstance(value, dict):
        return {
            str(key): _sanitize_template_value(
                child,
                owner=owner,
                source_run_id=source_run_id,
                source_job_id=source_job_id,
            )
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [
            _sanitize_template_value(
                item,
                owner=owner,
                source_run_id=source_run_id,
                source_job_id=source_job_id,
            )
            for item in value
        ]
    if not isinstance(value, str):
        return value
    result = value.replace(owner, "{owner}")
    result = result.replace(source_run_id, "{source_run}")
    if source_job_id is not None:
        # Slurm job IDs are short decimal strings. A global replacement would
        # corrupt unrelated Contract values such as recipe version ``1.0.0``
        # when a source Run happened to receive job 1, making semantically
        # equivalent bundles differ or fail validation. Redact the identifier
        # only when the value is the ID itself or a canonical Slurm log name.
        if result == source_job_id:
            result = "{source_job}"
        result = result.replace(
            f"slurm-{source_job_id}.",
            "slurm-{source_job}.",
        )
    return result


def _rebase_contract_workdir(
    payload: dict[str, object],
    *,
    owner: str,
) -> dict[str, object]:
    target = deepcopy(payload)
    project = target.get("project")
    if not isinstance(project, dict):
        return target
    workdir = project.get("workdir")
    if not isinstance(workdir, str):
        return target
    for root in ("/public/home", "/home"):
        prefix = f"{root}/"
        if workdir.startswith(prefix):
            _source_owner, separator, remainder = workdir[len(prefix) :].partition("/")
            project["workdir"] = f"{root}/{owner}" + (f"/{remainder}" if separator else "")
            break
    return target


def _reference_session(
    application: MarketApplicationSession,
    detail: dict[str, object],
) -> ReferenceAdaptationSession:
    target_payload = detail.get("target_contract_payload")
    if not isinstance(target_payload, dict):
        raise ValueError("reference target Contract payload is invalid")
    return ReferenceAdaptationSession(
        application=application,
        source_run_id=str(detail["source_run_id"]),
        source_contract_id=str(detail["source_contract_id"]),
        share_manifest_digest=str(detail["share_manifest_digest"]),
        target_contract_payload=target_payload,
        plan_digest=str(detail["plan_digest"]),
        confirmation_digest=str(detail["confirmation_digest"]),
        change_set_digest=(
            None
            if detail.get("change_set_digest") is None
            else str(detail["change_set_digest"])
        ),
    )


def _template_session(
    application: MarketApplicationSession,
    detail: dict[str, object],
) -> TemplateApplicationSession:
    target_payload = detail.get("target_contract_payload")
    if not isinstance(target_payload, dict):
        raise ValueError("template target Contract payload is invalid")
    return TemplateApplicationSession(
        application=application,
        release_id=str(detail["release_id"]),
        template_id=str(detail["template_id"]),
        release_version=str(detail["release_version"]),
        bundle_digest=str(detail["bundle_digest"]),
        target_contract_payload=target_payload,
        plan_digest=str(detail["plan_digest"]),
        confirmation_digest=str(detail["confirmation_digest"]),
        change_set_digest=(
            None
            if detail.get("change_set_digest") is None
            else str(detail["change_set_digest"])
        ),
    )
