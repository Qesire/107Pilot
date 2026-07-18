"""Recipe and Contract primitives for the Phase 0A vertical slice."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml

from pilot107.core.contract_v2 import (
    CONTRACT_SCHEMA_V2,
    ContractV2Error,
    contract_digest,
    contract_v2_schema,
    normalize_contract,
)
from pilot107.core.materializer import SUPPORTED_MATERIALIZERS, materialize_contract
from pilot107.core.pagination import CursorPosition
from pilot107.core.platform_preflight import (
    validate_platform_snapshot_resource_plan,
    validate_user_entitlement_resource_plan,
)
from pilot107.core.platform_snapshot import PlatformSnapshotScope
from pilot107.core.platform_snapshot_store import PlatformSnapshotStore
from pilot107.core.resources import (
    REAL107_SIM_PARTITION_QOS,
    ArraySpec,
    PreflightFinding,
    PreflightSeverity,
    QosResourceLimit,
    ResourcePlan,
    validate_resource_plan,
)
from pilot107.core.run_service import RunSubmitRequest, WorkflowPolicy
from pilot107.core.run_store import utc_now_iso
from pilot107.core.user_entitlement_store import UserEntitlementStore


@dataclass(frozen=True)
class RecipeSummary:
    recipe_id: str
    latest_version: str
    title: str
    trust_level: str
    executable: bool


@dataclass(frozen=True)
class RecipeVersion:
    recipe_id: str
    version: str
    title: str
    description: str
    trust_level: str
    parameter_schema: dict[str, Any]
    compatibility: dict[str, Any]
    risk_declaration: dict[str, Any]
    sbatch_template: str | None = None
    preflight_checks: tuple[dict[str, Any], ...] = ()
    recovery: dict[str, Any] | None = None
    success_protocol: dict[str, Any] | None = None
    source: str = "builtin"
    content_sha256: str = ""
    materializer: str = "generic_command"

    @property
    def recipe_version_id(self) -> str:
        return f"{self.recipe_id}@{self.version}"


@dataclass(frozen=True)
class ContractRecord:
    contract_id: str
    owner: str
    recipe_version_id: str
    payload: dict[str, Any]
    field_sources: list[dict[str, Any]]
    created_at: str
    updated_at: str
    schema_version: str = CONTRACT_SCHEMA_V2
    digest: str = ""
    parent_contract_id: str | None = None
    derivation_reason: str | None = None
    source_advice_id: str | None = None
    source_action_id: str | None = None


@dataclass(frozen=True)
class ContractValidationResult:
    status: str
    findings: list[PreflightFinding]
    effective_request: dict[str, Any]
    risk_lint: list[dict[str, Any]]


class ContractError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "CONTRACT.INVALID",
        findings: list[PreflightFinding] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.findings = findings or []


class RecipeCatalog:
    def __init__(
        self,
        recipes: list[RecipeVersion] | None = None,
        *,
        store: ContractStore | None = None,
        template_dir: Path | None = None,
        allow_gpu: bool = True,
        partition_qos: dict[str, tuple[str, ...]] | None = None,
    ) -> None:
        packaged = recipes
        if packaged is None:
            directory = template_dir or _default_template_dir()
            packaged = [_python_cpu_recipe(), *_load_packaged_recipes(directory)]
        packaged = [_with_content_digest(recipe) for recipe in packaged]
        if partition_qos is not None:
            packaged = [
                _with_partition_qos(recipe, partition_qos)
                for recipe in packaged
                if allow_gpu or not _recipe_requires_gpu(recipe)
            ]
        if store is not None:
            for recipe in packaged:
                store.upsert_recipe_version(recipe)
            packaged = store.list_recipe_versions()
        if not allow_gpu:
            packaged = [recipe for recipe in packaged if not _recipe_requires_gpu(recipe)]
        self._recipes = {recipe.recipe_version_id: recipe for recipe in packaged}

    def list_summaries(self) -> list[RecipeSummary]:
        versions_by_recipe: dict[str, list[RecipeVersion]] = {}
        for recipe in self._recipes.values():
            versions_by_recipe.setdefault(recipe.recipe_id, []).append(recipe)
        latest_by_recipe = [
            max(versions, key=lambda item: _semver_key(item.version))
            for versions in versions_by_recipe.values()
        ]
        return [
            RecipeSummary(
                recipe_id=recipe.recipe_id,
                latest_version=recipe.version,
                title=recipe.title,
                trust_level=recipe.trust_level,
                executable=_recipe_is_executable(recipe),
            )
            for recipe in sorted(latest_by_recipe, key=lambda item: item.recipe_id)
        ]

    def get(self, recipe_id: str, version: str) -> RecipeVersion:
        try:
            return self._recipes[f"{recipe_id}@{version}"]
        except KeyError as exc:
            raise KeyError(f"unknown recipe version: {recipe_id}@{version}") from exc

    def get_by_version_id(self, recipe_version_id: str) -> RecipeVersion:
        try:
            return self._recipes[recipe_version_id]
        except KeyError as exc:
            raise KeyError(f"unknown recipe version: {recipe_version_id}") from exc

    def list_versions(self) -> list[RecipeVersion]:
        return sorted(
            self._recipes.values(),
            key=lambda item: (item.recipe_id, _semver_key(item.version)),
        )


class ContractStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    def _initialize(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS contracts (
                    contract_id TEXT PRIMARY KEY,
                    owner TEXT NOT NULL,
                    recipe_version_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    field_sources_json TEXT NOT NULL DEFAULT '[]',
                    schema_version TEXT NOT NULL DEFAULT 'pilot107.contract/v1',
                    digest TEXT NOT NULL DEFAULT '',
                    parent_contract_id TEXT,
                    derivation_reason TEXT,
                    source_advice_id TEXT,
                    source_action_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_contracts_owner
                    ON contracts(owner, created_at);
                CREATE INDEX IF NOT EXISTS idx_contracts_owner_created
                    ON contracts(owner, created_at DESC, contract_id DESC);
                CREATE INDEX IF NOT EXISTS idx_contracts_owner_recipe_created
                    ON contracts(
                        owner, recipe_version_id, created_at DESC, contract_id DESC
                    );

                CREATE TABLE IF NOT EXISTS recipe_versions (
                    recipe_version_id TEXT PRIMARY KEY,
                    recipe_id TEXT NOT NULL,
                    version TEXT NOT NULL,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    trust_level TEXT NOT NULL,
                    parameter_schema_json TEXT NOT NULL,
                    compatibility_json TEXT NOT NULL,
                    risk_declaration_json TEXT NOT NULL,
                    sbatch_template TEXT,
                    preflight_checks_json TEXT NOT NULL DEFAULT '[]',
                    recovery_json TEXT,
                    success_protocol_json TEXT,
                    source TEXT NOT NULL,
                    content_sha256 TEXT NOT NULL,
                    materializer TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(recipe_id, version)
                );

                CREATE INDEX IF NOT EXISTS idx_recipe_versions_recipe
                    ON recipe_versions(recipe_id, version);
                """
            )
            self._ensure_column(
                conn,
                table="contracts",
                column="schema_version",
                definition="TEXT NOT NULL DEFAULT 'pilot107.contract/v1'",
            )
            self._ensure_column(
                conn,
                table="contracts",
                column="digest",
                definition="TEXT NOT NULL DEFAULT ''",
            )
            for column in (
                "parent_contract_id",
                "derivation_reason",
                "source_advice_id",
                "source_action_id",
            ):
                self._ensure_column(
                    conn,
                    table="contracts",
                    column=column,
                    definition="TEXT",
                )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_contracts_parent "
                "ON contracts(parent_contract_id, created_at)"
            )
            self._migrate_contracts(conn)

    def create_contract(
        self,
        *,
        owner: str,
        recipe_version_id: str,
        payload: dict[str, Any],
        field_sources: list[dict[str, Any]] | None = None,
        contract_id: str | None = None,
        parent_contract_id: str | None = None,
        derivation_reason: str | None = None,
        source_advice_id: str | None = None,
        source_action_id: str | None = None,
        idempotent: bool = False,
        connection: sqlite3.Connection | None = None,
    ) -> ContractRecord:
        canonical = normalize_contract(payload)
        now = utc_now_iso()
        selected_contract_id = contract_id or f"contract_{uuid4().hex}"
        if connection is None:
            with self.connect() as conn:
                return self._create_contract_with_connection(
                    conn,
                    owner=owner,
                    recipe_version_id=recipe_version_id,
                    canonical=canonical,
                    field_sources=field_sources or [],
                    contract_id=selected_contract_id,
                    parent_contract_id=parent_contract_id,
                    derivation_reason=derivation_reason,
                    source_advice_id=source_advice_id,
                    source_action_id=source_action_id,
                    idempotent=idempotent,
                    now=now,
                )
        return self._create_contract_with_connection(
            connection,
            owner=owner,
            recipe_version_id=recipe_version_id,
            canonical=canonical,
            field_sources=field_sources or [],
            contract_id=selected_contract_id,
            parent_contract_id=parent_contract_id,
            derivation_reason=derivation_reason,
            source_advice_id=source_advice_id,
            source_action_id=source_action_id,
            idempotent=idempotent,
            now=now,
        )

    def _create_contract_with_connection(
        self,
        conn: sqlite3.Connection,
        *,
        owner: str,
        recipe_version_id: str,
        canonical: dict[str, Any],
        field_sources: list[dict[str, Any]],
        contract_id: str,
        parent_contract_id: str | None,
        derivation_reason: str | None,
        source_advice_id: str | None,
        source_action_id: str | None,
        idempotent: bool,
        now: str,
    ) -> ContractRecord:
        digest = contract_digest(canonical)
        insert_error: sqlite3.IntegrityError | None = None
        try:
            conn.execute(
                """
                INSERT INTO contracts (
                    contract_id, owner, recipe_version_id, payload_json,
                    field_sources_json, schema_version, digest,
                    parent_contract_id, derivation_reason, source_advice_id,
                    source_action_id, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    contract_id,
                    owner,
                    recipe_version_id,
                    json.dumps(canonical, sort_keys=True),
                    json.dumps(field_sources, sort_keys=True),
                    CONTRACT_SCHEMA_V2,
                    digest,
                    parent_contract_id,
                    derivation_reason,
                    source_advice_id,
                    source_action_id,
                    now,
                    now,
                ),
            )
        except sqlite3.IntegrityError as exc:
            if not idempotent:
                raise
            insert_error = exc
        row = conn.execute(
            """
            SELECT contract_id, owner, recipe_version_id, payload_json,
                   field_sources_json, schema_version, digest,
                   parent_contract_id, derivation_reason, source_advice_id,
                   source_action_id, created_at, updated_at
            FROM contracts WHERE contract_id = ?
            """,
            (contract_id,),
        ).fetchone()
        if row is None:
            if insert_error is not None:
                raise insert_error
            raise KeyError(contract_id)
        record = _row_to_contract(row)
        if idempotent and (
            record.owner != owner
            or record.recipe_version_id != recipe_version_id
            or record.digest != digest
            or record.field_sources != field_sources
            or record.parent_contract_id != parent_contract_id
            or record.derivation_reason != derivation_reason
            or record.source_advice_id != source_advice_id
            or record.source_action_id != source_action_id
        ):
            raise ContractError(
                "idempotent contract id refers to different content",
                code="CONTRACT.IDEMPOTENCY_CONFLICT",
            )
        return record

    def get_contract(self, contract_id: str) -> ContractRecord:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT contract_id, owner, recipe_version_id, payload_json,
                       field_sources_json, schema_version, digest,
                       parent_contract_id, derivation_reason, source_advice_id,
                       source_action_id, created_at, updated_at
                FROM contracts
                WHERE contract_id = ?
                """,
                (contract_id,),
            ).fetchone()
        if row is None:
            raise KeyError(contract_id)
        return _row_to_contract(row)

    def list_contracts_page(
        self,
        *,
        owner: str,
        recipe_version_id: str | None = None,
        digest: str | None = None,
        derived: bool | None = None,
        query: str | None = None,
        cursor: CursorPosition | None = None,
        limit: int = 50,
    ) -> tuple[list[ContractRecord], CursorPosition | None]:
        if not owner:
            raise ValueError("owner is required")
        if limit <= 0 or limit > 100:
            raise ValueError("page limit must be between 1 and 100")
        conditions = ["owner = ?"]
        values: list[Any] = [owner]
        if recipe_version_id is not None:
            conditions.append("recipe_version_id = ?")
            values.append(recipe_version_id)
        if digest is not None:
            conditions.append("digest = ?")
            values.append(digest)
        if derived is True:
            conditions.append("parent_contract_id IS NOT NULL")
        elif derived is False:
            conditions.append("parent_contract_id IS NULL")
        if query is not None:
            pattern = f"%{_escape_like(query)}%"
            conditions.append(
                "(contract_id LIKE ? ESCAPE '\\' "
                "OR recipe_version_id LIKE ? ESCAPE '\\' "
                "OR digest LIKE ? ESCAPE '\\')"
            )
            values.extend([pattern, pattern, pattern])
        if cursor is not None:
            conditions.append("(created_at < ? OR (created_at = ? AND contract_id < ?))")
            values.extend([cursor.primary, cursor.primary, cursor.secondary])
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM contracts WHERE "
                + " AND ".join(conditions)
                + " ORDER BY created_at DESC, contract_id DESC LIMIT ?",
                (*values, limit + 1),
            ).fetchall()
        selected = rows[:limit]
        items = [_row_to_contract(row) for row in selected]
        next_position = None
        if len(rows) > limit and selected:
            last = selected[-1]
            next_position = CursorPosition(
                primary=str(last["created_at"]),
                secondary=str(last["contract_id"]),
            )
        return items, next_position

    def upsert_recipe_version(self, recipe: RecipeVersion) -> RecipeVersion:
        now = utc_now_iso()
        with self.connect() as conn:
            existing = conn.execute(
                "SELECT content_sha256 FROM recipe_versions WHERE recipe_version_id = ?",
                (recipe.recipe_version_id,),
            ).fetchone()
            if (
                existing is not None
                and str(existing["content_sha256"])
                and str(existing["content_sha256"]) != recipe.content_sha256
            ):
                raise ContractError(
                    f"recipe version is immutable: {recipe.recipe_version_id}",
                    code="RECIPE.VERSION_IMMUTABLE",
                )
            conn.execute(
                """
                INSERT INTO recipe_versions (
                    recipe_version_id, recipe_id, version, title, description,
                    trust_level, parameter_schema_json, compatibility_json,
                    risk_declaration_json, sbatch_template, preflight_checks_json,
                    recovery_json, success_protocol_json, source, content_sha256,
                    materializer, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(recipe_version_id) DO UPDATE SET
                    title = excluded.title,
                    description = excluded.description,
                    trust_level = excluded.trust_level,
                    parameter_schema_json = excluded.parameter_schema_json,
                    compatibility_json = excluded.compatibility_json,
                    risk_declaration_json = excluded.risk_declaration_json,
                    sbatch_template = excluded.sbatch_template,
                    preflight_checks_json = excluded.preflight_checks_json,
                    recovery_json = excluded.recovery_json,
                    success_protocol_json = excluded.success_protocol_json,
                    source = excluded.source,
                    content_sha256 = excluded.content_sha256,
                    materializer = excluded.materializer,
                    updated_at = excluded.updated_at
                """,
                _recipe_sql_values(recipe, now),
            )
        return self.get_recipe_version(recipe.recipe_version_id)

    def get_recipe_version(self, recipe_version_id: str) -> RecipeVersion:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM recipe_versions WHERE recipe_version_id = ?",
                (recipe_version_id,),
            ).fetchone()
        if row is None:
            raise KeyError(recipe_version_id)
        return _row_to_recipe(row)

    def list_recipe_versions(self) -> list[RecipeVersion]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM recipe_versions ORDER BY recipe_id, version",
            ).fetchall()
        return [_row_to_recipe(row) for row in rows]

    @staticmethod
    def _ensure_column(
        conn: sqlite3.Connection,
        *,
        table: str,
        column: str,
        definition: str,
    ) -> None:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        if column not in {str(row["name"]) for row in rows}:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    @staticmethod
    def _migrate_contracts(conn: sqlite3.Connection) -> None:
        rows = conn.execute(
            "SELECT contract_id, payload_json, digest FROM contracts",
        ).fetchall()
        for row in rows:
            try:
                payload = json.loads(str(row["payload_json"]))
                canonical = normalize_contract(payload)
            except (json.JSONDecodeError, ContractV2Error, TypeError):
                continue
            digest = contract_digest(canonical)
            if row["digest"] == digest and payload == canonical:
                continue
            conn.execute(
                """
                UPDATE contracts
                SET payload_json = ?, schema_version = ?, digest = ?, updated_at = ?
                WHERE contract_id = ?
                """,
                (
                    json.dumps(canonical, sort_keys=True),
                    CONTRACT_SCHEMA_V2,
                    digest,
                    utc_now_iso(),
                    str(row["contract_id"]),
                ),
            )


