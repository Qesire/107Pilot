"""Template draft, review, immutable release, and adoption state machines."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from copy import deepcopy
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import uuid4

from pilot107.core.contracts import ContractService
from pilot107.core.pagination import CursorPosition
from pilot107.core.run_store import utc_now_iso
from pilot107.core.schema_migrations import apply_schema_migrations
from pilot107.core.template_market_migrations import TEMPLATE_MARKET_MIGRATIONS
from pilot107.core.template_policy import (
    TemplateGateResult,
    TemplatePublicationGate,
    TemplateReviewerPrincipal,
    TemplateReviewerRole,
    authorize_template_review,
)

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_REQUEST_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_RELEASE_VERSION = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")


class TemplateVisibility(StrEnum):
    PRIVATE = "private"
    COURSE = "course"
    CAMPUS = "campus"
    PUBLIC = "public"


class TemplateDraftState(StrEnum):
    EDITABLE = "editable"
    SUBMITTED = "submitted"
    APPROVED = "approved"
    REJECTED = "rejected"
    PUBLISHED = "published"
    ARCHIVED = "archived"


class TemplateReviewState(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"


class TemplateMarketError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        code: str,
        findings: tuple[dict[str, Any], ...] = (),
    ) -> None:
        super().__init__(message)
        self.code = code
        self.findings = findings


@dataclass(frozen=True)
class TemplateDraftRecord:
    draft_id: str
    template_id: str
    owner: str
    title: str
    description: str
    visibility: TemplateVisibility
    scope_key: str | None
    state: TemplateDraftState
    version: int
    payload: dict[str, Any]
    compatibility: dict[str, Any]
    publication: dict[str, Any]
    content_sha256: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class TemplateReviewRecord:
    review_id: str
    draft_id: str
    requester: str
    reviewer: str | None
    reviewer_role: str | None
    reviewer_scope_key: str | None
    state: TemplateReviewState
    version: int
    draft_version: int
    content_sha256: str
    note: str | None
    gate_report: dict[str, Any]
    validated_at: str | None
    created_at: str
    updated_at: str
    decided_at: str | None


@dataclass(frozen=True)
class TemplateReleaseRecord:
    release_id: str
    template_id: str
    release_version: str
    source_draft_id: str
    source_draft_version: int
    review_id: str
    publisher: str
    request_key: str | None
    title: str
    description: str
    visibility: TemplateVisibility
    scope_key: str | None
    payload: dict[str, Any]
    compatibility: dict[str, Any]
    publication: dict[str, Any]
    gate_report: dict[str, Any]
    content_sha256: str
    published_at: str
    withdrawn_at: str | None = None
    withdrawal_actor: str | None = None
    withdrawal_reason: str | None = None


@dataclass(frozen=True)
class TemplateAdoptionRecord:
    adoption_id: str
    release_id: str
    adopter: str
    request_key: str
    target_template_id: str
    target_draft_id: str
    target_contract_id: str | None
    created_at: str


@dataclass(frozen=True)
class TemplateVerificationRecord:
    verification_id: str
    release_id: str
    run_id: str | None
    environment: str
    status: str
    evidence_ref: str | None
    evidence_sha256: str | None
    verified_by: str | None
    request_key: str | None
    detail: dict[str, Any]
    verified_at: str


@dataclass(frozen=True)
class TemplateMetricsRecord:
    adoption_count: int
    verification_passed: int
    verification_failed: int
    verification_expired: int
    latest_verification: TemplateVerificationRecord | None


@dataclass(frozen=True)
class TemplateMarketItemRecord:
    release: TemplateReleaseRecord
    metrics: TemplateMetricsRecord


@dataclass(frozen=True)
class TemplateReviewQueueRecord:
    review: TemplateReviewRecord
    draft_title: str
    visibility: TemplateVisibility
    scope_key: str | None


class TemplateMarketStore:
    def __init__(
        self,
        db_path: Path,
        *,
        publication_gate: TemplatePublicationGate | None = None,
        contract_service: ContractService | None = None,
    ) -> None:
        self.db_path = db_path
        self.publication_gate = publication_gate
        self.contract_service = contract_service
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            apply_schema_migrations(conn, TEMPLATE_MARKET_MIGRATIONS)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 10000")
        return conn

    def create_draft(
        self,
        *,
        owner: str,
        title: str,
        description: str,
        visibility: TemplateVisibility,
        payload: dict[str, Any],
        compatibility: dict[str, Any] | None = None,
        publication: dict[str, Any] | None = None,
        scope_key: str | None = None,
        draft_id: str | None = None,
        template_id: str | None = None,
    ) -> TemplateDraftRecord:
        _validate_actor(owner)
        _validate_text(title, field="title", limit=160, required=True)
        _validate_text(description, field="description", limit=4000, required=False)
        _validate_scope(visibility, scope_key)
        _validate_mapping(payload, field="payload")
        normalized_compatibility = compatibility or {}
        _validate_mapping(normalized_compatibility, field="compatibility")
        normalized_publication = publication or {}
        _validate_mapping(normalized_publication, field="publication")
        resolved_draft_id = draft_id or f"draft_{uuid4().hex}"
        resolved_template_id = template_id or f"template_{uuid4().hex}"
        _validate_id(resolved_draft_id, field="draft_id")
        _validate_id(resolved_template_id, field="template_id")
        now = utc_now_iso()
        digest = _content_digest(
            title=title,
            description=description,
            visibility=visibility,
            scope_key=scope_key,
            payload=payload,
            compatibility=normalized_compatibility,
            publication=normalized_publication,
        )
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO template_drafts (
                    draft_id, template_id, owner, title, description, visibility,
                    scope_key, state, version, payload_json, compatibility_json,
                    publication_json, content_sha256, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?)
                """,
                (
                    resolved_draft_id,
                    resolved_template_id,
                    owner,
                    title.strip(),
                    description.strip(),
                    visibility.value,
                    scope_key,
                    TemplateDraftState.EDITABLE.value,
                    _canonical_json(payload),
                    _canonical_json(normalized_compatibility),
                    _canonical_json(normalized_publication),
                    digest,
                    now,
                    now,
                ),
            )
        return self.get_draft(resolved_draft_id, owner=owner)

    def get_draft(self, draft_id: str, *, owner: str) -> TemplateDraftRecord:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM template_drafts WHERE draft_id = ? AND owner = ?",
                (draft_id, owner),
            ).fetchone()
        if row is None:
            raise KeyError(draft_id)
        return _row_to_draft(row)

    def list_drafts_page(
        self,
        *,
        owner: str,
        cursor: CursorPosition | None = None,
        limit: int = 50,
    ) -> tuple[tuple[TemplateDraftRecord, ...], CursorPosition | None]:
        _validate_actor(owner)
        if limit <= 0 or limit > 100:
            raise TemplateMarketError(
                "limit must be between 1 and 100",
                code="TEMPLATE.LIMIT_INVALID",
            )
        conditions = ["owner = ?"]
        values: list[object] = [owner]
        if cursor is not None:
            conditions.append("(updated_at < ? OR (updated_at = ? AND draft_id < ?))")
            values.extend([cursor.primary, cursor.primary, cursor.secondary])
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM template_drafts WHERE "
                + " AND ".join(conditions)
                + " ORDER BY updated_at DESC, draft_id DESC LIMIT ?",
                (*values, limit + 1),
            ).fetchall()
        selected = rows[:limit]
        records = tuple(_row_to_draft(row) for row in selected)
        next_position = None
        if len(rows) > limit and selected:
            last = selected[-1]
            next_position = CursorPosition(
                primary=str(last["updated_at"]),
                secondary=str(last["draft_id"]),
            )
        return records, next_position

    def list_review_queue_page(
        self,
        *,
        principal: TemplateReviewerPrincipal,
        cursor: CursorPosition | None = None,
        limit: int = 50,
    ) -> tuple[tuple[TemplateReviewQueueRecord, ...], CursorPosition | None]:
        _validate_actor(principal.actor)
        if limit <= 0 or limit > 100:
            raise TemplateMarketError(
                "limit must be between 1 and 100",
                code="TEMPLATE.LIMIT_INVALID",
            )
        access_conditions: list[str] = []
        values: list[object] = [principal.actor]
        if TemplateReviewerRole.ADMIN in principal.roles:
            access_conditions.append("1 = 1")
        if TemplateReviewerRole.REVIEWER in principal.roles:
            access_conditions.append("drafts.visibility != 'course'")
        course_roles = {
            TemplateReviewerRole.COURSE_INSTRUCTOR,
            TemplateReviewerRole.COURSE_TA,
        }
        if principal.roles & course_roles and principal.course_scopes:
            placeholders = ", ".join("?" for _ in principal.course_scopes)
            access_conditions.append(
                f"(drafts.visibility = 'course' AND drafts.scope_key IN ({placeholders}))"
            )
            values.extend(sorted(principal.course_scopes))
        if not access_conditions:
            return (), None
        conditions = [
            "reviews.state = 'pending'",
            "reviews.requester != ?",
            "(" + " OR ".join(access_conditions) + ")",
        ]
        if cursor is not None:
            conditions.append(
                "(reviews.created_at > ? OR (reviews.created_at = ? AND reviews.review_id > ?))"
            )
            values.extend([cursor.primary, cursor.primary, cursor.secondary])
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT reviews.*, drafts.title AS draft_title, "
                "drafts.visibility AS draft_visibility, drafts.scope_key AS draft_scope_key "
                "FROM template_reviews AS reviews "
                "JOIN template_drafts AS drafts USING (draft_id) WHERE "
                + " AND ".join(conditions)
                + " ORDER BY reviews.created_at, reviews.review_id LIMIT ?",
                (*values, limit + 1),
            ).fetchall()
        selected = rows[:limit]
        records = tuple(_row_to_review_queue(row) for row in selected)
        next_position = None
        if len(rows) > limit and selected:
            last = selected[-1]
            next_position = CursorPosition(
                primary=str(last["created_at"]),
                secondary=str(last["review_id"]),
            )
        return records, next_position

    def validate_draft(self, draft_id: str, *, owner: str) -> TemplateGateResult:
        if self.publication_gate is None:
            raise TemplateMarketError(
                "publication gate is not configured",
                code="TEMPLATE.GATE_UNAVAILABLE",
            )
        draft = self.get_draft(draft_id, owner=owner)
        return self.publication_gate.validate(
            payload=draft.payload,
            compatibility=draft.compatibility,
            publication=draft.publication,
        )

    def update_draft(
        self,
        draft_id: str,
        *,
        owner: str,
        expected_version: int,
        title: str,
        description: str,
        visibility: TemplateVisibility,
        payload: dict[str, Any],
        compatibility: dict[str, Any],
        publication: dict[str, Any],
        scope_key: str | None = None,
    ) -> TemplateDraftRecord:
        _validate_text(title, field="title", limit=160, required=True)
        _validate_text(description, field="description", limit=4000, required=False)
        _validate_scope(visibility, scope_key)
        _validate_mapping(payload, field="payload")
        _validate_mapping(compatibility, field="compatibility")
        _validate_mapping(publication, field="publication")
        digest = _content_digest(
            title=title,
            description=description,
            visibility=visibility,
            scope_key=scope_key,
            payload=payload,
            compatibility=compatibility,
            publication=publication,
        )
        now = utc_now_iso()
        with self.connect() as conn:
            result = conn.execute(
                """
                UPDATE template_drafts
                SET title = ?, description = ?, visibility = ?, scope_key = ?,
                    state = 'editable', version = version + 1, payload_json = ?,
                    compatibility_json = ?, publication_json = ?, content_sha256 = ?,
                    updated_at = ?
                WHERE draft_id = ? AND owner = ? AND version = ?
                  AND state IN ('editable', 'rejected', 'published')
                """,
                (
                    title.strip(),
                    description.strip(),
                    visibility.value,
                    scope_key,
                    _canonical_json(payload),
                    _canonical_json(compatibility),
                    _canonical_json(publication),
                    digest,
                    now,
                    draft_id,
                    owner,
                    expected_version,
                ),
            )
            if result.rowcount != 1:
                raise TemplateMarketError(
                    "draft version, owner, or state changed",
                    code="TEMPLATE.DRAFT_CONFLICT",
                )
        return self.get_draft(draft_id, owner=owner)

    def submit_review(
        self,
        draft_id: str,
        *,
        owner: str,
        expected_version: int,
        review_id: str | None = None,
    ) -> TemplateReviewRecord:
        if self.publication_gate is None:
            raise TemplateMarketError(
                "publication gate is not configured",
                code="TEMPLATE.GATE_UNAVAILABLE",
            )
        resolved_review_id = review_id or f"review_{uuid4().hex}"
        _validate_id(resolved_review_id, field="review_id")
        now = utc_now_iso()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM template_drafts WHERE draft_id = ? AND owner = ?",
                (draft_id, owner),
            ).fetchone()
            if (
                row is None
                or int(row["version"]) != expected_version
                or str(row["state"]) not in {"editable", "rejected"}
            ):
                raise TemplateMarketError(
                    "draft is not submit-ready",
                    code="TEMPLATE.DRAFT_CONFLICT",
                )
            gate_result = self.publication_gate.validate(
                payload=json.loads(str(row["payload_json"])),
                compatibility=json.loads(str(row["compatibility_json"])),
                publication=json.loads(str(row["publication_json"])),
            )
            if gate_result.status == "BLOCK":
                raise TemplateMarketError(
                    "draft failed the publication gate",
                    code="TEMPLATE.PUBLICATION_BLOCKED",
                    findings=tuple(finding.as_payload() for finding in gate_result.findings),
                )
            conn.execute(
                "UPDATE template_drafts SET state = 'submitted', updated_at = ? WHERE draft_id = ?",
                (now, draft_id),
            )
            conn.execute(
                """
                INSERT INTO template_reviews (
                    review_id, draft_id, requester, reviewer, state, version,
                    draft_version, content_sha256, note, created_at, updated_at, decided_at,
                    reviewer_role, reviewer_scope_key, gate_report_json, validated_at
                ) VALUES (
                    ?, ?, ?, NULL, 'pending', 1, ?, ?, NULL, ?, ?, NULL,
                    NULL, NULL, ?, ?
                )
                """,
                (
                    resolved_review_id,
                    draft_id,
                    owner,
                    expected_version,
                    str(row["content_sha256"]),
                    now,
                    now,
                    _canonical_json(gate_result.as_payload()),
                    now,
                ),
            )
        return self.get_review(resolved_review_id)

    def get_review(self, review_id: str) -> TemplateReviewRecord:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM template_reviews WHERE review_id = ?",
                (review_id,),
            ).fetchone()
        if row is None:
            raise KeyError(review_id)
        return _row_to_review(row)

    def decide_review(
        self,
        review_id: str,
        *,
        principal: TemplateReviewerPrincipal,
        expected_version: int,
        approve: bool,
        note: str | None = None,
    ) -> TemplateReviewRecord:
        _validate_actor(principal.actor)
        _validate_text(note or "", field="note", limit=2000, required=False)
        state = TemplateReviewState.APPROVED if approve else TemplateReviewState.REJECTED
        draft_state = TemplateDraftState.APPROVED if approve else TemplateDraftState.REJECTED
        now = utc_now_iso()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT reviews.*, drafts.visibility, drafts.scope_key, drafts.owner
                FROM template_reviews AS reviews
                JOIN template_drafts AS drafts USING (draft_id)
                WHERE review_id = ?
                """,
                (review_id,),
            ).fetchone()
            if (
                row is None
                or int(row["version"]) != expected_version
                or str(row["state"]) != TemplateReviewState.PENDING.value
            ):
                raise TemplateMarketError(
                    "review version or state changed",
                    code="TEMPLATE.REVIEW_CONFLICT",
                )
            try:
                reviewer_role, reviewer_scope_key = authorize_template_review(
                    principal=principal,
                    requester=str(row["requester"]),
                    visibility=str(row["visibility"]),
                    scope_key=(None if row["scope_key"] is None else str(row["scope_key"])),
                )
            except PermissionError as exc:
                raise TemplateMarketError(
                    str(exc),
                    code=(
                        "TEMPLATE.SELF_REVIEW_FORBIDDEN"
                        if principal.actor == str(row["requester"])
                        else "TEMPLATE.REVIEW_FORBIDDEN"
                    ),
                ) from exc
            result = conn.execute(
                """
                UPDATE template_reviews
                SET reviewer = ?, reviewer_role = ?, reviewer_scope_key = ?, state = ?,
                    version = version + 1, note = ?, updated_at = ?, decided_at = ?
                WHERE review_id = ? AND version = ? AND state = 'pending'
                """,
                (
                    principal.actor,
                    reviewer_role.value,
                    reviewer_scope_key,
                    state.value,
                    _optional_text(note),
                    now,
                    now,
                    review_id,
                    expected_version,
                ),
            )
            if result.rowcount != 1:
                raise TemplateMarketError(
                    "review version or state changed",
                    code="TEMPLATE.REVIEW_CONFLICT",
                )
            conn.execute(
                "UPDATE template_drafts SET state = ?, updated_at = ? WHERE draft_id = ?",
                (draft_state.value, now, str(row["draft_id"])),
            )
        return self.get_review(review_id)

    def publish(
        self,
        review_id: str,
        *,
        owner: str,
        release_version: str,
        request_key: str | None = None,
        release_id: str | None = None,
    ) -> TemplateReleaseRecord:
        if self.publication_gate is None:
            raise TemplateMarketError(
                "publication gate is not configured",
                code="TEMPLATE.GATE_UNAVAILABLE",
            )
        if not _RELEASE_VERSION.fullmatch(release_version):
            raise TemplateMarketError(
                "release_version must be semantic major.minor.patch",
                code="TEMPLATE.RELEASE_VERSION_INVALID",
            )
        if request_key is not None and not _REQUEST_KEY.fullmatch(request_key):
            raise TemplateMarketError(
                "request_key is invalid",
                code="TEMPLATE.REQUEST_KEY_INVALID",
            )
        resolved_release_id = release_id or f"release_{uuid4().hex}"
        _validate_id(resolved_release_id, field="release_id")
        now = utc_now_iso()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if request_key is not None:
                existing = conn.execute(
                    """
                    SELECT releases.*, withdrawals.withdrawn_at,
                           withdrawals.actor AS withdrawal_actor,
                           withdrawals.reason AS withdrawal_reason
                    FROM template_releases AS releases
                    LEFT JOIN template_release_withdrawals AS withdrawals USING (release_id)
                    WHERE publisher = ? AND request_key = ?
                    """,
                    (owner, request_key),
                ).fetchone()
                if existing is not None:
                    if (
                        str(existing["review_id"]) != review_id
                        or str(existing["release_version"]) != release_version
                    ):
                        raise TemplateMarketError(
                            "request_key was used for another release",
                            code="TEMPLATE.IDEMPOTENCY_CONFLICT",
                        )
                    return _row_to_release(existing)
            review = conn.execute(
                "SELECT * FROM template_reviews WHERE review_id = ?",
                (review_id,),
            ).fetchone()
            if review is None or str(review["state"]) != TemplateReviewState.APPROVED.value:
                raise TemplateMarketError(
                    "review is not approved",
                    code="TEMPLATE.REVIEW_NOT_APPROVED",
                )
            gate_report = json.loads(str(review["gate_report_json"]))
            if gate_report.get("status") != "OK" or review["reviewer_role"] is None:
                raise TemplateMarketError(
                    "approved review does not carry a passing publication gate",
                    code="TEMPLATE.PUBLICATION_GATE_STALE",
                )
            draft = conn.execute(
                "SELECT * FROM template_drafts WHERE draft_id = ? AND owner = ?",
                (str(review["draft_id"]), owner),
            ).fetchone()
            if (
                draft is None
                or str(draft["state"]) != TemplateDraftState.APPROVED.value
                or int(draft["version"]) != int(review["draft_version"])
                or str(draft["content_sha256"]) != str(review["content_sha256"])
            ):
                raise TemplateMarketError(
                    "approved draft no longer matches the review snapshot",
                    code="TEMPLATE.REVIEW_STALE",
                )
            current_gate = self.publication_gate.validate(
                payload=json.loads(str(draft["payload_json"])),
                compatibility=json.loads(str(draft["compatibility_json"])),
                publication=json.loads(str(draft["publication_json"])),
            )
            if current_gate.status == "BLOCK":
                raise TemplateMarketError(
                    "approved draft no longer passes the publication gate",
                    code="TEMPLATE.PUBLICATION_BLOCKED",
                    findings=tuple(finding.as_payload() for finding in current_gate.findings),
                )
            publication_payload = json.loads(str(draft["publication_json"]))
            release_template_id = publication_payload.get("template_family_id")
            if release_template_id is None:
                release_template_id = str(draft["template_id"])
            if not isinstance(release_template_id, str):
                raise TemplateMarketError(
                    "template family identifier is invalid",
                    code="TEMPLATE.PUBLICATION_BLOCKED",
                )
            _validate_id(release_template_id, field="template_id")
            try:
                conn.execute(
                    """
                    INSERT INTO template_releases (
                        release_id, template_id, release_version, source_draft_id,
                        source_draft_version, review_id, publisher, request_key,
                        title, description,
                        visibility, scope_key, payload_json, compatibility_json,
                        publication_json, gate_report_json, content_sha256, published_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        resolved_release_id,
                        release_template_id,
                        release_version,
                        str(draft["draft_id"]),
                        int(draft["version"]),
                        review_id,
                        owner,
                        request_key,
                        str(draft["title"]),
                        str(draft["description"]),
                        str(draft["visibility"]),
                        draft["scope_key"],
                        str(draft["payload_json"]),
                        str(draft["compatibility_json"]),
                        str(draft["publication_json"]),
                        _canonical_json(current_gate.as_payload()),
                        str(draft["content_sha256"]),
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise TemplateMarketError(
                    "release version or review already published",
                    code="TEMPLATE.RELEASE_CONFLICT",
                ) from exc
            conn.execute(
                "UPDATE template_drafts SET state = 'published', updated_at = ? WHERE draft_id = ?",
                (now, str(draft["draft_id"])),
            )
        return self.get_release(resolved_release_id)

    def get_release(self, release_id: str) -> TemplateReleaseRecord:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT releases.*, withdrawals.withdrawn_at,
                       withdrawals.actor AS withdrawal_actor,
                       withdrawals.reason AS withdrawal_reason
                FROM template_releases AS releases
                LEFT JOIN template_release_withdrawals AS withdrawals USING (release_id)
                WHERE releases.release_id = ?
                """,
                (release_id,),
            ).fetchone()
        if row is None:
            raise KeyError(release_id)
        return _row_to_release(row)

    def find_active_release_by_bundle_digest(
        self,
        bundle_digest: str,
    ) -> TemplateReleaseRecord | None:
        """Return the immutable active release for an equivalent semantic bundle."""

        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT releases.*, withdrawals.withdrawn_at,
                       withdrawals.actor AS withdrawal_actor,
                       withdrawals.reason AS withdrawal_reason
                FROM template_releases AS releases
                LEFT JOIN template_release_withdrawals AS withdrawals USING (release_id)
                WHERE withdrawals.release_id IS NULL
                  AND json_extract(releases.publication_json, '$.bundle_digest') = ?
                ORDER BY releases.published_at, releases.release_id
                LIMIT 1
                """,
                (bundle_digest,),
            ).fetchone()
        return None if row is None else _row_to_release(row)

    def get_release_by_version(
        self, template_id: str, release_version: str
    ) -> TemplateReleaseRecord:
        _validate_id(template_id, field="template_id")
        if not _RELEASE_VERSION.fullmatch(release_version):
            raise KeyError((template_id, release_version))
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT releases.*, withdrawals.withdrawn_at,
                       withdrawals.actor AS withdrawal_actor,
                       withdrawals.reason AS withdrawal_reason
                FROM template_releases AS releases
                LEFT JOIN template_release_withdrawals AS withdrawals USING (release_id)
                WHERE template_id = ? AND release_version = ?
                """,
                (template_id, release_version),
            ).fetchone()
        if row is None:
            raise KeyError((template_id, release_version))
        return _row_to_release(row)

    def list_market_page(
        self,
        *,
        actor: str,
        course_scopes: frozenset[str] = frozenset(),
        query: str | None = None,
        visibility: TemplateVisibility | None = None,
        partition: str | None = None,
        gpu: bool | None = None,
        verification_environment: str | None = None,
        verified_only: bool = False,
        cursor: CursorPosition | None = None,
        limit: int = 50,
    ) -> tuple[tuple[TemplateMarketItemRecord, ...], CursorPosition | None]:
        if actor:
            _validate_actor(actor)
        if limit <= 0 or limit > 100:
            raise TemplateMarketError(
                "limit must be between 1 and 100",
                code="TEMPLATE.LIMIT_INVALID",
            )
        visibility_conditions = [
            "releases.visibility IN ('campus', 'public')",
            "releases.publisher = ?",
        ]
        values: list[object] = [actor]
        if course_scopes:
            placeholders = ", ".join("?" for _ in course_scopes)
            visibility_conditions.append(
                f"(releases.visibility = 'course' AND releases.scope_key IN ({placeholders}))"
            )
            values.extend(sorted(course_scopes))
        conditions = [
            "withdrawals.release_id IS NULL",
            "(" + " OR ".join(visibility_conditions) + ")",
        ]
        if visibility is not None:
            conditions.append("releases.visibility = ?")
            values.append(visibility.value)
        if query is not None:
            pattern = f"%{_escape_like(query)}%"
            conditions.append(
                "(releases.title LIKE ? ESCAPE '\\' "
                "OR releases.description LIKE ? ESCAPE '\\' "
                "OR releases.template_id LIKE ? ESCAPE '\\')"
            )
            values.extend([pattern, pattern, pattern])
        if partition is not None:
            conditions.append(
                "EXISTS (SELECT 1 FROM json_each(releases.compatibility_json, "
                "'$.partitions') WHERE value = ?)"
            )
            values.append(partition)
        if gpu is not None:
            conditions.append("json_extract(releases.compatibility_json, '$.gpu') = ?")
            values.append(1 if gpu else 0)
        if verification_environment is not None:
            conditions.append(
                "EXISTS (SELECT 1 FROM template_verifications AS environment_verification "
                "WHERE environment_verification.release_id = releases.release_id "
                "AND environment_verification.environment = ? "
                "AND environment_verification.status = 'passed')"
            )
            values.append(verification_environment)
        market_query = (
            """
            WITH market AS (
                SELECT releases.*, withdrawals.withdrawn_at,
                       withdrawals.actor AS withdrawal_actor,
                       withdrawals.reason AS withdrawal_reason,
                       COUNT(DISTINCT adoptions.adoption_id) AS adoption_count,
                       COUNT(DISTINCT CASE WHEN verifications.status = 'passed'
                                          THEN verifications.verification_id END)
                           AS verification_passed,
                       COUNT(DISTINCT CASE WHEN verifications.status = 'failed'
                                          THEN verifications.verification_id END)
                           AS verification_failed,
                       COUNT(DISTINCT CASE WHEN verifications.status = 'expired'
                                          THEN verifications.verification_id END)
                           AS verification_expired,
                       COALESCE(MAX(CASE WHEN verifications.status = 'passed'
                                         THEN verifications.verified_at END), '')
                           AS latest_passed_at,
                       COALESCE(MAX(CASE
                           WHEN verifications.status = 'passed'
                                AND verifications.environment = 'real107_gpu' THEN 3
                           WHEN verifications.status = 'passed'
                                AND verifications.environment = 'real107_cpu' THEN 2
                           WHEN verifications.status = 'passed'
                                AND verifications.environment = 'docker' THEN 1
                           ELSE 0 END), 0) AS verification_tier
                FROM template_releases AS releases
                LEFT JOIN template_release_withdrawals AS withdrawals USING (release_id)
                LEFT JOIN template_adoptions AS adoptions USING (release_id)
                LEFT JOIN template_verifications AS verifications USING (release_id)
                WHERE """
            + " AND ".join(conditions)
            # SQLite accepts ungrouped columns from the one-to-one withdrawal
            # join, but PostgreSQL correctly requires them to be explicit.
            # Keeping these columns in the group also preserves the existing
            # market-row shape across both storage engines.
            + " GROUP BY releases.release_id, withdrawals.withdrawn_at, "
            "withdrawals.actor, withdrawals.reason) SELECT * FROM market"
        )
        outer_conditions: list[str] = []
        outer_values: list[object] = []
        if verified_only:
            outer_conditions.append("verification_tier > 0")
        if cursor is not None:
            tier, verified_at, adoption_count, published_at = _decode_market_cursor(cursor)
            outer_conditions.append(
                "(verification_tier < ? "
                "OR (verification_tier = ? AND latest_passed_at < ?) "
                "OR (verification_tier = ? AND latest_passed_at = ? "
                "AND adoption_count < ?) "
                "OR (verification_tier = ? AND latest_passed_at = ? "
                "AND adoption_count = ? AND published_at < ?) "
                "OR (verification_tier = ? AND latest_passed_at = ? "
                "AND adoption_count = ? AND published_at = ? AND release_id < ?))"
            )
            outer_values.extend(
                [
                    tier,
                    tier,
                    verified_at,
                    tier,
                    verified_at,
                    adoption_count,
                    tier,
                    verified_at,
                    adoption_count,
                    published_at,
                    tier,
                    verified_at,
                    adoption_count,
                    published_at,
                    cursor.secondary,
                ]
            )
        if outer_conditions:
            market_query += " WHERE " + " AND ".join(outer_conditions)
        market_query += (
            " ORDER BY verification_tier DESC, latest_passed_at DESC, "
            "adoption_count DESC, published_at DESC, release_id DESC LIMIT ?"
        )
        with self.connect() as conn:
            rows = conn.execute(
                market_query,
                (*values, *outer_values, limit + 1),
            ).fetchall()
            selected = rows[:limit]
            records = tuple(
                TemplateMarketItemRecord(
                    release=_row_to_release(row),
                    metrics=TemplateMetricsRecord(
                        adoption_count=int(row["adoption_count"]),
                        verification_passed=int(row["verification_passed"]),
                        verification_failed=int(row["verification_failed"]),
                        verification_expired=int(row["verification_expired"]),
                        latest_verification=_latest_verification(conn, str(row["release_id"])),
                    ),
                )
                for row in selected
            )
        next_position = None
        if len(rows) > limit and selected:
            last = selected[-1]
            next_position = CursorPosition(
                primary=_canonical_json(
                    {
                        "verification_tier": int(last["verification_tier"]),
                        "latest_passed_at": str(last["latest_passed_at"]),
                        "adoption_count": int(last["adoption_count"]),
                        "published_at": str(last["published_at"]),
                    }
                ),
                secondary=str(last["release_id"]),
            )
        return records, next_position

    def list_market_chronological_page(
        self,
        *,
        actor: str,
        course_scopes: frozenset[str] = frozenset(),
        query: str | None = None,
        visibility: TemplateVisibility | None = None,
        cursor: CursorPosition | None = None,
        limit: int = 50,
    ) -> tuple[tuple[TemplateMarketItemRecord, ...], CursorPosition | None]:
        """List visible releases by publication time for the unified market.

        The template-only market keeps its verification/adoption ranking.  A
        cross-kind feed cannot reuse that cursor because successful Run
        publications do not have those ranking fields, so the unified read
        model deliberately uses the common ``published_at + item_id`` order.
        """

        if actor:
            _validate_actor(actor)
        if limit <= 0 or limit > 100:
            raise TemplateMarketError(
                "limit must be between 1 and 100",
                code="TEMPLATE.LIMIT_INVALID",
            )
        visibility_conditions = [
            "releases.visibility IN ('campus', 'public')",
            "releases.publisher = ?",
        ]
        values: list[object] = [actor]
        if course_scopes:
            placeholders = ", ".join("?" for _ in course_scopes)
            visibility_conditions.append(
                f"(releases.visibility = 'course' AND releases.scope_key IN ({placeholders}))"
            )
            values.extend(sorted(course_scopes))
        conditions = [
            "withdrawals.release_id IS NULL",
            "(" + " OR ".join(visibility_conditions) + ")",
        ]
        if visibility is not None:
            conditions.append("releases.visibility = ?")
            values.append(visibility.value)
        if query is not None and query.strip():
            pattern = f"%{_escape_like(query.strip())}%"
            conditions.append(
                "(releases.title LIKE ? ESCAPE '\\' "
                "OR releases.description LIKE ? ESCAPE '\\' "
                "OR releases.template_id LIKE ? ESCAPE '\\')"
            )
            values.extend([pattern, pattern, pattern])
        if cursor is not None:
            conditions.append(
                "(releases.published_at < ? "
                "OR (releases.published_at = ? AND releases.release_id < ?))"
            )
            values.extend([cursor.primary, cursor.primary, cursor.secondary])
        query_sql = (
            """
            SELECT releases.*, withdrawals.withdrawn_at,
                   withdrawals.actor AS withdrawal_actor,
                   withdrawals.reason AS withdrawal_reason,
                   COUNT(DISTINCT adoptions.adoption_id) AS adoption_count,
                   COUNT(DISTINCT CASE WHEN verifications.status = 'passed'
                                      THEN verifications.verification_id END)
                       AS verification_passed,
                   COUNT(DISTINCT CASE WHEN verifications.status = 'failed'
                                      THEN verifications.verification_id END)
                       AS verification_failed,
                   COUNT(DISTINCT CASE WHEN verifications.status = 'expired'
                                      THEN verifications.verification_id END)
                       AS verification_expired
            FROM template_releases AS releases
            LEFT JOIN template_release_withdrawals AS withdrawals USING (release_id)
            LEFT JOIN template_adoptions AS adoptions USING (release_id)
            LEFT JOIN template_verifications AS verifications USING (release_id)
            WHERE """
            + " AND ".join(conditions)
            + " GROUP BY releases.release_id, withdrawals.withdrawn_at, "
            "withdrawals.actor, withdrawals.reason "
            "ORDER BY releases.published_at DESC, releases.release_id DESC LIMIT ?"
        )
        with self.connect() as conn:
            rows = conn.execute(query_sql, (*values, limit + 1)).fetchall()
            selected = rows[:limit]
            records = tuple(_row_to_market_item(conn, row) for row in selected)
        next_position = (
            CursorPosition(
                primary=str(selected[-1]["published_at"]),
                secondary=str(selected[-1]["release_id"]),
            )
            if len(rows) > limit and selected
            else None
        )
        return records, next_position

    def get_market_item(self, release_id: str) -> TemplateMarketItemRecord:
        """Return one release with the metrics used by both market read models."""

        _validate_id(release_id, field="release_id")
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT releases.*, withdrawals.withdrawn_at,
                       withdrawals.actor AS withdrawal_actor,
                       withdrawals.reason AS withdrawal_reason,
                       COUNT(DISTINCT adoptions.adoption_id) AS adoption_count,
                       COUNT(DISTINCT CASE WHEN verifications.status = 'passed'
                                          THEN verifications.verification_id END)
                           AS verification_passed,
                       COUNT(DISTINCT CASE WHEN verifications.status = 'failed'
                                          THEN verifications.verification_id END)
                           AS verification_failed,
                       COUNT(DISTINCT CASE WHEN verifications.status = 'expired'
                                          THEN verifications.verification_id END)
                           AS verification_expired
                FROM template_releases AS releases
                LEFT JOIN template_release_withdrawals AS withdrawals USING (release_id)
                LEFT JOIN template_adoptions AS adoptions USING (release_id)
                LEFT JOIN template_verifications AS verifications USING (release_id)
                WHERE releases.release_id = ?
                GROUP BY releases.release_id, withdrawals.withdrawn_at,
                         withdrawals.actor, withdrawals.reason
                """,
                (release_id,),
            ).fetchone()
            if row is None:
                raise KeyError(release_id)
            return _row_to_market_item(conn, row)

    def withdraw_release(self, release_id: str, *, actor: str, reason: str) -> None:
        _validate_actor(actor)
        _validate_text(reason, field="reason", limit=2000, required=True)
        release = self.get_release(release_id)
        if release.publisher != actor:
            raise TemplateMarketError(
                "only the publisher can withdraw a release",
                code="TEMPLATE.FORBIDDEN",
            )
        try:
            with self.connect() as conn:
                conn.execute(
                    "INSERT INTO template_release_withdrawals "
                    "(release_id, actor, reason, withdrawn_at) VALUES (?, ?, ?, ?)",
                    (release_id, actor, reason.strip(), utc_now_iso()),
                )
        except sqlite3.IntegrityError as exc:
            raise TemplateMarketError(
                "release is already withdrawn",
                code="TEMPLATE.RELEASE_WITHDRAWN",
            ) from exc

    def get_adoption_for_contract(
        self,
        *,
        release_id: str,
        adopter: str,
        contract_id: str,
    ) -> TemplateAdoptionRecord:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM template_adoptions
                WHERE release_id = ? AND adopter = ? AND target_contract_id = ?
                """,
                (release_id, adopter, contract_id),
            ).fetchone()
        if row is None:
            raise KeyError((release_id, adopter, contract_id))
        return _row_to_adoption(row)

    def create_verification(
        self,
        *,
        release_id: str,
        run_id: str,
        environment: str,
        status: str,
        evidence_ref: str,
        evidence_sha256: str,
        verified_by: str,
        request_key: str,
        detail: dict[str, Any],
    ) -> TemplateVerificationRecord:
        _validate_actor(verified_by)
        _validate_id(run_id, field="run_id")
        if not _REQUEST_KEY.fullmatch(request_key):
            raise TemplateMarketError(
                "request_key is invalid",
                code="TEMPLATE.REQUEST_KEY_INVALID",
            )
        if environment not in {"docker", "real107_cpu", "real107_gpu"}:
            raise TemplateMarketError(
                "verification environment is invalid",
                code="TEMPLATE.VERIFICATION_ENVIRONMENT_INVALID",
            )
        if status not in {"passed", "failed"}:
            raise TemplateMarketError(
                "verification status must be derived as passed or failed",
                code="TEMPLATE.VERIFICATION_STATUS_INVALID",
            )
        _validate_mapping(detail, field="detail")
        token = hashlib.sha256(f"{verified_by}\0{request_key}".encode()).hexdigest()[:32]
        verification_id = f"verification_{token}"
        now = utc_now_iso()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM template_verifications WHERE verified_by = ? AND request_key = ?",
                (verified_by, request_key),
            ).fetchone()
            if existing is not None:
                if (
                    str(existing["release_id"]) != release_id
                    or str(existing["run_id"]) != run_id
                    or str(existing["environment"]) != environment
                ):
                    raise TemplateMarketError(
                        "request_key was used for another verification",
                        code="TEMPLATE.IDEMPOTENCY_CONFLICT",
                    )
                return _row_to_verification(existing)
            try:
                conn.execute(
                    """
                    INSERT INTO template_verifications (
                        verification_id, release_id, run_id, environment, status,
                        evidence_ref, verified_at, verified_by, request_key,
                        evidence_sha256, detail_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        verification_id,
                        release_id,
                        run_id,
                        environment,
                        status,
                        evidence_ref,
                        now,
                        verified_by,
                        request_key,
                        evidence_sha256,
                        _canonical_json(detail),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise TemplateMarketError(
                    "run already verifies this release and environment",
                    code="TEMPLATE.VERIFICATION_CONFLICT",
                ) from exc
            row = conn.execute(
                "SELECT * FROM template_verifications WHERE verification_id = ?",
                (verification_id,),
            ).fetchone()
        if row is None:
            raise KeyError(verification_id)
        return _row_to_verification(row)

    def list_verifications(
        self, release_id: str, *, limit: int = 20
    ) -> tuple[TemplateVerificationRecord, ...]:
        if limit <= 0 or limit > 100:
            raise TemplateMarketError(
                "limit must be between 1 and 100",
                code="TEMPLATE.LIMIT_INVALID",
            )
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM template_verifications WHERE release_id = ? "
                "ORDER BY verified_at DESC, verification_id DESC LIMIT ?",
                (release_id, limit),
            ).fetchall()
        return tuple(_row_to_verification(row) for row in rows)

    def adopt_release(
        self,
        release_id: str,
        *,
        adopter: str,
        request_key: str,
        course_scopes: frozenset[str] = frozenset(),
        target_payload: dict[str, Any] | None = None,
        market_application_session_id: str | None = None,
    ) -> TemplateAdoptionRecord:
        _validate_actor(adopter)
        if self.contract_service is None or self.publication_gate is None:
            raise TemplateMarketError(
                "contract-backed adoption is not configured",
                code="TEMPLATE.CONTRACT_SERVICE_UNAVAILABLE",
            )
        if not _REQUEST_KEY.fullmatch(request_key):
            raise TemplateMarketError(
                "request_key is invalid",
                code="TEMPLATE.REQUEST_KEY_INVALID",
            )
        token = hashlib.sha256(f"{adopter}\0{request_key}".encode()).hexdigest()[:32]
        adoption_id = f"adoption_{token}"
        target_template_id = f"template_adopted_{token}"
        target_draft_id = f"draft_adopted_{token}"
        target_contract_id = f"contract_adopted_{token}"
        now = utc_now_iso()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM template_adoptions WHERE adopter = ? AND request_key = ?",
                (adopter, request_key),
            ).fetchone()
            if existing is not None:
                if str(existing["release_id"]) != release_id:
                    raise TemplateMarketError(
                        "request_key was used for another release",
                        code="TEMPLATE.IDEMPOTENCY_CONFLICT",
                    )
                return _row_to_adoption(existing)
            release_row = conn.execute(
                """
                SELECT releases.*, withdrawals.withdrawn_at,
                       withdrawals.actor AS withdrawal_actor,
                       withdrawals.reason AS withdrawal_reason
                FROM template_releases AS releases
                LEFT JOIN template_release_withdrawals AS withdrawals USING (release_id)
                WHERE releases.release_id = ?
                """,
                (release_id,),
            ).fetchone()
            if release_row is None:
                raise KeyError(release_id)
            release = _row_to_release(release_row)
            authorize_template_release(
                release,
                actor=adopter,
                course_scopes=course_scopes,
            )
            if release.gate_report.get("status") != "OK":
                raise TemplateMarketError(
                    "release does not carry a passing publication gate",
                    code="TEMPLATE.PUBLICATION_GATE_STALE",
                )
            if release.withdrawn_at is not None:
                raise TemplateMarketError(
                    "release is withdrawn",
                    code="TEMPLATE.RELEASE_WITHDRAWN",
                )
            current_gate = self.publication_gate.validate(
                payload=release.payload,
                compatibility=release.compatibility,
                publication=release.publication,
            )
            if current_gate.status == "BLOCK":
                raise TemplateMarketError(
                    "release no longer passes the current adoption gate",
                    code="TEMPLATE.PUBLICATION_BLOCKED",
                    findings=tuple(finding.as_payload() for finding in current_gate.findings),
                )
            payload = (
                _rebase_adopter_workdir(release.payload, adopter=adopter)
                if target_payload is None
                else json.loads(_canonical_json(target_payload))
            )
            validation = self.contract_service.validate(payload)
            if validation.status == "BLOCK":
                raise TemplateMarketError(
                    "release cannot produce a canonical Contract",
                    code="TEMPLATE.CONTRACT_BLOCKED",
                )
            compatibility = json.loads(_canonical_json(release.compatibility))
            payload = json.loads(_canonical_json(payload))
            digest = _content_digest(
                title=release.title,
                description=release.description,
                visibility=TemplateVisibility.PRIVATE,
                scope_key=None,
                payload=payload,
                compatibility=compatibility,
                publication=release.publication,
            )
            conn.execute(
                """
                INSERT INTO template_drafts (
                    draft_id, template_id, owner, title, description, visibility,
                    scope_key, state, version, payload_json, compatibility_json,
                    publication_json, content_sha256, created_at, updated_at
                ) VALUES (
                    ?, ?, ?, ?, ?, 'private', NULL, 'editable', 1, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    target_draft_id,
                    target_template_id,
                    adopter,
                    release.title,
                    release.description,
                    _canonical_json(payload),
                    _canonical_json(compatibility),
                    _canonical_json(release.publication),
                    digest,
                    now,
                    now,
                ),
            )
            recipe_version_id = validation.effective_request.get("recipe_version_id")
            if not isinstance(recipe_version_id, str):
                raise TemplateMarketError(
                    "validated release omitted recipe_version_id",
                    code="TEMPLATE.CONTRACT_BLOCKED",
                )
            self.contract_service.store.create_contract(
                owner=adopter,
                recipe_version_id=recipe_version_id,
                payload=payload,
                field_sources=[
                    {
                        "field": "*",
                        "source": "template_release",
                        "source_release_id": release.release_id,
                        "source_template_id": release.template_id,
                        "source_release_version": release.release_version,
                        "needs_user_confirmation": False,
                        "adopter_workdir_rebased": payload != release.payload,
                        **(
                            {
                                "market_application_session_id": market_application_session_id,
                                "assurance": "curated",
                            }
                            if market_application_session_id is not None
                            else {}
                        ),
                    }
                ],
                contract_id=target_contract_id,
                derivation_reason=(
                    "template_application"
                    if market_application_session_id is not None
                    else "template_adoption"
                ),
                idempotent=True,
                connection=conn,
            )
            conn.execute(
                """
                INSERT INTO template_adoptions (
                    adoption_id, release_id, adopter, request_key,
                    target_template_id, target_draft_id, target_contract_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    adoption_id,
                    release_id,
                    adopter,
                    request_key,
                    target_template_id,
                    target_draft_id,
                    target_contract_id,
                    now,
                ),
            )
        return TemplateAdoptionRecord(
            adoption_id=adoption_id,
            release_id=release_id,
            adopter=adopter,
            request_key=request_key,
            target_template_id=target_template_id,
            target_draft_id=target_draft_id,
            target_contract_id=target_contract_id,
            created_at=now,
        )


