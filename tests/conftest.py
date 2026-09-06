"""Explicit SQLite test composition for legacy unit contracts.

Production source composition remains PostgreSQL-only. This pytest plugin
injects SQLite repositories only inside tests that exercise API/worker behavior
without provisioning PostgreSQL.
"""

from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import pytest

import pilot107.api.http_app as http_app_module
import pilot107.api.service as api_service_module
import pilot107.worker.service as worker_service_module
from pilot107.agent.market_sessions import SQLiteMarketSessionStore
from pilot107.agent.project_store import SQLiteProjectStore
from pilot107.agent.store import SQLiteAgentSessionStore
from pilot107.agent.store_factory import DatabaseMode, DurableStoreSelection
from pilot107.agent.task_store import SQLiteAgentTaskStore
from pilot107.api.evidence_query import EvidenceQueryService
from pilot107.api.http_app import Pilot107HttpApi
from pilot107.api.observability_routes import ResourceObservationRoutes
from pilot107.core.contracts import ContractService, ContractStore, RecipeCatalog
from pilot107.core.control_repository import SQLiteControlRepository
from pilot107.core.platform import docker_sim_capability_profile
from pilot107.core.platform_snapshot_store import PlatformSnapshotStore
from pilot107.core.remediation_store import RemediationStore
from pilot107.core.repair_ticket_store import RepairTicketStore
from pilot107.core.run_publications import RunPublicationStore
from pilot107.core.run_store import RunStore
from pilot107.core.template_market import TemplateMarketStore
from pilot107.core.template_policy import TemplatePublicationGate
from pilot107.core.user_entitlement_store import UserEntitlementStore
from pilot107.observability.service import ObservabilityService
from pilot107.observability.store import SQLiteObservabilityStore
from pilot107.services.agent_session_service import AgentSessionService
from pilot107.worker.capsule import RawCapsuleService
from pilot107.worker.evidence import EvidenceStore

_ORIGINAL_HTTP_INIT = Pilot107HttpApi.__init__
_ORIGINAL_API_SERVICE_BUILDER = api_service_module.build_api_service
_ORIGINAL_WORKER_SERVICE_BUILDER = worker_service_module.build_worker_service


def _sqlite_selection(path: Path) -> DurableStoreSelection:
    return DurableStoreSelection(
        mode=DatabaseMode.SQLITE,
        sqlite_path=path.resolve(),
        postgres_dsn=None,
        control_postgres_dsn=None,
    )


def _test_http_init(self: Pilot107HttpApi, *args: object, **kwargs: object) -> None:
    store = kwargs.get("store")
    db_path = getattr(store, "db_path", None)
    if not isinstance(db_path, Path):
        _ORIGINAL_HTTP_INIT(self, *args, **kwargs)
        return
    if kwargs.get("control_repository") is None:
        kwargs["control_repository"] = SQLiteControlRepository(db_path)
    if kwargs.get("remediation_service") is None and kwargs.get("remediation_store") is None:
        kwargs["remediation_store"] = RemediationStore(db_path)
    if kwargs.get("repair_ticket_store") is None:
        kwargs["repair_ticket_store"] = RepairTicketStore(db_path)
    _ORIGINAL_HTTP_INIT(self, *args, **kwargs)


Pilot107HttpApi.__init__ = _test_http_init  # type: ignore[method-assign]