class ContractService:
    def __init__(
        self,
        *,
        catalog: RecipeCatalog,
        store: ContractStore,
        partition_qos: dict[str, tuple[str, ...]] | None = None,
        qos_limits: dict[str, QosResourceLimit] | None = None,
        platform_snapshot_store: PlatformSnapshotStore | None = None,
        user_entitlement_store: UserEntitlementStore | None = None,
    ) -> None:
        self.catalog = catalog
        self.store = store
        self.partition_qos = partition_qos
        self.qos_limits = qos_limits
        self.platform_snapshot_store = platform_snapshot_store
        self.user_entitlement_store = user_entitlement_store

    def validate(self, payload: dict[str, Any]) -> ContractValidationResult:
        canonical = _normalize_or_contract_error(payload)
        recipe = self.catalog.get_by_version_id(_recipe_version_id(canonical))
        resource_plan = _resource_plan_from_contract(canonical)
        materialization = materialize_contract(canonical, recipe)
        findings = [
            *_validate_required_contract_fields(canonical),
            *validate_resource_plan(
                resource_plan,
                partition_qos=self.partition_qos,
                qos_limits=self.qos_limits,
            ),
            *_validate_recipe_requirements(canonical, recipe),
            *_v2_capability_findings(canonical),
            *materialization.findings,
        ]
        risk_lint = _risk_lint(canonical)
        blocking = [finding for finding in findings if finding.severity == PreflightSeverity.BLOCK]
        status = "BLOCK" if blocking else "OK"
        return ContractValidationResult(
            status=status,
            findings=findings,
            effective_request={
                "recipe_version_id": recipe.recipe_version_id,
                "schema_version": CONTRACT_SCHEMA_V2,
                "contract_digest": contract_digest(canonical),
                "contract": canonical,
                "workdir": str(_workdir(canonical)),
                "script": materialization.script,
                "materializer": materialization.materializer,
                "resource_plan": _resource_plan_to_payload(resource_plan),
            },
            risk_lint=risk_lint,
        )

    def create(self, *, owner: str, payload: dict[str, Any]) -> ContractRecord:
        canonical = _normalize_or_contract_error(payload)
        result = self.validate(canonical)
        if result.status == "BLOCK":
            raise ContractError(
                "contract validation failed",
                code="CONTRACT.PREFLIGHT_BLOCKED",
                findings=result.findings,
            )
        return self.store.create_contract(
            owner=owner,
            recipe_version_id=_recipe_version_id(canonical),
            payload=canonical,
            field_sources=_field_sources(canonical),
        )

    def get(self, contract_id: str) -> ContractRecord:
        return self.store.get_contract(contract_id)

    def create_derived(
        self,
        *,
        source: ContractRecord,
        payload: dict[str, Any],
        contract_id: str,
        advice_id: str,
        action_id: str,
        patched_fields: list[str],
    ) -> ContractRecord:
        canonical = _normalize_or_contract_error(payload)
        result = self.validate(canonical)
        if result.status == "BLOCK":
            raise ContractError(
                "derived contract validation failed",
                code="CONTRACT.PREFLIGHT_BLOCKED",
                findings=result.findings,
            )
        field_sources = [
            *source.field_sources,
            *[
                {
                    "field": field,
                    "source": "agent_advice",
                    "source_advice_id": advice_id,
                    "source_action_id": action_id,
                    "needs_user_confirmation": False,
                }
                for field in sorted(set(patched_fields))
            ],
        ]
        return self.store.create_contract(
            owner=source.owner,
            recipe_version_id=_recipe_version_id(canonical),
            payload=canonical,
            field_sources=field_sources,
            contract_id=contract_id,
            parent_contract_id=source.contract_id,
            derivation_reason="agent_remediation",
            source_advice_id=advice_id,
            source_action_id=action_id,
            idempotent=True,
        )

    def preflight(self, contract: ContractRecord) -> ContractValidationResult:
        result = self.validate(contract.payload)
        if self.platform_snapshot_store is None and self.user_entitlement_store is None:
            return result
        platform_snapshot = (
            None
            if self.platform_snapshot_store is None
            else self.platform_snapshot_store.latest(
                owner=contract.owner,
                scope=PlatformSnapshotScope.LOGIN_NODE,
            )
        )
        entitlement = (
            None
            if self.user_entitlement_store is None
            else self.user_entitlement_store.latest(owner=contract.owner)
        )
        findings = [
            *result.findings,
        ]
        resource_plan = _resource_plan_from_contract(contract.payload)
        if self.platform_snapshot_store is not None:
            findings.extend(
                validate_platform_snapshot_resource_plan(resource_plan, platform_snapshot)
            )
        if self.user_entitlement_store is not None:
            findings.extend(validate_user_entitlement_resource_plan(resource_plan, entitlement))
        return ContractValidationResult(
            status=(
                "BLOCK"
                if any(item.severity == PreflightSeverity.BLOCK for item in findings)
                else "OK"
            ),
            findings=findings,
            effective_request=result.effective_request,
            risk_lint=result.risk_lint,
        )

    def to_submit_request(
        self,
        contract: ContractRecord,
        *,
        parent_run_id: str | None = None,
        lineage_reason: str | None = None,
        remediation_plan_id: str | None = None,
    ) -> RunSubmitRequest:
        result = self.preflight(contract)
        if result.status == "BLOCK":
            raise ContractError(
                "contract validation failed",
                code="CONTRACT.PREFLIGHT_BLOCKED",
                findings=result.findings,
            )
        return RunSubmitRequest(
            owner=contract.owner,
            workdir=_workdir(contract.payload),
            script=_required_materialized_script(result),
            resource_plan=_resource_plan_from_contract(contract.payload),
            contract_id=contract.contract_id,
            parent_run_id=parent_run_id,
            lineage_reason=lineage_reason,
            remediation_plan_id=remediation_plan_id,
            workflow=_workflow_policy(contract.payload),
        )