def authorize_template_release(
    release: TemplateReleaseRecord,
    *,
    actor: str,
    course_scopes: frozenset[str],
) -> None:
    allowed = release.publisher == actor
    allowed = allowed or release.visibility in {
        TemplateVisibility.CAMPUS,
        TemplateVisibility.PUBLIC,
    }
    allowed = allowed or (
        release.visibility == TemplateVisibility.COURSE
        and release.scope_key is not None
        and release.scope_key in course_scopes
    )
    if not allowed:
        raise TemplateMarketError(
            "release is not visible to this actor",
            code="TEMPLATE.FORBIDDEN",
        )


def template_draft_payload(record: TemplateDraftRecord) -> dict[str, Any]:
    return {
        "draft_id": record.draft_id,
        "template_id": record.template_id,
        "owner": record.owner,
        "title": record.title,
        "description": record.description,
        "visibility": record.visibility.value,
        "scope_key": record.scope_key,
        "state": record.state.value,
        "version": record.version,
        "payload": record.payload,
        "compatibility": record.compatibility,
        "publication": record.publication,
        "content_sha256": record.content_sha256,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
    }


def template_review_payload(record: TemplateReviewRecord) -> dict[str, Any]:
    return {
        "review_id": record.review_id,
        "draft_id": record.draft_id,
        "requester": record.requester,
        "reviewer": record.reviewer,
        "reviewer_role": record.reviewer_role,
        "reviewer_scope_key": record.reviewer_scope_key,
        "state": record.state.value,
        "version": record.version,
        "draft_version": record.draft_version,
        "content_sha256": record.content_sha256,
        "note": record.note,
        "gate_report": record.gate_report,
        "validated_at": record.validated_at,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "decided_at": record.decided_at,
    }


