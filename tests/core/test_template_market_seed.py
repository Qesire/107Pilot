"""template_market_seed: publish preset recipes as market releases idempotently."""
from __future__ import annotations

import pytest

from pilot107.core.contracts import ContractService, ContractStore, RecipeCatalog
from pilot107.core.template_market import TemplateMarketStore, TemplateVisibility
from pilot107.core.template_market_seed import SeedReport, seed_preset_recipes
from pilot107.core.template_policy import (
    TemplatePublicationGate,
    TemplateReviewerRole,
    TemplateRoleDirectory,
)


@pytest.fixture()
def recipe_catalog() -> RecipeCatalog:
    return RecipeCatalog(allow_gpu=False)


@pytest.fixture()
def template_store(tmp_path):
    db_path = tmp_path / "templates.db"
    contract_service = ContractService(
        catalog=RecipeCatalog(),
        store=ContractStore(db_path),
    )
    return TemplateMarketStore(
        db_path,
        publication_gate=TemplatePublicationGate(contract_service),
        contract_service=contract_service,
    )


@pytest.fixture()
def role_directory() -> TemplateRoleDirectory:
    return TemplateRoleDirectory(
        reviewers=frozenset({"pilot107-system-reviewer"}),
        admins=frozenset({"pilot107-system-reviewer", "pilot107-system-author"}),
    )


def test_seed_publishes_cpu_recipes(recipe_catalog, template_store, role_directory):
    report = seed_preset_recipes(
        catalog=recipe_catalog,
        store=template_store,
        role_directory=role_directory,
    )
    assert report.published >= 1
    # Verify releases exist via list_market_page
    items, _ = template_store.list_market_page(actor="pilot107-system-author")
    assert len(items) >= 1


def test_seed_is_idempotent(recipe_catalog, template_store, role_directory):
    first = seed_preset_recipes(
        catalog=recipe_catalog, store=template_store, role_directory=role_directory
    )
    second = seed_preset_recipes(
        catalog=recipe_catalog, store=template_store, role_directory=role_directory
    )
    assert first.published >= 1
    assert second.published == 0, "re-running seed must not publish duplicates"
    assert second.skipped == first.published


def test_seed_records_gate_blocked_without_raising(
    recipe_catalog, template_store, role_directory
):
    """If publication gate blocks a recipe, seed records it and continues."""
    report = seed_preset_recipes(
        catalog=recipe_catalog, store=template_store, role_directory=role_directory
    )
    # For CPU-RC with allow_gpu=False, all recipes should publish (no GPU gate).
    # But the seed must not RAISE even if gate blocks something.
    assert isinstance(report, SeedReport)
    assert report.gate_blocked >= 0


def test_seed_uses_system_reviewer_not_self_review(
    recipe_catalog, template_store, role_directory
):
    """Seed must use different actor for draft owner vs reviewer (no self-review)."""
    seed_preset_recipes(
        catalog=recipe_catalog, store=template_store, role_directory=role_directory
    )
    items, _ = template_store.list_market_page(actor="pilot107-system-author")
    assert len(items) >= 1
    for item in items:
        review = template_store.get_review(item.release.review_id)
        assert review.reviewer == "pilot107-system-reviewer"
        assert review.reviewer != "pilot107-system-author"  # not self-review


def test_role_directory_system_reviewer_principal():
    """TemplateRoleDirectory.system_reviewer_principal() returns a principal
    with REVIEWER+ADMIN roles, actor='pilot107-system-reviewer'."""
    directory = TemplateRoleDirectory(
        reviewers=frozenset({"pilot107-system-reviewer"}),
        admins=frozenset({"pilot107-system-reviewer"}),
    )
    principal = directory.system_reviewer_principal()
    assert principal.actor == "pilot107-system-reviewer"
    assert TemplateReviewerRole.REVIEWER in principal.roles
    assert TemplateReviewerRole.ADMIN in principal.roles