def render_submitted_script(
    payload: dict[str, Any],
    recipe: RecipeVersion | None = None,
) -> str:
    canonical = _normalize_or_contract_error(payload)
    selected = recipe
    if selected is None:
        if _recipe_version_id(canonical) != "recipe_python_cpu@1.0.0":
            raise ContractError(
                "recipe is required to render a non-builtin contract",
                code="RECIPE.RENDER_SPEC_REQUIRED",
            )
        selected = _python_cpu_recipe()
    result = materialize_contract(canonical, selected)
    if result.script is None:
        raise ContractError(
            "contract cannot be materialized",
            code="CONTRACT.MATERIALIZATION_BLOCKED",
            findings=list(result.findings),
        )
    return result.script


def contract_payload(record: ContractRecord) -> dict[str, Any]:
    return {
        "contract_id": record.contract_id,
        "owner": record.owner,
        "recipe_version_id": record.recipe_version_id,
        "schema_version": record.schema_version,
        "digest": record.digest,
        "parent_contract_id": record.parent_contract_id,
        "derivation_reason": record.derivation_reason,
        "source_advice_id": record.source_advice_id,
        "source_action_id": record.source_action_id,
        "contract": record.payload,
        "field_sources": record.field_sources,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
    }


def validation_payload(result: ContractValidationResult) -> dict[str, Any]:
    return {
        "status": result.status,
        "findings": [_finding_payload(finding) for finding in result.findings],
        "effective_request": result.effective_request,
        "risk_lint": result.risk_lint,
        "configuration_snapshot_id": "phase0_static",
        "observed_at": utc_now_iso(),
    }