def template_review_queue_payload(record: TemplateReviewQueueRecord) -> dict[str, Any]:
    return {
        **template_review_payload(record.review),
        "draft_title": record.draft_title,
        "visibility": record.visibility.value,
        "scope_key": record.scope_key,
    }


def template_release_payload(record: TemplateReleaseRecord) -> dict[str, Any]:
    return {
        "release_id": record.release_id,
        "template_id": record.template_id,
        "release_version": record.release_version,
        "source_draft_id": record.source_draft_id,
        "source_draft_version": record.source_draft_version,
        "review_id": record.review_id,
        "publisher": record.publisher,
        "request_key": record.request_key,
        "title": record.title,
        "description": record.description,
        "visibility": record.visibility.value,
        "scope_key": record.scope_key,
        "payload": record.payload,
        "compatibility": record.compatibility,
        "publication": record.publication,
        "gate_report": record.gate_report,
        "content_sha256": record.content_sha256,
        "published_at": record.published_at,
        "withdrawn_at": record.withdrawn_at,
        "withdrawal_actor": record.withdrawal_actor,
        "withdrawal_reason": record.withdrawal_reason,
    }


def template_adoption_payload(record: TemplateAdoptionRecord) -> dict[str, Any]:
    return {
        "adoption_id": record.adoption_id,
        "release_id": record.release_id,
        "adopter": record.adopter,
        "request_key": record.request_key,
        "target_template_id": record.target_template_id,
        "target_draft_id": record.target_draft_id,
        "target_contract_id": record.target_contract_id,
        "created_at": record.created_at,
    }


