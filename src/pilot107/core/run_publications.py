"""Owner-confirmed sharing of successful Runs.

This is intentionally a lighter-weight market path than curated template
releases.  It records what a user chose to share after a successful run; it
does not claim that private code, data, paths, or credentials are portable.
When a source Run has a Contract, adopters receive a new private Contract.
"""

from __future__ import annotations

import json
import re
import sqlite3
from copy import deepcopy
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import uuid4

from pilot107.core.contracts import ContractError, ContractService
from pilot107.core.pagination import CursorPosition
from pilot107.core.run_publication_migrations import RUN_PUBLICATION_MIGRATIONS
from pilot107.core.run_store import RunRecord, RunStore, utc_now_iso
from pilot107.core.schema_migrations import apply_schema_migrations
from pilot107.core.states import RunState

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_TAG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")


class RunPublicationVisibility(StrEnum):
    PRIVATE = "private"
    COURSE = "course"
    CAMPUS = "campus"
    PUBLIC = "public"


class RunPublicationError(ValueError):
    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class RunPublicationRecord:
    publication_id: str
    source_run_id: str
    source_contract_id: str | None
    owner: str
    title: str
    description: str
    visibility: RunPublicationVisibility
    scope_key: str | None
    tags: tuple[str, ...]
    reproduction_note: str
    request_key: str
    published_at: str
    updated_at: str
    withdrawn_at: str | None
    withdrawal_actor: str | None
    withdrawal_reason: str | None

    @property
    def active(self) -> bool:
        return self.withdrawn_at is None

    @property
    def adoptable(self) -> bool:
        return self.source_contract_id is not None


@dataclass(frozen=True)
class RunPublicationAdoptionRecord:
    adoption_id: str
    publication_id: str
    adopter: str
    request_key: str
    target_contract_id: str | None
    created_at: str


@dataclass(frozen=True)
class RunPublicationEligibilityRecord:
    status: str
    reason: str | None
    publication_id: str | None

    def as_payload(self) -> dict[str, str | None]:
        return {
            "status": self.status,
            "reason": self.reason,
            "publication_id": self.publication_id,
        }