def recipe_summary_payload(summary: RecipeSummary) -> dict[str, Any]:
    return {
        "recipe_id": summary.recipe_id,
        "latest_version": summary.latest_version,
        "title": summary.title,
        "trust_level": summary.trust_level,
        "executable": summary.executable,
    }


def contract_schema_payload() -> dict[str, Any]:
    return contract_v2_schema()


def recipe_version_payload(recipe: RecipeVersion) -> dict[str, Any]:
    return {
        "recipe_id": recipe.recipe_id,
        "version": recipe.version,
        "recipe_version_id": recipe.recipe_version_id,
        "title": recipe.title,
        "description": recipe.description,
        "trust_level": recipe.trust_level,
        "parameter_schema": recipe.parameter_schema,
        "compatibility": recipe.compatibility,
        "risk_declaration": recipe.risk_declaration,
        "preflight_checks": list(recipe.preflight_checks),
        "recovery": recipe.recovery,
        "success_protocol": recipe.success_protocol,
        "source": recipe.source,
        "content_sha256": recipe.content_sha256,
        "materializer": recipe.materializer,
        "executable": _recipe_is_executable(recipe),
    }


def _recipe_is_executable(recipe: RecipeVersion) -> bool:
    return recipe.materializer in SUPPORTED_MATERIALIZERS and (
        recipe.materializer != "sbatch_template_v1" or recipe.sbatch_template is not None
    )