def _rebase_adopter_workdir(
    payload: dict[str, Any],
    *,
    adopter: str,
) -> dict[str, Any]:
    """Copy a release payload and prevent adoption into another user's home root."""

    rebased = deepcopy(payload)
    project = rebased.get("project")
    if not isinstance(project, dict):
        return rebased
    workdir = project.get("workdir")
    if not isinstance(workdir, str):
        return rebased
    for root in ("/public/home", "/home"):
        prefix = f"{root}/"
        if workdir.startswith(prefix):
            relative = workdir[len(prefix) :]
            _owner, separator, remainder = relative.partition("/")
            suffix = f"/{remainder}" if separator else ""
            project["workdir"] = f"{root}/{adopter}{suffix}"
            break
    return rebased


def template_verification_payload(record: TemplateVerificationRecord) -> dict[str, Any]:
    return {
        "verification_id": record.verification_id,
        "release_id": record.release_id,
        "run_id": record.run_id,
        "environment": record.environment,
        "status": record.status,
        "evidence_ref": record.evidence_ref,
        "evidence_sha256": record.evidence_sha256,
        "verified_by": record.verified_by,
        "request_key": record.request_key,
        "detail": record.detail,
        "verified_at": record.verified_at,
    }


def template_market_item_payload(record: TemplateMarketItemRecord) -> dict[str, Any]:
    decided = record.metrics.verification_passed + record.metrics.verification_failed
    return {
        **template_release_payload(record.release),
        "metrics": {
            "adoption_count": record.metrics.adoption_count,
            "verification_passed": record.metrics.verification_passed,
            "verification_failed": record.metrics.verification_failed,
            "verification_expired": record.metrics.verification_expired,
            "success_rate": (
                None if decided == 0 else record.metrics.verification_passed / decided
            ),
            "latest_verification": (
                None
                if record.metrics.latest_verification is None
                else template_verification_payload(record.metrics.latest_verification)
            ),
        },
    }