class RunPublicationStore:
    def __init__(
        self,
        db_path: Path,
        *,
        run_store: RunStore,
        contract_service: ContractService | None = None,
    ) -> None:
        self.db_path = db_path
        self.run_store = run_store
        self.contract_service = contract_service
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            apply_schema_migrations(conn, RUN_PUBLICATION_MIGRATIONS)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 10000")
        return conn

    def publish(
        self,
        *,
        source_run_id: str,
        owner: str,
        title: str,
        description: str,
        visibility: RunPublicationVisibility,
        scope_key: str | None,
        tags: list[str] | tuple[str, ...] = (),
        reproduction_note: str = "",
        request_key: str,
        confirmed: bool,
    ) -> RunPublicationRecord:
        _validate_id(source_run_id, field="source_run_id")
        _validate_actor(owner)
        _validate_id(request_key, field="request_key")
        _validate_text(title, field="title", limit=160, required=True)
        _validate_text(description, field="description", limit=4000, required=False)
        _validate_text(reproduction_note, field="reproduction_note", limit=4000, required=False)
        _validate_scope(visibility, scope_key)
        normalized_tags = _normalize_tags(tags)
        if confirmed is not True:
            raise RunPublicationError(
                "owner confirmation is required before sharing a Run",
                code="MARKET.CONFIRMATION_REQUIRED",
            )
        source_run = self._eligible_source_run(source_run_id=source_run_id, owner=owner)
        now = utc_now_iso()
        with self.connect() as conn:
            existing_by_request = conn.execute(
                "SELECT * FROM run_publications WHERE owner = ? AND request_key = ?",
                (owner, request_key),
            ).fetchone()
            if existing_by_request is not None:
                existing = _row_to_publication(existing_by_request)
                if existing.source_run_id != source_run_id:
                    raise RunPublicationError(
                        "request_key was already used for another source Run",
                        code="MARKET.IDEMPOTENCY_CONFLICT",
                    )
                return existing
            existing_by_run = conn.execute(
                "SELECT * FROM run_publications WHERE source_run_id = ?",
                (source_run_id,),
            ).fetchone()
            if existing_by_run is not None:
                raise RunPublicationError(
                    "this successful Run has already been published",
                    code="MARKET.RUN_ALREADY_PUBLISHED",
                )
            publication_id = f"runpub_{uuid4().hex}"
            conn.execute(
                """
                INSERT INTO run_publications (
                    publication_id, source_run_id, source_contract_id, owner,
                    title, description, visibility, scope_key, tags_json,
                    reproduction_note, request_key, published_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    publication_id,
                    source_run_id,
                    source_run.contract_id,
                    owner,
                    title.strip(),
                    description.strip(),
                    visibility.value,
                    scope_key,
                    json.dumps(normalized_tags),
                    reproduction_note.strip(),
                    request_key,
                    now,
                    now,
                ),
            )
            record = _row_to_publication(
                conn.execute(
                    "SELECT * FROM run_publications WHERE publication_id = ?",
                    (publication_id,),
                ).fetchone()
            )
        self.run_store.append_event(
            run_id=source_run_id,
            event_type="market.run_published",
            payload={
                "publication_id": record.publication_id,
                "visibility": record.visibility.value,
                "scope_key": record.scope_key,
            },
        )
        return record

    def get(self, publication_id: str) -> RunPublicationRecord:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM run_publications WHERE publication_id = ?",
                (publication_id,),
            ).fetchone()
        if row is None:
            raise KeyError(publication_id)
        return _row_to_publication(row)

    def get_for_source_run(
        self,
        *,
        source_run_id: str,
        owner: str,
    ) -> RunPublicationRecord | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM run_publications
                WHERE source_run_id = ? AND owner = ?
                """,
                (source_run_id, owner),
            ).fetchone()
        return None if row is None else _row_to_publication(row)

    def eligibility(
        self,
        *,
        source_run_id: str,
        owner: str,
    ) -> RunPublicationEligibilityRecord:
        """Compute the authoritative publish CTA state for an owned Run."""

        _validate_id(source_run_id, field="source_run_id")
        _validate_actor(owner)
        run = self.run_store.get_run(source_run_id)
        if run.owner != owner:
            return RunPublicationEligibilityRecord(
                status="ineligible",
                reason="not_owner",
                publication_id=None,
            )
        existing = self.get_for_source_run(
            source_run_id=source_run_id,
            owner=owner,
        )
        if existing is not None:
            return RunPublicationEligibilityRecord(
                status=("published" if existing.active else "ineligible"),
                reason=(None if existing.active else "already_published"),
                publication_id=existing.publication_id,
            )
        if run.state != RunState.SUCCEEDED:
            return RunPublicationEligibilityRecord(
                status="ineligible",
                reason="run_not_succeeded",
                publication_id=None,
            )
        if not (run.exit_code or "").startswith("0:"):
            return RunPublicationEligibilityRecord(
                status="ineligible",
                reason="exit_nonzero",
                publication_id=None,
            )
        return RunPublicationEligibilityRecord(
            status="eligible",
            reason=None,
            publication_id=None,
        )

    def list_market_page(
        self,
        *,
        actor: str,
        course_scopes: frozenset[str] | set[str] | tuple[str, ...] = (),
        query: str | None = None,
        visibility: RunPublicationVisibility | None = None,
        tag: str | None = None,
        cursor: CursorPosition | None = None,
        limit: int = 50,
    ) -> tuple[list[RunPublicationRecord], CursorPosition | None]:
        _require_page_limit(limit)
        if query is not None:
            _validate_text(query, field="q", limit=200, required=False)
        if tag is not None and not _TAG.fullmatch(tag):
            raise RunPublicationError("tag is invalid", code="MARKET.INVALID_TAG")
        normalized_scopes = tuple(sorted(set(course_scopes)))
        visibility_conditions, visibility_values = _visibility_conditions(
            actor=actor,
            course_scopes=normalized_scopes,
        )
        conditions = ["withdrawn_at IS NULL", f"({' OR '.join(visibility_conditions)})"]
        values: list[Any] = [*visibility_values]
        if visibility is not None:
            conditions.append("visibility = ?")
            values.append(visibility.value)
        if query is not None and query.strip():
            escaped = _escape_like(query.strip())
            conditions.append("(title LIKE ? ESCAPE '\\' OR description LIKE ? ESCAPE '\\')")
            values.extend([f"%{escaped}%", f"%{escaped}%"])
        if tag is not None:
            conditions.append("tags_json LIKE ?")
            values.append(f'%"{tag}"%')
        if cursor is not None:
            conditions.append("(published_at < ? OR (published_at = ? AND publication_id < ?))")
            values.extend([cursor.primary, cursor.primary, cursor.secondary])
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM run_publications WHERE "
                + " AND ".join(conditions)
                + " ORDER BY published_at DESC, publication_id DESC LIMIT ?",
                (*values, limit + 1),
            ).fetchall()
        selected = rows[:limit]
        records = [_row_to_publication(row) for row in selected]
        next_position = (
            CursorPosition(
                primary=records[-1].published_at,
                secondary=records[-1].publication_id,
            )
            if len(rows) > limit and records
            else None
        )
        return records, next_position

    def get_visible(
        self,
        *,
        publication_id: str,
        actor: str,
        course_scopes: frozenset[str] | set[str] | tuple[str, ...] = (),
    ) -> RunPublicationRecord:
        record = self.get(publication_id)
        if not record.active or not _can_view(
            record,
            actor=actor,
            course_scopes=frozenset(course_scopes),
        ):
            raise KeyError(publication_id)
        return record

    def withdraw(
        self,
        *,
        publication_id: str,
        actor: str,
        reason: str,
    ) -> RunPublicationRecord:
        _validate_actor(actor)
        _validate_text(reason, field="reason", limit=1000, required=True)
        now = utc_now_iso()
        with self.connect() as conn:
            record = _fetch_publication(conn, publication_id)
            if record.owner != actor:
                raise RunPublicationError(
                    "only the publisher may withdraw this item",
                    code="MARKET.FORBIDDEN",
                )
            if not record.active:
                return record
            conn.execute(
                """
                UPDATE run_publications
                SET withdrawn_at = ?, withdrawal_actor = ?, withdrawal_reason = ?, updated_at = ?
                WHERE publication_id = ?
                """,
                (now, actor, reason.strip(), now, publication_id),
            )
            updated = _fetch_publication(conn, publication_id)
        self.run_store.append_event(
            run_id=updated.source_run_id,
            event_type="market.run_withdrawn",
            payload={"publication_id": updated.publication_id, "reason": updated.withdrawal_reason},
        )
        return updated

    def adopt(
        self,
        *,
        publication_id: str,
        adopter: str,
        request_key: str,
        course_scopes: frozenset[str] | set[str] | tuple[str, ...] = (),
    ) -> RunPublicationAdoptionRecord:
        _validate_actor(adopter)
        _validate_id(request_key, field="request_key")
        existing = self._adoption_by_request(adopter=adopter, request_key=request_key)
        if existing is not None:
            if existing.publication_id != publication_id:
                raise RunPublicationError(
                    "request_key was already used for another market item",
                    code="MARKET.IDEMPOTENCY_CONFLICT",
                )
            return existing
        publication = self.get_visible(
            publication_id=publication_id,
            actor=adopter,
            course_scopes=course_scopes,
        )
        if publication.source_contract_id is None:
            raise RunPublicationError(
                "this Run is a reference item and has no adoptable Contract",
                code="MARKET.SOURCE_CONTRACT_UNAVAILABLE",
            )
        if self.contract_service is None:
            raise RunPublicationError(
                "contract adoption is not configured",
                code="MARKET.ADOPTION_UNAVAILABLE",
            )
        try:
            source = self.contract_service.get(publication.source_contract_id)
        except KeyError as exc:
            raise RunPublicationError(
                "the source Contract is no longer available",
                code="MARKET.SOURCE_CONTRACT_UNAVAILABLE",
            ) from exc
        target_payload = _rebase_adopter_workdir(source.payload, adopter=adopter)
        try:
            validation = self.contract_service.validate(target_payload)
        except ContractError as exc:
            raise RunPublicationError(
                f"the source Contract cannot be adopted: {exc}",
                code="MARKET.ADOPTION_CONTRACT_INVALID",
            ) from exc
        if validation.status == "BLOCK":
            raise RunPublicationError(
                "the source Contract is blocked by the adopter's current policy",
                code="MARKET.ADOPTION_CONTRACT_INVALID",
            )
        token = _adoption_token(adopter=adopter, request_key=request_key)
        record = RunPublicationAdoptionRecord(
            adoption_id=f"runadopt_{token}",
            publication_id=publication_id,
            adopter=adopter,
            request_key=request_key,
            target_contract_id=f"contract_runpub_{token}",
            created_at=utc_now_iso(),
        )
        try:
            with self.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                existing = conn.execute(
                    """
                    SELECT * FROM run_publication_adoptions
                    WHERE adopter = ? AND request_key = ?
                    """,
                    (adopter, request_key),
                ).fetchone()
                if existing is not None:
                    persisted = _row_to_adoption(existing)
                    if persisted.publication_id != publication_id:
                        raise RunPublicationError(
                            "request_key was already used for another market item",
                            code="MARKET.IDEMPOTENCY_CONFLICT",
                        )
                    return persisted
                stored_publication = _fetch_publication(conn, publication_id)
                if not stored_publication.active or not _can_view(
                    stored_publication,
                    actor=adopter,
                    course_scopes=frozenset(course_scopes),
                ):
                    raise KeyError(publication_id)
                assert record.target_contract_id is not None
                self.contract_service.store.create_contract(
                    owner=adopter,
                    recipe_version_id=source.recipe_version_id,
                    payload=target_payload,
                    field_sources=[
                        {
                            "field": "*",
                            "source": "run_publication",
                            "source_publication_id": publication.publication_id,
                            "source_run_id": publication.source_run_id,
                            "source_contract_id": source.contract_id,
                            "needs_user_confirmation": True,
                            "adopter_workdir_rebased": target_payload != source.payload,
                        }
                    ],
                    contract_id=record.target_contract_id,
                    parent_contract_id=source.contract_id,
                    derivation_reason="run_publication_adoption",
                    idempotent=True,
                    connection=conn,
                )
                conn.execute(
                    """
                    INSERT INTO run_publication_adoptions (
                        adoption_id, publication_id, adopter, request_key,
                        target_contract_id, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.adoption_id,
                        record.publication_id,
                        record.adopter,
                        record.request_key,
                        record.target_contract_id,
                        record.created_at,
                    ),
                )
        except sqlite3.IntegrityError:
            # A concurrent retry might have completed after the first lookup.
            existing = self._adoption_by_request(adopter=adopter, request_key=request_key)
            if existing is not None and existing.publication_id == publication_id:
                return existing
            raise
        self.run_store.append_event(
            run_id=publication.source_run_id,
            event_type="market.run_adopted",
            payload={
                "publication_id": publication.publication_id,
                "adoption_id": record.adoption_id,
            },
        )
        return record

    def _eligible_source_run(self, *, source_run_id: str, owner: str) -> RunRecord:
        try:
            run = self.run_store.get_run(source_run_id)
        except KeyError as exc:
            raise RunPublicationError(
                "source Run was not found",
                code="MARKET.RUN_NOT_FOUND",
            ) from exc
        if run.owner != owner:
            raise RunPublicationError(
                "only the Run owner may publish it",
                code="MARKET.FORBIDDEN",
            )
        if run.state != RunState.SUCCEEDED or not (run.exit_code or "").startswith("0:"):
            raise RunPublicationError(
                "only a succeeded Run with zero exit status may be published",
                code="MARKET.RUN_NOT_SUCCESSFUL",
            )
        return run

    def _adoption_by_request(
        self,
        *,
        adopter: str,
        request_key: str,
    ) -> RunPublicationAdoptionRecord | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM run_publication_adoptions
                WHERE adopter = ? AND request_key = ?
                """,
                (adopter, request_key),
            ).fetchone()
        return None if row is None else _row_to_adoption(row)


