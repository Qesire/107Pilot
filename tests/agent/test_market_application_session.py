from __future__ import annotations

from pathlib import Path

import pytest

from pilot107.agent.market_sessions import (
    MarketApplicationError,
    MarketApplicationService,
    MarketAssurance,
    ReferenceAdaptationSession,
    SQLiteMarketSessionStore,
    TemplateApplicationSession,
)
from pilot107.agent.project import ExperimentProjectOrigin
from pilot107.agent.project_store import SQLiteProjectStore
from pilot107.agent.sandbox import SandboxExecutor
from pilot107.agent.workspace import WorkspaceChangeSetState
from pilot107.core.contracts import ContractService, ContractStore, RecipeCatalog
from pilot107.core.run_publications import (
    RunPublicationShareManifest,
    RunPublicationStore,
    RunPublicationVisibility,
)
from pilot107.core.run_store import RunStore
from pilot107.core.template_market import (
    TemplateMarketStore,
    TemplateVisibility,
)
from pilot107.core.template_policy import (
    TemplatePublicationGate,
    TemplateReviewerPrincipal,
    TemplateReviewerRole,
)
from pilot107.services.project_agent_service import ProjectAgentService


def test_reference_application_requires_explicit_contract_share(tmp_path: Path) -> None:
    database = tmp_path / "pilot107.db"
    runs = RunStore(database)
    contracts = ContractService(
        catalog=RecipeCatalog(),
        store=ContractStore(database),
    )
    source_contract = contracts.create(
        owner="alice",
        payload=_contract_payload("alice"),
    )
    run = runs.create_run(
        run_id="run-reference-private-contract",
        owner="alice",
        workdir="/public/home/alice/project",
        script="python train.py",
        contract_id=source_contract.contract_id,
    )
    with runs.connect() as connection:
        connection.execute(
            "UPDATE runs SET state = 'SUCCEEDED', terminal_state = 'COMPLETED', "
            "exit_code = '0:0' WHERE run_id = ?",
            (run.run_id,),
        )
    publications = RunPublicationStore(
        database,
        run_store=runs,
        contract_service=contracts,
    )
    publication = publications.publish(
        source_run_id=run.run_id,
        owner="alice",
        title="Reference without Contract authorization",
        description="The market card is visible, but the Contract remains private.",
        visibility=RunPublicationVisibility.CAMPUS,
        scope_key=None,
        request_key="publish-reference-only-card",
        confirmed=True,
        share_manifest=RunPublicationShareManifest(
            title="Reference without Contract authorization",
            visibility=RunPublicationVisibility.CAMPUS,
        ),
    )
    sessions = SQLiteMarketSessionStore(database)
    service = MarketApplicationService(
        store=sessions,
        contract_service=contracts,
        run_publications=publications,
        template_market=None,
        project_service=None,
    )

    with pytest.raises(MarketApplicationError) as captured:
        service.start_reference_adaptation(
            owner="bob",
            publication_id=publication.publication_id,
            user_intent="reuse this successful Run safely",
            request_key="bob-reference-session",
        )

    assert captured.value.code == "MARKET.SOURCE_NOT_ADAPTABLE"
    assert sessions.list_market_applications(owner="bob") == []