def _content_digest(
    *,
    title: str,
    description: str,
    visibility: TemplateVisibility,
    scope_key: str | None,
    payload: dict[str, Any],
    compatibility: dict[str, Any],
    publication: dict[str, Any],
) -> str:
    content = {
        "title": title.strip(),
        "description": description.strip(),
        "visibility": visibility.value,
        "scope_key": scope_key,
        "payload": payload,
        "compatibility": compatibility,
        "publication": publication,
    }
    return hashlib.sha256(_canonical_json(content).encode()).hexdigest()


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _decode_market_cursor(cursor: CursorPosition) -> tuple[int, str, int, str]:
    try:
        payload = json.loads(cursor.primary)
        if not isinstance(payload, dict):
            raise TypeError("market cursor payload must be an object")
        tier = payload["verification_tier"]
        verified_at = payload["latest_passed_at"]
        adoption_count = payload["adoption_count"]
        published_at = payload["published_at"]
        if (
            not isinstance(tier, int)
            or isinstance(tier, bool)
            or tier < 0
            or not isinstance(verified_at, str)
            or not isinstance(adoption_count, int)
            or isinstance(adoption_count, bool)
            or adoption_count < 0
            or not isinstance(published_at, str)
        ):
            raise TypeError("market cursor fields are invalid")
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise TemplateMarketError(
            "market cursor is invalid",
            code="TEMPLATE.CURSOR_INVALID",
        ) from exc
    return tier, verified_at, adoption_count, published_at