def run_publication_payload(record: RunPublicationRecord) -> dict[str, Any]:
    """Public-facing payload; intentionally excludes Script, workdir and Contract payload."""

    return {
        "kind": "successful_run",
        "publication_id": record.publication_id,
        "source_run_id": record.source_run_id,
        "owner": record.owner,
        "title": record.title,
        "description": record.description,
        "visibility": record.visibility.value,
        "scope_key": record.scope_key,
        "tags": list(record.tags),
        "reproduction_note": record.reproduction_note,
        "adoptable": record.adoptable,
        "published_at": record.published_at,
        "updated_at": record.updated_at,
    }


def run_publication_adoption_payload(record: RunPublicationAdoptionRecord) -> dict[str, Any]:
    return {
        "adoption_id": record.adoption_id,
        "publication_id": record.publication_id,
        "target_contract_id": record.target_contract_id,
        "created_at": record.created_at,
    }


def _fetch_publication(conn: sqlite3.Connection, publication_id: str) -> RunPublicationRecord:
    row = conn.execute(
        "SELECT * FROM run_publications WHERE publication_id = ?",
        (publication_id,),
    ).fetchone()
    if row is None:
        raise KeyError(publication_id)
    return _row_to_publication(row)


def _row_to_publication(row: Any) -> RunPublicationRecord:
    return RunPublicationRecord(
        publication_id=str(row["publication_id"]),
        source_run_id=str(row["source_run_id"]),
        source_contract_id=(
            None if row["source_contract_id"] is None else str(row["source_contract_id"])
        ),
        owner=str(row["owner"]),
        title=str(row["title"]),
        description=str(row["description"]),
        visibility=RunPublicationVisibility(str(row["visibility"])),
        scope_key=None if row["scope_key"] is None else str(row["scope_key"]),
        tags=tuple(json.loads(str(row["tags_json"]))),
        reproduction_note=str(row["reproduction_note"]),
        request_key=str(row["request_key"]),
        published_at=str(row["published_at"]),
        updated_at=str(row["updated_at"]),
        withdrawn_at=None if row["withdrawn_at"] is None else str(row["withdrawn_at"]),
        withdrawal_actor=(
            None if row["withdrawal_actor"] is None else str(row["withdrawal_actor"])
        ),
        withdrawal_reason=(
            None if row["withdrawal_reason"] is None else str(row["withdrawal_reason"])
        ),
    )