def _build_api_for_tests(
    *,
    db_path: Path,
    evidence_root: Path,
    auth_required: bool = False,
    trusted_user_header: str = "X-Pilot107-User",
) -> Pilot107HttpApi:
    store = RunStore(db_path)
    contract_store = ContractStore(db_path)
    capability_profile = docker_sim_capability_profile()
    partition_qos = capability_profile.partition_qos()
    catalog = RecipeCatalog(
        store=contract_store,
        partition_qos=partition_qos,
        default_partition=capability_profile.default_partition,
        default_qos=capability_profile.default_qos,
    )
    platform_snapshot_store = PlatformSnapshotStore(db_path)
    user_entitlement_store = UserEntitlementStore(db_path)
    contract_service = ContractService(
        catalog=catalog,
        store=contract_store,
        partition_qos=partition_qos,
        qos_limits=capability_profile.qos_limits(),
        platform_snapshot_store=platform_snapshot_store,
        user_entitlement_store=user_entitlement_store,
    )
    control_repository = SQLiteControlRepository(db_path)
    evidence_store = EvidenceStore(evidence_root)
    return Pilot107HttpApi(
        store=store,
        control_repository=control_repository,
        agent_session_service=AgentSessionService(
            store=SQLiteAgentSessionStore(db_path),
            control_repository=control_repository,
        ),
        auth_required=auth_required,
        trusted_user_header=trusted_user_header,
        recipe_catalog=catalog,
        contract_service=contract_service,
        template_market_store=TemplateMarketStore(
            db_path,
            publication_gate=TemplatePublicationGate(contract_service),
            contract_service=contract_service,
        ),
        run_publication_store=RunPublicationStore(
            db_path,
            run_store=store,
            contract_service=contract_service,
        ),
        evidence_query=EvidenceQueryService(
            store=store,
            evidence_store=evidence_store,
        ),
        capsule_service=RawCapsuleService(
            store=store,
            evidence_store=evidence_store,
            capsule_root=evidence_root.parent / "capsules",
            creator="pilot107-api-test",
        ),
        platform_snapshot_store=platform_snapshot_store,
        user_entitlement_store=user_entitlement_store,
        capability_profile=capability_profile,
        observability_routes=ResourceObservationRoutes(
            ObservabilityService(store=SQLiteObservabilityStore(db_path))
        ),
    )


http_app_module.build_api = _build_api_for_tests


def _patch_common_sqlite_builders(
    stack: ExitStack,
    module: object,
    db_path: Path,
) -> None:
    stack.enter_context(
        patch.object(
            module,
            "resolve_durable_store_selection",
            lambda **kwargs: _sqlite_selection(db_path),
        )
    )
    stack.enter_context(
        patch.object(
            module,
            "build_control_repository",
            lambda **kwargs: SQLiteControlRepository(Path(kwargs["sqlite_path"])),
        )
    )
    for name, factory in (
        (
            "build_agent_session_store",
            lambda **kwargs: SQLiteAgentSessionStore(Path(kwargs["sqlite_path"])),
        ),
        (
            "build_project_store",
            lambda **kwargs: SQLiteProjectStore(Path(kwargs["sqlite_path"])),
        ),
        (
            "build_agent_task_store",
            lambda **kwargs: SQLiteAgentTaskStore(Path(kwargs["sqlite_path"])),
        ),
    ):
        if hasattr(module, name):
            stack.enter_context(patch.object(module, name, factory))


def _build_api_service_for_tests(config: object) -> Pilot107HttpApi:
    db_path = Path(getattr(config, "db_path"))
    with ExitStack() as stack:
        _patch_common_sqlite_builders(stack, api_service_module, db_path)
        stack.enter_context(
            patch.object(
                api_service_module,
                "build_market_session_store",
                lambda *, selection: SQLiteMarketSessionStore(selection.sqlite_path),
            )
        )
        return _ORIGINAL_API_SERVICE_BUILDER(config)  # type: ignore[arg-type]


def _build_worker_service_for_tests(config: object) -> object:
    db_path = Path(getattr(config, "db_path"))
    with ExitStack() as stack:
        _patch_common_sqlite_builders(stack, worker_service_module, db_path)
        return _ORIGINAL_WORKER_SERVICE_BUILDER(config)  # type: ignore[arg-type]


api_service_module.build_api_service = _build_api_service_for_tests
worker_service_module.build_worker_service = _build_worker_service_for_tests


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Keep the dedicated lifecycle test bound to production builders."""

    for item in items:
        module = item.module
        if module.__name__.endswith("test_lifecycle_store_selection"):
            module.build_api_service = _ORIGINAL_API_SERVICE_BUILDER
            module.build_worker_service = _ORIGINAL_WORKER_SERVICE_BUILDER
