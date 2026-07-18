"""Seed the template market with preset recipes via the full publish flow.

Walks ``RecipeCatalog.list_versions()`` and publishes each recipe through
``create_draft -> submit_review -> decide_review -> publish``. Idempotent:
already published recipe+version pairs are skipped. Fault-tolerant:
gate-blocked recipes are recorded and do not abort the seed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pilot107.core.contracts import RecipeCatalog, RecipeVersion
from pilot107.core.template_market import TemplateMarketItemRecord, TemplateMarketStore, TemplateVisibility
from pilot107.core.template_policy import TemplateRoleDirectory

_SEED_AUTHOR = "pilot107-system-author"
_SEED_PUBLICATION: dict[str, str] = {
    "license": "MIT",
    "attribution": "107Pilot preset recipe catalog",
    "dataset_access": "No external dataset is required",
    "risk_statement": "Seed preset recipe validated through the publication gate",
}


@dataclass
class SeedReport:
    published: int = 0
    skipped: int = 0
    gate_blocked: int = 0
    errors: list[str] = field(default_factory=list)


def _seed_environment() -> dict[str, str]:
    """Environment values covering every preset recipe's required env fields."""
    return {
        "SLURM_ACCOUNT": "pilot107",
        "KIT_ROOT": "/public/kit",
        "DATA_ROOT": "/public/data",
        "SHARD_ROOT": "/public/shards",
        "EXPECTED_TASKS": "4",
        "CONTRACT_PATH": "/public/contract.json",
    }


def _choose_partition(recipe: RecipeVersion) -> str:
    compatibility = recipe.compatibility or {}
    partitions = compatibility.get("partitions") or {}
    allowed = partitions.get("allowed") if isinstance(partitions, dict) else None
    if isinstance(allowed, list) and allowed:
        for candidate in allowed:
            if isinstance(candidate, str) and candidate:
                return candidate
    return "debug"


def _draft_payload_from_recipe(recipe: RecipeVersion) -> dict[str, Any]:
    """Build a contract payload that satisfies the recipe's required fields."""
    partition = _choose_partition(recipe)
    return {
        "recipe_version_id": recipe.recipe_version_id,
        "project": {"workdir": f"/public/home/{_SEED_AUTHOR}"},
        "entry": {"command": "echo ok"},
        "runtime": {"environment": _seed_environment()},
        "resources": {
            "partition": partition,
            "qos": "normal",
            "nodes": 1,
            "ntasks": 1,
            "cpus_per_task": 1,
            "memory": "1G",
            "time_limit": "00:05:00",
        },
    }


def _draft_compatibility_from_recipe(recipe: RecipeVersion) -> dict[str, Any]:
    """Compatibility metadata: include the chosen partition and CPU-only flag."""
    partition = _choose_partition(recipe)
    return {"partitions": [partition], "gpu": False}


def _already_published(
    store: TemplateMarketStore, recipe: RecipeVersion
) -> bool:
    """Return True if a release for this recipe+version already exists."""
    try:
        items, _ = store.list_market_page(actor=_SEED_AUTHOR, limit=100)
    except Exception:
        return False
    target = recipe.recipe_version_id
    for item in items:
        if not isinstance(item, TemplateMarketItemRecord):
            continue
        payload = item.release.payload or {}
        if payload.get("recipe_version_id") == target:
            return True
    return False


def seed_preset_recipes(
    *,
    catalog: RecipeCatalog,
    store: TemplateMarketStore,
    role_directory: TemplateRoleDirectory,
) -> SeedReport:
    """Publish all preset recipes as template market releases.

    Idempotent: re-running on an already-seeded store is a no-op.
    Fault-tolerant: gate-blocked recipes are recorded, not raised.
    """
    report = SeedReport()
    reviewer_principal = role_directory.system_reviewer_principal()

    for recipe in catalog.list_versions():
        if _already_published(store, recipe):
            report.skipped += 1
            continue
        try:
            draft = store.create_draft(
                owner=_SEED_AUTHOR,
                title=recipe.title,
                description=(
                    f"Seed preset recipe {recipe.recipe_version_id}"
                ),
                visibility=TemplateVisibility.PUBLIC,
                payload=_draft_payload_from_recipe(recipe),
                compatibility=_draft_compatibility_from_recipe(recipe),
                publication=dict(_SEED_PUBLICATION),
                template_id=f"seed-{recipe.recipe_id}",
            )
            review = store.submit_review(
                draft.draft_id, owner=_SEED_AUTHOR, expected_version=draft.version
            )
            decision = store.decide_review(
                review.review_id,
                principal=reviewer_principal,
                expected_version=review.version,
                approve=True,
                note="bootstrap seed: auto-approved",
            )
            store.publish(
                decision.review_id,
                owner=_SEED_AUTHOR,
                release_version=recipe.version,
                request_key=f"pilot107-seed-{recipe.recipe_id}-{recipe.version}",
            )
            report.published += 1
        except Exception as exc:  # noqa: BLE001
            message = str(exc)
            upper = message.upper()
            if "GATE" in upper or "BLOCK" in upper or "OCI" in upper:
                report.gate_blocked += 1
                report.errors.append(
                    f"{recipe.recipe_version_id}: gate-blocked"
                )
            else:
                report.errors.append(
                    f"{recipe.recipe_version_id}: {message}"
                )
    return report