def _validate_actor(value: str) -> None:
    if not _ID.fullmatch(value):
        raise TemplateMarketError("actor is invalid", code="TEMPLATE.ACTOR_INVALID")


def _validate_id(value: str, *, field: str) -> None:
    if not _ID.fullmatch(value):
        raise TemplateMarketError(f"{field} is invalid", code="TEMPLATE.ID_INVALID")


def _validate_text(value: str, *, field: str, limit: int, required: bool) -> None:
    if not isinstance(value, str) or len(value.strip()) > limit or (required and not value.strip()):
        raise TemplateMarketError(f"{field} is invalid", code="TEMPLATE.FIELD_INVALID")


def _validate_mapping(value: object, *, field: str) -> None:
    if not isinstance(value, dict):
        raise TemplateMarketError(f"{field} must be an object", code="TEMPLATE.FIELD_INVALID")
    try:
        _canonical_json(value)
    except (TypeError, ValueError) as exc:
        raise TemplateMarketError(
            f"{field} must be JSON serializable",
            code="TEMPLATE.FIELD_INVALID",
        ) from exc


def _validate_scope(visibility: TemplateVisibility, scope_key: str | None) -> None:
    if visibility == TemplateVisibility.COURSE:
        if scope_key is None or not _ID.fullmatch(scope_key):
            raise TemplateMarketError(
                "course visibility requires a valid scope_key",
                code="TEMPLATE.SCOPE_INVALID",
            )
    elif scope_key is not None:
        raise TemplateMarketError(
            "scope_key is only valid for course visibility",
            code="TEMPLATE.SCOPE_INVALID",
        )


