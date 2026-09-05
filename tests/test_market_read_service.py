import unittest

from pilot107.core.market import MarketItemKind, MarketReadService
from pilot107.core.pagination import CursorPosition
from pilot107.core.run_publications import (
    RunPublicationRecord,
    RunPublicationShareManifest,
    RunPublicationVisibility,
)
from pilot107.core.template_market import (
    TemplateMarketItemRecord,
    TemplateMetricsRecord,
    TemplateReleaseRecord,
    TemplateVisibility,
)


class MarketReadServiceTests(unittest.TestCase):
    def test_cross_kind_cursor_is_chronological_stable_and_private_safe(self) -> None:
        run_records = (
            _run("runpub_new", "2026-07-26T03:00:00+00:00"),
            _run("runpub_old", "2026-07-26T01:00:00+00:00"),
        )
        template_records = (_template("release_middle", "2026-07-26T02:00:00+00:00"),)
        service = MarketReadService(
            run_publications=_RunStoreStub(run_records),
            templates=_TemplateStoreStub(template_records),
        )

        first = service.list_page(
            actor="bob",
            course_scopes=frozenset(),
            query=None,
            kind=None,
            visibility=None,
            tag=None,
            cursor=None,
            limit=2,
        )
        second = service.list_page(
            actor="bob",
            course_scopes=frozenset(),
            query=None,
            kind=None,
            visibility=None,
            tag=None,
            cursor=first.next_position,
            limit=2,
        )

        self.assertEqual(
            [item.item_id for item in first.items],
            ["runpub_new", "release_middle"],
        )
        self.assertEqual([item.item_id for item in second.items], ["runpub_old"])
        self.assertIsNone(second.next_position)
        self.assertNotIn("workdir", first.items[0].payload)
        self.assertNotIn("contract_payload", first.items[0].payload)

    def test_tag_filter_has_no_invented_curated_template_semantics(self) -> None:
        service = MarketReadService(
            run_publications=_RunStoreStub((_run("runpub_tagged", "2026-07-26T03:00:00+00:00"),)),
            templates=_TemplateStoreStub((_template("release_a", "2026-07-26T04:00:00+00:00"),)),
        )

        page = service.list_page(
            actor="bob",
            course_scopes=frozenset(),
            query=None,
            kind=None,
            visibility=None,
            tag="smoke",
            cursor=None,
            limit=10,
        )

        self.assertEqual([item.kind for item in page.items], [MarketItemKind.RUN_PUBLICATION])


class _RunStoreStub:
    def __init__(self, records: tuple[RunPublicationRecord, ...]) -> None:
        self.records = records

    def list_market_page(self, *, cursor=None, limit=50, tag=None, **_kwargs):
        records = _after(
            self.records,
            cursor,
            lambda item: item.published_at,
            lambda item: item.publication_id,
        )
        if tag is not None:
            records = tuple(item for item in records if tag in item.tags)
        return list(records[:limit]), (
            CursorPosition(records[limit - 1].published_at, records[limit - 1].publication_id)
            if len(records) > limit
            else None
        )


class _TemplateStoreStub:
    def __init__(self, records: tuple[TemplateMarketItemRecord, ...]) -> None:
        self.records = records

    def list_market_chronological_page(self, *, cursor=None, limit=50, **_kwargs):
        records = _after(
            self.records,
            cursor,
            lambda item: item.release.published_at,
            lambda item: item.release.release_id,
        )
        return records[:limit], (
            CursorPosition(
                records[limit - 1].release.published_at,
                records[limit - 1].release.release_id,
            )
            if len(records) > limit
            else None
        )


def _after(records, cursor, primary, secondary):
    if cursor is None:
        return records
    return tuple(
        item
        for item in records
        if primary(item) < cursor.primary
        or (primary(item) == cursor.primary and secondary(item) < cursor.secondary)
    )


def _run(publication_id: str, published_at: str) -> RunPublicationRecord:
    share_manifest = RunPublicationShareManifest(
        title=publication_id,
        visibility=RunPublicationVisibility.CAMPUS,
    )
    return RunPublicationRecord(
        publication_id=publication_id,
        source_run_id=f"run_{publication_id}",
        source_contract_id=f"contract_{publication_id}",
        owner="alice",
        title=publication_id,
        description="",
        visibility=RunPublicationVisibility.CAMPUS,
        scope_key=None,
        tags=("smoke",),
        reproduction_note="",
        request_key=f"request_{publication_id}",
        published_at=published_at,
        updated_at=published_at,
        withdrawn_at=None,
        withdrawal_actor=None,
        withdrawal_reason=None,
        share_manifest=share_manifest.as_payload(),
        share_manifest_digest=share_manifest.manifest_digest,
        shared_payload={"description": ""},
    )


def _template(release_id: str, published_at: str) -> TemplateMarketItemRecord:
    release = TemplateReleaseRecord(
        release_id=release_id,
        template_id=f"template_{release_id}",
        release_version="1.0.0",
        source_draft_id=f"draft_{release_id}",
        source_draft_version=1,
        review_id=f"review_{release_id}",
        publisher="alice",
        request_key=f"request_{release_id}",
        title=release_id,
        description="",
        visibility=TemplateVisibility.CAMPUS,
        scope_key=None,
        payload={"recipe_version_id": "recipe_python_cpu@1.0.0"},
        compatibility={},
        publication={},
        gate_report={},
        content_sha256="a" * 64,
        published_at=published_at,
    )
    return TemplateMarketItemRecord(
        release=release,
        metrics=TemplateMetricsRecord(
            adoption_count=0,
            verification_passed=0,
            verification_failed=0,
            verification_expired=0,
            latest_verification=None,
        ),
    )


if __name__ == "__main__":
    unittest.main()