def _recipe_requires_gpu(recipe: RecipeVersion) -> bool:
    platform = recipe.compatibility.get("platform", {})
    return isinstance(platform, dict) and platform.get("requires_gpu") is True


def _with_partition_qos(
    recipe: RecipeVersion,
    partition_qos: dict[str, tuple[str, ...]],
) -> RecipeVersion:
    compatibility = json.loads(json.dumps(recipe.compatibility))
    partitions = list(partition_qos)
    allowed_by_partition = {
        partition: list(qos_values) for partition, qos_values in partition_qos.items()
    }
    first_qos = next(
        (qos for values in partition_qos.values() for qos in values),
        None,
    )
    compatibility["partitions"] = {
        "default": partitions[0] if partitions else None,
        "allowed": partitions,
    }
    compatibility["qos"] = {
        "default": first_qos,
        "allowed_by_partition": allowed_by_partition,
    }
    return _with_content_digest(replace(recipe, compatibility=compatibility))


def _required_materialized_script(result: ContractValidationResult) -> str:
    script = result.effective_request.get("script")
    if not isinstance(script, str) or not script:
        raise ContractError(
            "contract materialization did not produce a script",
            code="CONTRACT.MATERIALIZATION_BLOCKED",
            findings=result.findings,
        )
    return script


def _workflow_policy(payload: dict[str, Any]) -> WorkflowPolicy:
    workflow = payload["workflow"]
    policy = payload["policy"]
    return WorkflowPolicy.from_payload(
        {
            "dependencies": workflow.get("dependencies", []),
            "retry": workflow.get("retry", {}),
            "automation": {
                "level": policy.get("automation_level", "explain"),
                "require_approval": policy.get("require_approval", True),
            },
        }
    )


def _python_cpu_recipe() -> RecipeVersion:
    return RecipeVersion(
        recipe_id="recipe_python_cpu",
        version="1.0.0",
        title="Python CPU 基础作业",
        description="Run a CPU Python or shell command and collect logs, environment and outputs.",
        trust_level="L1",
        parameter_schema={
            "required": [
                "project.workdir",
                "entry.command",
                "resources.partition",
                "resources.time_limit",
            ],
            "entry.command": {"type": "plain_shell_command", "raw_shell": False},
        },
        compatibility={
            "slurm": {"min_version": "23.0"},
            "platform": {"docker_l2": True, "school_l3": False, "requires_gpu": False},
            "partitions": {
                "default": "Students",
                "allowed": ["Students", "P107-A100", "P107-RTX5090", "debug"],
            },
            "qos": {
                "default": "qos_stu_medium_2gpu",
                "allowed_by_partition": REAL107_SIM_PARTITION_QOS,
            },
        },
        risk_declaration={
            "blocks": ["empty command", "missing workdir", "invalid resource plan"],
            "warns": ["rm -rf", "curl|bash", "background process"],
        },
    )