def _row_to_adoption(row: Any) -> RunPublicationAdoptionRecord:
    return RunPublicationAdoptionRecord(
        adoption_id=str(row["adoption_id"]),
        publication_id=str(row["publication_id"]),
        adopter=str(row["adopter"]),
        request_key=str(row["request_key"]),
        target_contract_id=(
            None if row["target_contract_id"] is None else str(row["target_contract_id"])
        ),
        created_at=str(row["created_at"]),
    )


def _visibility_conditions(
    *,
    actor: str,
    course_scopes: tuple[str, ...],
) -> tuple[list[str], list[Any]]:
    conditions = ["visibility = 'public'"]
    values: list[Any] = []
    if actor:
        conditions.extend(
            [
                "visibility = 'campus'",
                "owner = ?",
                "visibility = 'private' AND owner = ?",
            ]
        )
        values.extend([actor, actor])
    if course_scopes:
        placeholders = ",".join("?" for _ in course_scopes)
        conditions.append(f"visibility = 'course' AND scope_key IN ({placeholders})")
        values.extend(course_scopes)
    return conditions, values


def _can_view(
    record: RunPublicationRecord,
    *,
    actor: str,
    course_scopes: frozenset[str],
) -> bool:
    if record.owner == actor:
        return True
    if record.visibility == RunPublicationVisibility.PUBLIC:
        return True
    if not actor:
        return False
    if record.visibility == RunPublicationVisibility.CAMPUS:
        return True
    return (
        record.visibility == RunPublicationVisibility.COURSE
        and record.scope_key is not None
        and record.scope_key in course_scopes
    )