def _optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    return value.strip() or None


def _row_to_draft(row: sqlite3.Row) -> TemplateDraftRecord:
    return TemplateDraftRecord(
        draft_id=str(row["draft_id"]),
        template_id=str(row["template_id"]),
        owner=str(row["owner"]),
        title=str(row["title"]),
        description=str(row["description"]),
        visibility=TemplateVisibility(str(row["visibility"])),
        scope_key=None if row["scope_key"] is None else str(row["scope_key"]),
        state=TemplateDraftState(str(row["state"])),
        version=int(row["version"]),
        payload=json.loads(str(row["payload_json"])),
        compatibility=json.loads(str(row["compatibility_json"])),
        publication=json.loads(str(row["publication_json"])),
        content_sha256=str(row["content_sha256"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _row_to_review(row: sqlite3.Row) -> TemplateReviewRecord:
    return TemplateReviewRecord(
        review_id=str(row["review_id"]),
        draft_id=str(row["draft_id"]),
        requester=str(row["requester"]),
        reviewer=None if row["reviewer"] is None else str(row["reviewer"]),
        reviewer_role=(None if row["reviewer_role"] is None else str(row["reviewer_role"])),
        reviewer_scope_key=(
            None if row["reviewer_scope_key"] is None else str(row["reviewer_scope_key"])
        ),
        state=TemplateReviewState(str(row["state"])),
        version=int(row["version"]),
        draft_version=int(row["draft_version"]),
        content_sha256=str(row["content_sha256"]),
        note=None if row["note"] is None else str(row["note"]),
        gate_report=json.loads(str(row["gate_report_json"])),
        validated_at=(None if row["validated_at"] is None else str(row["validated_at"])),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        decided_at=None if row["decided_at"] is None else str(row["decided_at"]),
    )


def _row_to_review_queue(row: sqlite3.Row) -> TemplateReviewQueueRecord:
    return TemplateReviewQueueRecord(
        review=_row_to_review(row),
        draft_title=str(row["draft_title"]),
        visibility=TemplateVisibility(str(row["draft_visibility"])),
        scope_key=(None if row["draft_scope_key"] is None else str(row["draft_scope_key"])),
    )


def _row_to_release(row: sqlite3.Row) -> TemplateReleaseRecord:
    return TemplateReleaseRecord(
        release_id=str(row["release_id"]),
        template_id=str(row["template_id"]),
        release_version=str(row["release_version"]),
        source_draft_id=str(row["source_draft_id"]),
        source_draft_version=int(row["source_draft_version"]),
        review_id=str(row["review_id"]),
        publisher=str(row["publisher"]),
        request_key=None if row["request_key"] is None else str(row["request_key"]),
        title=str(row["title"]),
        description=str(row["description"]),
        visibility=TemplateVisibility(str(row["visibility"])),
        scope_key=None if row["scope_key"] is None else str(row["scope_key"]),
        payload=json.loads(str(row["payload_json"])),
        compatibility=json.loads(str(row["compatibility_json"])),
        publication=json.loads(str(row["publication_json"])),
        gate_report=json.loads(str(row["gate_report_json"])),
        content_sha256=str(row["content_sha256"]),
        published_at=str(row["published_at"]),
        withdrawn_at=(None if row["withdrawn_at"] is None else str(row["withdrawn_at"])),
        withdrawal_actor=(
            None if row["withdrawal_actor"] is None else str(row["withdrawal_actor"])
        ),
        withdrawal_reason=(
            None if row["withdrawal_reason"] is None else str(row["withdrawal_reason"])
        ),
    )


def _row_to_adoption(row: sqlite3.Row) -> TemplateAdoptionRecord:
    return TemplateAdoptionRecord(
        adoption_id=str(row["adoption_id"]),
        release_id=str(row["release_id"]),
        adopter=str(row["adopter"]),
        request_key=str(row["request_key"]),
        target_template_id=str(row["target_template_id"]),
        target_draft_id=str(row["target_draft_id"]),
        target_contract_id=(
            None if row["target_contract_id"] is None else str(row["target_contract_id"])
        ),
        created_at=str(row["created_at"]),
    )


def _row_to_verification(row: sqlite3.Row) -> TemplateVerificationRecord:
    return TemplateVerificationRecord(
        verification_id=str(row["verification_id"]),
        release_id=str(row["release_id"]),
        run_id=None if row["run_id"] is None else str(row["run_id"]),
        environment=str(row["environment"]),
        status=str(row["status"]),
        evidence_ref=(None if row["evidence_ref"] is None else str(row["evidence_ref"])),
        evidence_sha256=(None if row["evidence_sha256"] is None else str(row["evidence_sha256"])),
        verified_by=(None if row["verified_by"] is None else str(row["verified_by"])),
        request_key=(None if row["request_key"] is None else str(row["request_key"])),
        detail=json.loads(str(row["detail_json"])),
        verified_at=str(row["verified_at"]),
    )


def _latest_verification(
    conn: sqlite3.Connection, release_id: str
) -> TemplateVerificationRecord | None:
    row = conn.execute(
        "SELECT * FROM template_verifications WHERE release_id = ? "
        "ORDER BY verified_at DESC, verification_id DESC LIMIT 1",
        (release_id,),
    ).fetchone()
    return None if row is None else _row_to_verification(row)


def _row_to_market_item(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
) -> TemplateMarketItemRecord:
    return TemplateMarketItemRecord(
        release=_row_to_release(row),
        metrics=TemplateMetricsRecord(
            adoption_count=int(row["adoption_count"]),
            verification_passed=int(row["verification_passed"]),
            verification_failed=int(row["verification_failed"]),
            verification_expired=int(row["verification_expired"]),
            latest_verification=_latest_verification(conn, str(row["release_id"])),
        ),
    )