def _default_template_dir() -> Path:
    return Path(__file__).resolve().parents[3] / "data" / "submission_templates"


def _load_packaged_recipes(template_dir: Path) -> list[RecipeVersion]:
    if not template_dir.is_dir():
        return []
    recipes: list[RecipeVersion] = []
    for path in sorted(template_dir.glob("*.yaml")):
        if path.name == "INDEX.yaml":
            continue
        raw = path.read_bytes()
        decoded = yaml.safe_load(raw)
        if not isinstance(decoded, dict):
            raise ContractError(
                f"recipe template must be an object: {path.name}",
                code="RECIPE.INVALID_TEMPLATE",
            )
        template_id = decoded.get("template_id")
        if not isinstance(template_id, str) or "@" not in template_id:
            raise ContractError(
                f"recipe template_id is invalid: {path.name}",
                code="RECIPE.INVALID_TEMPLATE",
            )
        recipe_id, version = template_id.rsplit("@", 1)
        recipes.append(
            RecipeVersion(
                recipe_id=recipe_id,
                version=version,
                title=_required_template_string(decoded, "title", path),
                description=_required_template_string(decoded, "description", path),
                trust_level=_required_template_string(decoded, "trust_level", path),
                parameter_schema=_template_dict(decoded, "parameter_schema", path),
                compatibility=_template_dict(decoded, "compatibility", path),
                risk_declaration=_template_dict(decoded, "risk_declaration", path),
                sbatch_template=_optional_template_string(decoded, "sbatch_template", path),
                preflight_checks=tuple(
                    _template_dict_item(item, "preflight_checks", path)
                    for item in _template_list(decoded, "preflight_checks", path)
                ),
                recovery=_template_dict(decoded, "recovery", path),
                success_protocol=_template_dict(decoded, "success_protocol", path),
                source=f"packaged:{path.name}",
                content_sha256=hashlib.sha256(raw).hexdigest(),
                materializer="sbatch_template_v1",
            )
        )
    return recipes


def _required_template_string(payload: dict[str, Any], key: str, path: Path) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ContractError(
            f"recipe {key} is invalid: {path.name}",
            code="RECIPE.INVALID_TEMPLATE",
        )
    return value.strip()


def _optional_template_string(
    payload: dict[str, Any],
    key: str,
    path: Path,
) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ContractError(
            f"recipe {key} is invalid: {path.name}",
            code="RECIPE.INVALID_TEMPLATE",
        )
    return value


def _template_dict(payload: dict[str, Any], key: str, path: Path) -> dict[str, Any]:
    value = payload.get(key)
    if not isinstance(value, dict):
        raise ContractError(
            f"recipe {key} is invalid: {path.name}",
            code="RECIPE.INVALID_TEMPLATE",
        )
    return value


def _template_list(payload: dict[str, Any], key: str, path: Path) -> list[Any]:
    value = payload.get(key)
    if not isinstance(value, list):
        raise ContractError(
            f"recipe {key} is invalid: {path.name}",
            code="RECIPE.INVALID_TEMPLATE",
        )
    return value


def _template_dict_item(value: Any, key: str, path: Path) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError(
            f"recipe {key} item is invalid: {path.name}",
            code="RECIPE.INVALID_TEMPLATE",
        )
    return value


def _recipe_sql_values(recipe: RecipeVersion, now: str) -> tuple[Any, ...]:
    return (
        recipe.recipe_version_id,
        recipe.recipe_id,
        recipe.version,
        recipe.title,
        recipe.description,
        recipe.trust_level,
        json.dumps(recipe.parameter_schema, sort_keys=True),
        json.dumps(recipe.compatibility, sort_keys=True),
        json.dumps(recipe.risk_declaration, sort_keys=True),
        recipe.sbatch_template,
        json.dumps(recipe.preflight_checks, sort_keys=True),
        None if recipe.recovery is None else json.dumps(recipe.recovery, sort_keys=True),
        None
        if recipe.success_protocol is None
        else json.dumps(recipe.success_protocol, sort_keys=True),
        recipe.source,
        recipe.content_sha256,
        recipe.materializer,
        now,
        now,
    )


_SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-([0-9A-Za-z.-]+))?$")


def _semver_key(
    version: str,
) -> tuple[int, int, int, int, tuple[tuple[int, int, str], ...]]:
    match = _SEMVER.fullmatch(version)
    if match is None:
        raise ContractError(
            f"recipe version must use semantic versioning: {version}",
            code="RECIPE.INVALID_VERSION",
        )
    major, minor, patch, prerelease = match.groups()
    prerelease_key: list[tuple[int, int, str]] = []
    for identifier in prerelease.split(".") if prerelease else ():
        if not identifier:
            raise ContractError(
                f"prerelease identifiers cannot be empty: {version}",
                code="RECIPE.INVALID_VERSION",
            )
        if identifier.isdigit():
            if len(identifier) > 1 and identifier.startswith("0"):
                raise ContractError(
                    f"numeric prerelease identifiers cannot have leading zeros: {version}",
                    code="RECIPE.INVALID_VERSION",
                )
            prerelease_key.append((0, int(identifier), ""))
        else:
            prerelease_key.append((1, 0, identifier))
    return (
        int(major),
        int(minor),
        int(patch),
        int(prerelease is None),
        tuple(prerelease_key),
    )