def test_reference_application_is_strong_typed_private_and_idempotent(
    tmp_path: Path,
) -> None:
    database = tmp_path / "pilot107.db"
    runs = RunStore(database)
    contracts = ContractService(
        catalog=RecipeCatalog(),
        store=ContractStore(database),
    )
    source_contract = contracts.create(
        owner="alice",
        payload=_contract_payload("alice"),
    )
    run = runs.create_run(
        run_id="run-reference-shared-contract",
        owner="alice",
        workdir="/public/home/alice/project",
        script="python train.py",
        contract_id=source_contract.contract_id,
    )
    with runs.connect() as connection:
        connection.execute(
            "UPDATE runs SET state = 'SUCCEEDED', terminal_state = 'COMPLETED', "
            "exit_code = '0:0' WHERE run_id = ?",
            (run.run_id,),
        )
    publications = RunPublicationStore(
        database,
        run_store=runs,
        contract_service=contracts,
    )
    manifest = RunPublicationShareManifest(
        title="Reference with Contract authorization",
        visibility=RunPublicationVisibility.CAMPUS,
        contract_for_adaptation=True,
    )
    publication = publications.publish(
        source_run_id=run.run_id,
        owner="alice",
        title=manifest.title,
        description="The Contract may be adapted after an Agent plan.",
        visibility=manifest.visibility,
        scope_key=None,
        request_key="publish-adaptable-reference",
        confirmed=True,
        share_manifest=manifest,
    )
    sessions = SQLiteMarketSessionStore(database)
    service = MarketApplicationService(
        store=sessions,
        contract_service=contracts,
        run_publications=publications,
        template_market=None,
        project_service=None,
    )

    started = service.start_reference_adaptation(
        owner="bob",
        publication_id=publication.publication_id,
        user_intent="adapt the reference into my private project",
        request_key="bob-adapt-reference",
    )
    replayed = service.start_reference_adaptation(
        owner="bob",
        publication_id=publication.publication_id,
        user_intent="adapt the reference into my private project",
        request_key="bob-adapt-reference",
    )

    assert isinstance(started, ReferenceAdaptationSession)
    assert started.application.assurance is MarketAssurance.REFERENCE_ONLY
    assert started.application.state == "awaiting_confirmation"
    assert started.source_run_id == run.run_id
    assert started.source_contract_id == source_contract.contract_id
    assert started.share_manifest_digest == manifest.manifest_digest
    assert started.application.target_contract_id is None
    assert replayed == started
    assert len(sessions.list_market_applications(owner="bob")) == 1


def test_reference_finalizer_creates_exact_private_contract_lineage(
    tmp_path: Path,
) -> None:
    database = tmp_path / "pilot107.db"
    runs = RunStore(database)
    contracts = ContractService(
        catalog=RecipeCatalog(),
        store=ContractStore(database),
    )
    source_contract = contracts.create(
        owner="alice",
        payload=_contract_payload("alice"),
    )
    run = runs.create_run(
        run_id="run-reference-finalize",
        owner="alice",
        workdir="/public/home/alice/project",
        script="python train.py",
        contract_id=source_contract.contract_id,
    )
    with runs.connect() as connection:
        connection.execute(
            "UPDATE runs SET state = 'SUCCEEDED', terminal_state = 'COMPLETED', "
            "exit_code = '0:0' WHERE run_id = ?",
            (run.run_id,),
        )
    publications = RunPublicationStore(
        database,
        run_store=runs,
        contract_service=contracts,
    )
    manifest = RunPublicationShareManifest(
        title="Finalizable reference",
        visibility=RunPublicationVisibility.CAMPUS,
        contract_for_adaptation=True,
    )
    publication = publications.publish(
        source_run_id=run.run_id,
        owner="alice",
        title=manifest.title,
        description="Agent adaptation is required.",
        visibility=manifest.visibility,
        scope_key=None,
        request_key="publish-finalizable-reference",
        confirmed=True,
        share_manifest=manifest,
    )
    sessions = SQLiteMarketSessionStore(database)
    service = MarketApplicationService(
        store=sessions,
        contract_service=contracts,
        run_publications=publications,
        template_market=None,
        project_service=None,
    )
    started = service.start_reference_adaptation(
        owner="bob",
        publication_id=publication.publication_id,
        user_intent="adapt into Bob's private workspace",
        request_key="bob-finalize-reference",
    )

    with pytest.raises(MarketApplicationError) as stale:
        service.finalize_reference_adaptation(
            session_id=started.application.session_id,
            owner="bob",
            expected_version=started.application.version,
            confirmation_digest="0" * 64,
            request_key="bob-finalize-reference-contract",
        )
    assert stale.value.code == "MARKET.CONFIRMATION_STALE"
    assert contracts.store.list_contracts_page(owner="bob")[0] == []

    completed = service.finalize_reference_adaptation(
        session_id=started.application.session_id,
        owner="bob",
        expected_version=started.application.version,
        confirmation_digest=started.confirmation_digest,
        request_key="bob-finalize-reference-contract",
    )
    replayed = service.finalize_reference_adaptation(
        session_id=started.application.session_id,
        owner="bob",
        expected_version=completed.application.version,
        confirmation_digest=started.confirmation_digest,
        request_key="bob-finalize-reference-contract",
    )

    assert completed.application.state == "completed"
    assert completed.application.assurance is MarketAssurance.REFERENCE_ONLY
    assert completed.application.target_contract_id is not None
    target = contracts.get(completed.application.target_contract_id)
    assert target.owner == "bob"
    assert target.parent_contract_id == source_contract.contract_id
    assert target.derivation_reason == "run_publication_adaptation"
    assert target.payload["project"]["workdir"] == "/public/home/bob/project"
    assert target.field_sources[0]["market_application_session_id"] == (
        started.application.session_id
    )
    assert target.field_sources[0]["assurance"] == "reference_only"
    assert replayed == completed
    assert len(contracts.store.list_contracts_page(owner="bob")[0]) == 1


