"""Unified public read model for successful Runs and curated templates."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pilot107.core.pagination import CursorPosition
from pilot107.core.run_publications import (
    RunPublicationRecord,
    RunPublicationStore,
    RunPublicationVisibility,
)
from pilot107.core.template_market import (
    TemplateMarketItemRecord,
    TemplateMarketStore,
    TemplateVisibility,
    authorize_template_release,
    template_verification_payload,
)


class MarketItemKind(StrEnum):
    RUN_PUBLICATION = "run_publication"
    CURATED_TEMPLATE = "curated_template"


class MarketVisibility(StrEnum):
    PRIVATE = "private"
    COURSE = "course"
    CAMPUS = "campus"
    PUBLIC = "public"


@dataclass(frozen=True)
class MarketItemRecord:
    kind: MarketItemKind
    item_id: str
    published_at: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class MarketItemPage:
    items: tuple[MarketItemRecord, ...]
    next_position: CursorPosition | None


class MarketReadService:
    """Merge domain stores without weakening either publication policy."""

    def __init__(
        self,
        *,
        run_publications: RunPublicationStore | None,
        templates: TemplateMarketStore | None,
    ) -> None:
        self.run_publications = run_publications
        self.templates = templates

    def list_page(
        self,
        *,
        actor: str,
        course_scopes: frozenset[str],
        query: str | None,
        kind: MarketItemKind | None,
        visibility: MarketVisibility | None,
        tag: str | None,
        cursor: CursorPosition | None,
        limit: int,
    ) -> MarketItemPage:
        if limit <= 0 or limit > 100:
            raise ValueError("limit must be between 1 and 100")
        merged: list[MarketItemRecord] = []
        source_has_more = False

        if kind in {None, MarketItemKind.RUN_PUBLICATION} and self.run_publications is not None:
            run_records, run_next = self.run_publications.list_market_page(
                actor=actor,
                course_scopes=course_scopes,
                query=query,
                visibility=(
                    None
                    if visibility is None
                    else RunPublicationVisibility(visibility.value)
                ),
                tag=tag,
                cursor=cursor,
                limit=limit,
            )
            merged.extend(_run_item(record) for record in run_records)
            source_has_more = source_has_more or run_next is not None

        # Tags belong only to ordinary Run publications.  Applying a tag
        # filter to curated releases would invent a semantic they do not have.
        include_templates = tag is None and kind in {
            None,
            MarketItemKind.CURATED_TEMPLATE,
        }
        if include_templates and self.templates is not None:
            template_records, template_next = self.templates.list_market_chronological_page(
                actor=actor,
                course_scopes=course_scopes,
                query=query,
                visibility=(
                    None if visibility is None else TemplateVisibility(visibility.value)
                ),
                cursor=cursor,
                limit=limit,
            )
            merged.extend(_template_item(record) for record in template_records)
            source_has_more = source_has_more or template_next is not None

        merged.sort(key=lambda item: (item.published_at, item.item_id), reverse=True)
        selected = tuple(merged[:limit])
        has_more = source_has_more or len(merged) > limit
        next_position = (
            CursorPosition(
                primary=selected[-1].published_at,
                secondary=selected[-1].item_id,
            )
            if has_more and selected
            else None
        )
        return MarketItemPage(items=selected, next_position=next_position)

    def get(
        self,
        *,
        item_id: str,
        actor: str,
        course_scopes: frozenset[str],
    ) -> MarketItemRecord:
        if item_id.startswith("runpub_") and self.run_publications is not None:
            return _run_item(
                self.run_publications.get_visible(
                    publication_id=item_id,
                    actor=actor,
                    course_scopes=course_scopes,
                )
            )
        if item_id.startswith("release_") and self.templates is not None:
            record = self.templates.get_market_item(item_id)
            authorize_template_release(
                record.release,
                actor=actor,
                course_scopes=course_scopes,
            )
            if record.release.withdrawn_at is not None and record.release.publisher != actor:
                raise KeyError(item_id)
            return _template_item(record)
        raise KeyError(item_id)


def _run_item(record: RunPublicationRecord) -> MarketItemRecord:
    payload: dict[str, Any] = {
        "kind": MarketItemKind.RUN_PUBLICATION.value,
        "item_id": record.publication_id,
        "title": record.title,
        "description": record.description,
        "visibility": record.visibility.value,
        "scope_key": record.scope_key,
        "publisher": record.owner,
        "published_at": record.published_at,
        "updated_at": record.updated_at,
        "tags": list(record.tags),
        "source": {
            "type": "successful_run",
            "run_id": record.source_run_id,
        },
        "adoption": {
            "available": record.adoptable,
            "reason": None if record.adoptable else "source_contract_unavailable",
        },
        "reproduction_note": record.reproduction_note,
        "withdrawn_at": record.withdrawn_at,
    }
    return MarketItemRecord(
        kind=MarketItemKind.RUN_PUBLICATION,
        item_id=record.publication_id,
        published_at=record.published_at,
        payload=payload,
    )


def _template_item(record: TemplateMarketItemRecord) -> MarketItemRecord:
    release = record.release
    decided = record.metrics.verification_passed + record.metrics.verification_failed
    payload: dict[str, Any] = {
        "kind": MarketItemKind.CURATED_TEMPLATE.value,
        "item_id": release.release_id,
        "title": release.title,
        "description": release.description,
        "visibility": release.visibility.value,
        "scope_key": release.scope_key,
        "publisher": release.publisher,
        "published_at": release.published_at,
        "updated_at": release.published_at,
        "tags": [],
        "template": {
            "template_id": release.template_id,
            "release_version": release.release_version,
            "content_sha256": release.content_sha256,
        },
        "contract_payload": release.payload,
        "compatibility": release.compatibility,
        "publication": release.publication,
        "adoption": {
            "available": release.withdrawn_at is None,
            "reason": (
                None if release.withdrawn_at is None else "template_release_withdrawn"
            ),
        },
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
        "withdrawn_at": release.withdrawn_at,
    }
    return MarketItemRecord(
        kind=MarketItemKind.CURATED_TEMPLATE,
        item_id=release.release_id,
        published_at=release.published_at,
        payload=payload,
    )