def _validate_actor(actor: str) -> None:
    if not _ID.fullmatch(actor):
        raise RunPublicationError("actor is invalid", code="MARKET.INVALID_ACTOR")


def _validate_id(value: str, *, field: str) -> None:
    if not _ID.fullmatch(value):
        raise RunPublicationError(f"{field} is invalid", code="MARKET.INVALID_REQUEST")


def _validate_text(value: str, *, field: str, limit: int, required: bool) -> None:
    if not isinstance(value, str) or len(value.strip()) > limit or (required and not value.strip()):
        raise RunPublicationError(f"{field} is invalid", code="MARKET.INVALID_REQUEST")


def _validate_scope(visibility: RunPublicationVisibility, scope_key: str | None) -> None:
    if visibility == RunPublicationVisibility.COURSE:
        if scope_key is None or not _ID.fullmatch(scope_key):
            raise RunPublicationError(
                "course visibility requires scope_key",
                code="MARKET.INVALID_SCOPE",
            )
        return
    if scope_key is not None:
        raise RunPublicationError(
            "scope_key is supported only for course visibility",
            code="MARKET.INVALID_SCOPE",
        )


def _normalize_tags(tags: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(tags, (list, tuple)) or len(tags) > 12:
        raise RunPublicationError("tags are invalid", code="MARKET.INVALID_TAG")
    result: list[str] = []
    for tag in tags:
        if not isinstance(tag, str) or not _TAG.fullmatch(tag):
            raise RunPublicationError("tags are invalid", code="MARKET.INVALID_TAG")
        if tag not in result:
            result.append(tag)
    return tuple(result)


def _require_page_limit(limit: int) -> None:
    if not isinstance(limit, int) or not 1 <= limit <= 100:
        raise RunPublicationError("limit must be between 1 and 100", code="MARKET.INVALID_PAGE")


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _adoption_token(*, adopter: str, request_key: str) -> str:
    # A deterministic target Contract makes a retried request idempotent
    # without exposing the original request key in the object ID.
    import hashlib

    return hashlib.sha256(f"{adopter}\0{request_key}".encode()).hexdigest()[:32]


def _rebase_adopter_workdir(payload: dict[str, Any], *, adopter: str) -> dict[str, Any]:
    """Avoid copying a known personal-home path into another user's Contract.

    This is a narrow safety transformation, not a portability validator.  A
    project may still require code, data, modules, or other changes that the
    adopter must inspect in Studio before submitting.
    """

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