def _with_content_digest(recipe: RecipeVersion) -> RecipeVersion:
    _semver_key(recipe.version)
    if recipe.content_sha256:
        return recipe
    payload = {
        "recipe_id": recipe.recipe_id,
        "version": recipe.version,
        "title": recipe.title,
        "description": recipe.description,
        "trust_level": recipe.trust_level,
        "parameter_schema": recipe.parameter_schema,
        "compatibility": recipe.compatibility,
        "risk_declaration": recipe.risk_declaration,
        "sbatch_template": recipe.sbatch_template,
        "preflight_checks": recipe.preflight_checks,
        "recovery": recipe.recovery,
        "success_protocol": recipe.success_protocol,
        "materializer": recipe.materializer,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return replace(recipe, content_sha256=hashlib.sha256(encoded).hexdigest())


def _row_to_contract(row: sqlite3.Row) -> ContractRecord:
    return ContractRecord(
        contract_id=str(row["contract_id"]),
        owner=str(row["owner"]),
        recipe_version_id=str(row["recipe_version_id"]),
        payload=json.loads(str(row["payload_json"])),
        field_sources=json.loads(str(row["field_sources_json"])),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        schema_version=str(row["schema_version"]),
        digest=str(row["digest"]),
        parent_contract_id=(
            None if row["parent_contract_id"] is None else str(row["parent_contract_id"])
        ),
        derivation_reason=(
            None if row["derivation_reason"] is None else str(row["derivation_reason"])
        ),
        source_advice_id=(
            None if row["source_advice_id"] is None else str(row["source_advice_id"])
        ),
        source_action_id=(
            None if row["source_action_id"] is None else str(row["source_action_id"])
        ),
    )


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _row_to_recipe(row: sqlite3.Row) -> RecipeVersion:
    return RecipeVersion(
        recipe_id=str(row["recipe_id"]),
        version=str(row["version"]),
        title=str(row["title"]),
        description=str(row["description"]),
        trust_level=str(row["trust_level"]),
        parameter_schema=json.loads(str(row["parameter_schema_json"])),
        compatibility=json.loads(str(row["compatibility_json"])),
        risk_declaration=json.loads(str(row["risk_declaration_json"])),
        sbatch_template=None if row["sbatch_template"] is None else str(row["sbatch_template"]),
        preflight_checks=tuple(json.loads(str(row["preflight_checks_json"]))),
        recovery=None if row["recovery_json"] is None else json.loads(str(row["recovery_json"])),
        success_protocol=None
        if row["success_protocol_json"] is None
        else json.loads(str(row["success_protocol_json"])),
        source=str(row["source"]),
        content_sha256=str(row["content_sha256"]),
        materializer=str(row["materializer"]),
    )


def _normalize_or_contract_error(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        return normalize_contract(payload)
    except ContractV2Error as exc:
        raise ContractError(str(exc), code=exc.code) from exc


def _validate_recipe_requirements(
    payload: dict[str, Any],
    recipe: RecipeVersion,
) -> list[PreflightFinding]:
    required = recipe.parameter_schema.get("required", [])
    if not isinstance(required, list):
        raise ContractError(
            "recipe required fields must be an array",
            code="RECIPE.INVALID_SCHEMA",
        )
    findings: list[PreflightFinding] = []
    for field in required:
        if not isinstance(field, str) or not _has_dotted_value(payload, field):
            findings.append(_block("CONTRACT.REQUIRED_FIELD", f"{field} is required"))
    findings.extend(_validate_recipe_parameter_limits(payload, recipe.parameter_schema))
    allowed_partitions = recipe.compatibility.get("partitions", {}).get("allowed", [])
    resources = payload.get("resources", {})
    partition = resources.get("partition") if isinstance(resources, dict) else None
    if allowed_partitions and partition not in allowed_partitions:
        findings.append(
            _block(
                "RECIPE.PARTITION_INCOMPATIBLE",
                f"partition {partition} is not compatible with {recipe.recipe_version_id}",
            )
        )
    return findings


def _validate_recipe_parameter_limits(
    payload: dict[str, Any],
    parameter_schema: dict[str, Any],
) -> list[PreflightFinding]:
    findings: list[PreflightFinding] = []
    for field, specification in parameter_schema.items():
        if field == "required" or not isinstance(specification, dict):
            continue
        value = _dotted_value(payload, field)
        if value is None:
            continue
        minimum = specification.get("minimum")
        maximum = specification.get("maximum")
        if minimum is not None and isinstance(value, (int, float)) and value < minimum:
            findings.append(
                _block(
                    "RECIPE.PARAMETER_BELOW_MINIMUM",
                    f"{field} must be at least {minimum}",
                )
            )
        if maximum is not None and isinstance(value, (int, float)) and value > maximum:
            findings.append(
                _block(
                    "RECIPE.PARAMETER_ABOVE_MAXIMUM",
                    f"{field} must be at most {maximum}",
                )
            )
        if field == "resources.array" and isinstance(value, dict):
            max_concurrency = specification.get("max_concurrency")
            actual = value.get("max_concurrency")
            if (
                isinstance(max_concurrency, int)
                and isinstance(actual, int)
                and actual > max_concurrency
            ):
                findings.append(
                    _block(
                        "RECIPE.ARRAY_CONCURRENCY_EXCEEDED",
                        f"resources.array.max_concurrency must be at most {max_concurrency}",
                    )
                )
    return findings


def _v2_capability_findings(payload: dict[str, Any]) -> list[PreflightFinding]:
    findings: list[PreflightFinding] = []
    workflow = payload["workflow"]
    retry = workflow.get("retry", {})
    policy = payload["policy"]
    if retry.get("max_attempts", 1) > 1 and (
        policy.get("automation_level") != "bounded_auto" or policy.get("require_approval", True)
    ):
        findings.append(
            PreflightFinding(
                severity=PreflightSeverity.WARN,
                code="WORKFLOW.RETRY_APPROVAL_REQUIRED",
                message="retry is configured but automatic retry is disabled by contract policy",
                source_authority="workflow_policy",
            )
        )
    return findings


def _has_dotted_value(payload: dict[str, Any], field: str) -> bool:
    value = _dotted_value(payload, field)
    return value is not None and value != ""


def _dotted_value(payload: dict[str, Any], field: str) -> Any:
    value: Any = payload
    for part in field.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def _recipe_version_id(payload: dict[str, Any]) -> str:
    value = payload.get("recipe_version_id")
    if not isinstance(value, str) or not value.strip():
        raise ContractError("recipe_version_id is required", code="CONTRACT.RECIPE_REQUIRED")
    return value.strip()


def _workdir(payload: dict[str, Any]) -> Path:
    try:
        project = payload["project"]
        if not isinstance(project, dict):
            raise TypeError("project must be an object")
        workdir = project["workdir"]
    except (KeyError, TypeError) as exc:
        raise ContractError(
            "project.workdir is required",
            code="CONTRACT.WORKDIR_REQUIRED",
        ) from exc
    if not isinstance(workdir, str) or not workdir.strip():
        raise ContractError("project.workdir is required", code="CONTRACT.WORKDIR_REQUIRED")
    return Path(workdir)


def _command(payload: dict[str, Any]) -> str:
    try:
        entry = payload["entry"]
        if not isinstance(entry, dict):
            raise TypeError("entry must be an object")
        command = entry["command"]
    except (KeyError, TypeError) as exc:
        raise ContractError("entry.command is required", code="CONTRACT.COMMAND_REQUIRED") from exc
    if not isinstance(command, str) or not command.strip():
        raise ContractError("entry.command is required", code="CONTRACT.COMMAND_REQUIRED")
    if "\x00" in command:
        raise ContractError("entry.command contains NUL", code="CONTRACT.COMMAND_UNSAFE")
    return command.strip()


def _resource_plan_from_contract(payload: dict[str, Any]) -> ResourcePlan:
    try:
        resources = payload["resources"]
        if not isinstance(resources, dict):
            raise TypeError("resources must be an object")
    except (KeyError, TypeError) as exc:
        raise ContractError("resources is required", code="CONTRACT.RESOURCES_REQUIRED") from exc

    memory_value, memory_unit = _parse_memory(resources.get("memory"))
    array_payload = resources.get("array")
    return ResourcePlan(
        partition=str(resources["partition"]),
        qos=None if resources.get("qos") is None else str(resources["qos"]),
        nodes=int(resources.get("nodes", 1)),
        ntasks=int(resources.get("ntasks", 1)),
        cpus_per_task=int(resources.get("cpus_per_task", 1)),
        memory_value=memory_value,
        memory_unit=memory_unit,
        gpus_per_node=None
        if resources.get("gpus_per_node") is None
        else int(resources["gpus_per_node"]),
        gpus_total=None if resources.get("gpus_total") is None else int(resources["gpus_total"]),
        gpu_type=None if resources.get("gpu_type") is None else str(resources["gpu_type"]),
        time_limit=None if resources.get("time_limit") is None else str(resources["time_limit"]),
        array=None
        if array_payload is None
        else ArraySpec(
            expression=str(array_payload["expression"]),
            max_concurrency=None
            if array_payload.get("max_concurrency") is None
            else int(array_payload["max_concurrency"]),
        ),
    )


def _resource_plan_to_payload(plan: ResourcePlan) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "partition": plan.partition,
        "qos": plan.qos,
        "nodes": plan.nodes,
        "ntasks": plan.ntasks,
        "cpus_per_task": plan.cpus_per_task,
        "memory_value": plan.memory_value,
        "memory_unit": plan.memory_unit,
        "gpus_per_node": plan.gpus_per_node,
        "gpus_total": plan.gpus_total,
        "gpu_type": plan.gpu_type,
        "time_limit": plan.time_limit,
    }
    if plan.array is not None:
        payload["array"] = {
            "expression": plan.array.expression,
            "max_concurrency": plan.array.max_concurrency,
        }
    return payload


def _validate_required_contract_fields(payload: dict[str, Any]) -> list[PreflightFinding]:
    findings: list[PreflightFinding] = []
    try:
        _workdir(payload)
    except ContractError as exc:
        findings.append(_block(exc.code, str(exc)))
    try:
        _command(payload)
    except ContractError as exc:
        findings.append(_block(exc.code, str(exc)))
    return findings


def _risk_lint(payload: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        command = _command(payload)
    except ContractError:
        return []
    rules: list[dict[str, Any]] = []
    if "rm -rf" in command:
        rules.append(
            {
                "rule_id": "RISK.RM_RF",
                "severity": "high_risk",
                "message": "command contains rm -rf; verify path scope before submitting",
                "blocking": False,
            }
        )
    if "curl" in command and "| bash" in command:
        rules.append(
            {
                "rule_id": "RISK.CURL_BASH",
                "severity": "high_risk",
                "message": "command pipes network content into bash",
                "blocking": False,
            }
        )
    return rules


def _field_sources(payload: dict[str, Any]) -> list[dict[str, Any]]:
    provided_fields = [
        "recipe_version_id",
        "project.workdir",
        "entry.command",
        "resources.partition",
        "resources.qos",
        "resources.time_limit",
    ]
    return [
        {"field": field, "source": "user", "needs_user_confirmation": False}
        for field in provided_fields
        if _field_exists(payload, field)
    ]


def _field_exists(payload: dict[str, Any], dotted: str) -> bool:
    value: Any = payload
    for part in dotted.split("."):
        if not isinstance(value, dict) or part not in value:
            return False
        value = value[part]
    return True


def _parse_memory(value: Any) -> tuple[int | None, str | None]:
    if value is None:
        return None, None
    text = str(value).strip()
    if not text:
        return None, None
    unit = text[-1]
    if unit.isalpha():
        return int(text[:-1]), unit
    return int(text), None


def _block(code: str, message: str) -> PreflightFinding:
    return PreflightFinding(severity=PreflightSeverity.BLOCK, code=code, message=message)


def _finding_payload(finding: PreflightFinding) -> dict[str, Any]:
    return {
        "severity": finding.severity.value,
        "code": finding.code,
        "message": finding.message,
        "source_authority": finding.source_authority,
    }


def _shell_single_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"