def test_curated_application_uses_only_its_typed_finalizer(tmp_path: Path) -> None:
    database = tmp_path / "pilot107.db"
    runs = RunStore(database)
    contracts = ContractService(
        catalog=RecipeCatalog(),
        store=ContractStore(database),
    )
    templates = TemplateMarketStore(
        database,
        publication_gate=TemplatePublicationGate(contracts),
        contract_service=contracts,
    )
    draft = templates.create_draft(
        owner="alice",
        title="Curated training template",
        description="Reviewed reusable training Contract",
        visibility=TemplateVisibility.CAMPUS,
        payload=_contract_payload("{owner}"),
        compatibility={"partitions": ["debug"], "gpu": False},
        publication={
            "license": "MIT",
            "attribution": "Original reusable example",
            "dataset_access": "No external dataset",
            "risk_statement": "Runs in the selected Slurm allocation",
        },
    )
    review = templates.submit_review(
        draft.draft_id,
        owner="alice",
        expected_version=draft.version,
    )
    templates.decide_review(
        review.review_id,
        principal=TemplateReviewerPrincipal(
            actor="reviewer",
            roles=frozenset({TemplateReviewerRole.REVIEWER}),
        ),
        expected_version=review.version,
        approve=True,
    )
    release = templates.publish(
        review.review_id,
        owner="alice",
        release_version="1.0.0",
        request_key="publish-curated-training",
    )
    publications = RunPublicationStore(
        database,
        run_store=runs,
        contract_service=contracts,
    )
    service = MarketApplicationService(
        store=SQLiteMarketSessionStore(database),
        contract_service=contracts,
        run_publications=publications,
        template_market=templates,
        project_service=None,
    )

    started = service.start_template_application(
        owner="bob",
        release_id=release.release_id,
        user_intent="instantiate the curated template privately",
        request_key="bob-curated-session",
    )

    assert isinstance(started, TemplateApplicationSession)
    assert started.application.assurance is MarketAssurance.CURATED
    assert started.release_id == release.release_id
    with pytest.raises(MarketApplicationError) as mismatch:
        service.finalize_reference_adaptation(
            session_id=started.application.session_id,
            owner="bob",
            expected_version=started.application.version,
            confirmation_digest=started.confirmation_digest,
            request_key="wrong-finalizer",
        )
    assert mismatch.value.code == "MARKET.ASSURANCE_MISMATCH"
    assert contracts.store.list_contracts_page(owner="bob")[0] == []

    completed = service.finalize_template_application(
        session_id=started.application.session_id,
        owner="bob",
        expected_version=started.application.version,
        confirmation_digest=started.confirmation_digest,
        request_key="bob-curated-contract",
    )

    assert completed.application.state == "completed"
    assert completed.application.target_contract_id is not None
    target = contracts.get(completed.application.target_contract_id)
    assert target.owner == "bob"
    assert target.derivation_reason == "template_application"
    assert target.payload["project"]["workdir"] == "/public/home/bob/project"
    assert target.field_sources[0]["market_application_session_id"] == (
        started.application.session_id
    )
    assert target.field_sources[0]["assurance"] == "curated"
    adoption = templates.get_adoption_for_contract(
        release_id=release.release_id,
        adopter="bob",
        contract_id=target.contract_id,
    )
    assert adoption.adoption_id == completed.application.adoption_id


def test_curated_application_creates_isolated_reviewable_project(tmp_path: Path) -> None:
    database = tmp_path / "pilot107.db"
    runs = RunStore(database)
    contracts = ContractService(
        catalog=RecipeCatalog(),
        store=ContractStore(database),
    )
    templates = TemplateMarketStore(
        database,
        publication_gate=TemplatePublicationGate(contracts),
        contract_service=contracts,
    )
    draft = templates.create_draft(
        owner="alice",
        title="Curated project template",
        description="Creates an isolated application Project",
        visibility=TemplateVisibility.CAMPUS,
        payload=_contract_payload("{owner}"),
        compatibility={"partitions": ["debug"], "gpu": False},
        publication={
            "license": "MIT",
            "attribution": "Reusable example",
            "dataset_access": "No external dataset",
            "risk_statement": "Runs in a bounded Slurm allocation",
        },
    )
    review = templates.submit_review(
        draft.draft_id,
        owner="alice",
        expected_version=draft.version,
    )
    templates.decide_review(
        review.review_id,
        principal=TemplateReviewerPrincipal(
            actor="reviewer",
            roles=frozenset({TemplateReviewerRole.REVIEWER}),
        ),
        expected_version=review.version,
        approve=True,
    )
    release = templates.publish(
        review.review_id,
        owner="alice",
        release_version="1.0.0",
        request_key="publish-project-template",
    )
    project_store = SQLiteProjectStore(database)
    projects = ProjectAgentService(
        store=project_store,
        workspace_root=tmp_path / "workspaces",
        sandbox=SandboxExecutor(store=project_store),
    )
    service = MarketApplicationService(
        store=SQLiteMarketSessionStore(database),
        contract_service=contracts,
        run_publications=RunPublicationStore(
            database,
            run_store=runs,
            contract_service=contracts,
        ),
        template_market=templates,
        project_service=projects,
    )

    started = service.start_template_application(
        owner="bob",
        release_id=release.release_id,
        user_intent="review the curated Contract before use",
        request_key="bob-curated-project",
    )

    assert started.application.project_id is not None
    assert started.application.workspace_id is not None
    assert started.application.change_set_id is not None
    view = projects.get_project(
        started.application.project_id,
        owner="bob",
        workspace_id=started.application.workspace_id,
    )
    assert view.project.origin is ExperimentProjectOrigin.TEMPLATE
    assert view.project.source is not None
    assert view.project.source.ref_id == release.release_id
    assert len(view.change_sets) == 1
    assert view.change_sets[0].state is WorkspaceChangeSetState.REVIEWABLE
    assert [item.path for item in view.change_sets[0].files] == ["contract.json"]
    assert started.change_set_digest == view.change_sets[0].digest
    assert started.application.target_contract_id is None


def _contract_payload(owner: str) -> dict[str, object]:
    return {
        "recipe_version_id": "recipe_python_cpu@1.0.0",
        "project": {"workdir": f"/public/home/{owner}/project"},
        "entry": {"command": "python train.py"},
        "resources": {
            "partition": "debug",
            "qos": "normal",
            "nodes": 1,
            "ntasks": 1,
            "cpus_per_task": 1,
            "time_limit": "00:05:00",
        },
    }
