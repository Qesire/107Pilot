from __future__ import annotations

import json
from pathlib import Path

import pytest

from pilot107.agent.market_sessions import (
    SQLiteMarketSessionStore,
    TemplatePublicationError,
    TemplatePublicationService,
)
from pilot107.api.market_session_routes import MarketSessionRoutes
from pilot107.core.contracts import ContractService, ContractStore, RecipeCatalog
from pilot107.core.identity import UserIdentity
from pilot107.core.run_publications import (
    RunPublicationError,
    RunPublicationShareManifest,
    RunPublicationStore,
    RunPublicationVisibility,
)
from pilot107.core.run_store import RunStore
from pilot107.core.template_market import TemplateMarketStore, TemplateVisibility
from pilot107.core.template_policy import (
    TemplatePublicationGate,
    TemplateReviewerPrincipal,
    TemplateReviewerRole,
)


def test_successful_run_creates_no_market_record_without_share_manifest(
    tmp_path: Path,
) -> None:
    database = tmp_path / "pilot107.db"
    runs = RunStore(database)
    contracts = ContractService(
        catalog=RecipeCatalog(),
        store=ContractStore(database),
    )
    contract = contracts.create(owner="alice", payload=_contract_payload("alice"))
    run = runs.create_run(
        run_id="run-success-private",
        owner="alice",
        workdir="/public/home/alice/project",
        script="python train.py",
        contract_id=contract.contract_id,
    )
    with runs.connect() as connection:
        connection.execute(
            """
            UPDATE runs
            SET state = 'SUCCEEDED', terminal_state = 'COMPLETED', exit_code = '0:0',
                collection_state = 'succeeded', diagnosis_state = 'skipped'
            WHERE run_id = ?
            """,
            (run.run_id,),
        )
    run = runs.get_run(run.run_id)
    publications = RunPublicationStore(
        database,
        run_store=runs,
        contract_service=contracts,
    )
    sessions = SQLiteMarketSessionStore(database)
    service = TemplatePublicationService(
        store=sessions,
        run_store=runs,
        contract_service=contracts,
        run_publications=publications,
        template_market=None,
    )

    observed = service.observe_successful_run(run)

    assert observed is None
    assert (
        publications.get_for_source_run(
            source_run_id=run.run_id,
            owner="alice",
        )
        is None
    )
    assert (
        sessions.list_template_publications(
            owner="alice",
            source_run_id=run.run_id,
        )
        == []
    )


def test_share_manifest_defaults_private_and_does_not_authorize_contract(
    tmp_path: Path,
) -> None:
    database = tmp_path / "pilot107.db"
    runs = RunStore(database)
    contracts = ContractService(
        catalog=RecipeCatalog(),
        store=ContractStore(database),
    )
    contract = contracts.create(owner="alice", payload=_contract_payload("alice"))
    run = runs.create_run(
        run_id="run-explicit-share",
        owner="alice",
        workdir="/public/home/alice/project",
        script="python train.py",
        contract_id=contract.contract_id,
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
    service = TemplatePublicationService(
        store=SQLiteMarketSessionStore(database),
        run_store=runs,
        contract_service=contracts,
        run_publications=publications,
        template_market=None,
    )
    manifest = RunPublicationShareManifest(title="Private successful Run")

    publication = service.publish_run_reference(
        source_run_id=run.run_id,
        owner="alice",
        request_key="share-private-run",
        manifest=manifest,
        description="Shared only after explicit confirmation",
    )

    assert manifest.visibility is RunPublicationVisibility.PRIVATE
    assert manifest.description is True
    assert manifest.resource_summary is False
    assert manifest.result_summary is False
    assert manifest.contract_for_adaptation is False
    assert manifest.script is False
    assert manifest.evidence_previews is False
    assert manifest.small_assets == ()
    assert len(manifest.manifest_digest) == 64
    assert publication.visibility is RunPublicationVisibility.PRIVATE
    assert publication.source_contract_id is None
    assert publication.adoptable is False
    assert publication.share_manifest_digest == manifest.manifest_digest


def test_share_manifest_rejects_forbidden_selected_script_before_persisting(
    tmp_path: Path,
) -> None:
    database = tmp_path / "pilot107.db"
    runs = RunStore(database)
    contracts = ContractService(
        catalog=RecipeCatalog(),
        store=ContractStore(database),
    )
    contract = contracts.create(owner="alice", payload=_contract_payload("alice"))
    run = runs.create_run(
        run_id="run-secret-script",
        owner="alice",
        workdir="/public/home/alice/project",
        script="python train.py --token ghp_12345678901234567890",
        contract_id=contract.contract_id,
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
    service = TemplatePublicationService(
        store=SQLiteMarketSessionStore(database),
        run_store=runs,
        contract_service=contracts,
        run_publications=publications,
        template_market=None,
    )

    with pytest.raises(RunPublicationError) as captured:
        service.publish_run_reference(
            source_run_id=run.run_id,
            owner="alice",
            request_key="share-secret-script",
            manifest=RunPublicationShareManifest(
                title="Unsafe selected script",
                script=True,
            ),
        )

    assert captured.value.code == "MARKET.SHARE_MANIFEST_FORBIDDEN"
    assert (
        publications.get_for_source_run(
            source_run_id=run.run_id,
            owner="alice",
        )
        is None
    )


def test_template_publication_starts_from_sanitized_run_bound_bundle(
    tmp_path: Path,
) -> None:
    database = tmp_path / "pilot107.db"
    runs = RunStore(database)
    contracts = ContractService(
        catalog=RecipeCatalog(),
        store=ContractStore(database),
    )
    source_payload = _contract_payload("alice")
    source_payload["extensions"] = {
        "owner_note": "prepared by alice",
        "source_run_note": "derived from run-template-source",
    }
    contract = contracts.create(owner="alice", payload=source_payload)
    run = runs.create_run(
        run_id="run-template-source",
        owner="alice",
        workdir="/public/home/alice/project",
        script="python train.py",
        contract_id=contract.contract_id,
    )
    with runs.connect() as connection:
        connection.execute(
            "UPDATE runs SET state = 'SUCCEEDED', terminal_state = 'COMPLETED', "
            "exit_code = '0:0', collection_state = 'succeeded' WHERE run_id = ?",
            (run.run_id,),
        )
    templates = TemplateMarketStore(
        database,
        publication_gate=TemplatePublicationGate(contracts),
        contract_service=contracts,
    )
    sessions = SQLiteMarketSessionStore(database)
    service = TemplatePublicationService(
        store=sessions,
        run_store=runs,
        contract_service=contracts,
        run_publications=RunPublicationStore(
            database,
            run_store=runs,
            contract_service=contracts,
        ),
        template_market=templates,
    )

    started = service.start_template_publication(
        owner="alice",
        source_run_id=run.run_id,
        request_key="alice-template-publication",
        title="Reusable training template",
        description="Sanitized and reproduced before release",
        visibility=TemplateVisibility.CAMPUS,
        scope_key=None,
        compatibility={"partitions": ["debug"], "gpu": False},
        publication_metadata={
            "license": "MIT",
            "attribution": "Reusable training example",
            "dataset_access": "No external dataset",
            "risk_statement": "Runs in a bounded Slurm allocation",
        },
    )

    assert started.state == "awaiting_reproduction"
    assert started.source_contract_id == contract.contract_id
    assert started.draft_id is not None
    assert len(started.bundle_digest) == 64
    assert started.release_id is None
    draft = templates.get_draft(started.draft_id, owner="alice")
    assert draft.payload["project"]["workdir"] == "/public/home/{owner}/project"
    serialized = str(draft.payload)
    assert "alice" not in serialized
    assert run.run_id not in serialized
    assert sessions.list_template_publications(
        owner="alice",
        source_run_id=run.run_id,
    ) == [started]


def test_template_publication_blocks_secret_before_draft_or_session(tmp_path: Path) -> None:
    database = tmp_path / "pilot107.db"
    runs = RunStore(database)
    contracts = ContractService(
        catalog=RecipeCatalog(),
        store=ContractStore(database),
    )
    payload = _contract_payload("alice")
    payload["runtime"] = {"environment": {"api_token": "ghp_12345678901234567890"}}
    contract = contracts.store.create_contract(
        owner="alice",
        recipe_version_id="recipe_python_cpu@1.0.0",
        payload=payload,
        contract_id="contract-template-secret",
    )
    run = runs.create_run(
        run_id="run-template-secret",
        owner="alice",
        workdir="/public/home/alice/project",
        script="python train.py",
        contract_id=contract.contract_id,
    )
    with runs.connect() as connection:
        connection.execute(
            "UPDATE runs SET state = 'SUCCEEDED', terminal_state = 'COMPLETED', "
            "exit_code = '0:0', collection_state = 'succeeded' WHERE run_id = ?",
            (run.run_id,),
        )
    templates = TemplateMarketStore(
        database,
        publication_gate=TemplatePublicationGate(contracts),
        contract_service=contracts,
    )
    sessions = SQLiteMarketSessionStore(database)
    service = TemplatePublicationService(
        store=sessions,
        run_store=runs,
        contract_service=contracts,
        run_publications=RunPublicationStore(
            database,
            run_store=runs,
            contract_service=contracts,
        ),
        template_market=templates,
    )

    with pytest.raises(TemplatePublicationError) as captured:
        service.start_template_publication(
            owner="alice",
            source_run_id=run.run_id,
            request_key="blocked-secret-template",
            title="Must not persist",
            description="contains a secret",
            visibility=TemplateVisibility.CAMPUS,
            scope_key=None,
            compatibility={"partitions": ["debug"], "gpu": False},
            publication_metadata={
                "license": "MIT",
                "attribution": "Unsafe source",
                "dataset_access": "No external dataset",
                "risk_statement": "Runs in a bounded allocation",
            },
        )

    assert captured.value.code == "TEMPLATE.SANITIZATION_BLOCKED"
    assert sessions.list_template_publications(owner="alice") == []
    assert templates.list_drafts_page(owner="alice")[0] == ()


def test_template_publication_requires_reproduction_confirmation_and_review(
    tmp_path: Path,
) -> None:
    database = tmp_path / "pilot107.db"
    runs = RunStore(database)
    contracts = ContractService(
        catalog=RecipeCatalog(),
        store=ContractStore(database),
    )
    contract = contracts.create(owner="alice", payload=_contract_payload("alice"))
    run = runs.create_run(
        run_id="run-template-reviewed",
        owner="alice",
        workdir="/public/home/alice/project",
        script="python train.py",
        contract_id=contract.contract_id,
    )
    with runs.connect() as connection:
        connection.execute(
            "UPDATE runs SET state = 'SUCCEEDED', terminal_state = 'COMPLETED', "
            "exit_code = '0:0', collection_state = 'succeeded' WHERE run_id = ?",
            (run.run_id,),
        )
    templates = TemplateMarketStore(
        database,
        publication_gate=TemplatePublicationGate(contracts),
        contract_service=contracts,
    )
    service = TemplatePublicationService(
        store=SQLiteMarketSessionStore(database),
        run_store=runs,
        contract_service=contracts,
        run_publications=RunPublicationStore(
            database,
            run_store=runs,
            contract_service=contracts,
        ),
        template_market=templates,
    )
    started = service.start_template_publication(
        owner="alice",
        source_run_id=run.run_id,
        request_key="reviewed-template-session",
        title="Reviewed template",
        description="Requires isolated reproduction",
        visibility=TemplateVisibility.CAMPUS,
        scope_key=None,
        compatibility={"partitions": ["debug"], "gpu": False},
        publication_metadata={
            "license": "MIT",
            "attribution": "Reusable example",
            "dataset_access": "No external dataset",
            "risk_statement": "Runs in a bounded allocation",
        },
    )

    with pytest.raises(TemplatePublicationError) as missing:
        service.submit_template_publication_review(
            session_id=started.session_id,
            owner="alice",
            expected_version=started.version,
            confirmation_digest="0" * 64,
        )
    assert missing.value.code == "TEMPLATE.REPRODUCTION_EVIDENCE_MISSING"

    reproduced = service.record_template_reproduction(
        session_id=started.session_id,
        owner="alice",
        expected_version=started.version,
        evidence_ref="evidence://runs/reproduction/manifest/manifest.json",
        evidence_digest="a" * 64,
        environment="docker",
        release_version="1.0.0",
    )
    assert reproduced.state == "awaiting_confirmation"
    assert reproduced.confirmation_digest is not None

    submitted = service.submit_template_publication_review(
        session_id=reproduced.session_id,
        owner="alice",
        expected_version=reproduced.version,
        confirmation_digest=reproduced.confirmation_digest,
    )
    assert submitted.state == "submitted"
    assert submitted.review_id is not None

    completed = service.approve_and_publish_template(
        session_id=submitted.session_id,
        owner="alice",
        expected_version=submitted.version,
        reviewer=TemplateReviewerPrincipal(
            actor="reviewer",
            roles=frozenset({TemplateReviewerRole.REVIEWER}),
        ),
        release_version="1.0.0",
        request_key="publish-reviewed-template",
    )

    assert completed.state == "completed"
    assert completed.release_id is not None
    assert completed.release_version == "1.0.0"
    release = templates.get_release(completed.release_id)
    assert release.publication["bundle_digest"] == started.bundle_digest
    assert release.release_version == "1.0.0"
    assert release.withdrawn_at is None

    revised_payload = _contract_payload("alice")
    revised_payload["resources"]["cpus_per_task"] = 2  # type: ignore[index]
    revised_contract = contracts.create(owner="alice", payload=revised_payload)
    revised_run = runs.create_run(
        run_id="run-template-reviewed-v2",
        owner="alice",
        workdir="/public/home/alice/project",
        script="python train.py",
        contract_id=revised_contract.contract_id,
    )
    with runs.connect() as connection:
        connection.execute(
            "UPDATE runs SET state = 'SUCCEEDED', terminal_state = 'COMPLETED', "
            "exit_code = '0:0', collection_state = 'succeeded' WHERE run_id = ?",
            (revised_run.run_id,),
        )
    revised = service.start_template_publication(
        owner="alice",
        source_run_id=revised_run.run_id,
        request_key="reviewed-template-v2-session",
        title="Reviewed template",
        description="A resource-tuned new version",
        visibility=TemplateVisibility.CAMPUS,
        scope_key=None,
        compatibility={"partitions": ["debug"], "gpu": False},
        publication_metadata={
            "license": "MIT",
            "attribution": "Reusable example",
            "dataset_access": "No external dataset",
            "risk_statement": "Runs in a bounded allocation",
        },
        base_release_id=release.release_id,
    )
    revised_reproduction = service.record_template_reproduction(
        session_id=revised.session_id,
        owner="alice",
        expected_version=revised.version,
        evidence_ref="evidence://runs/reproduction-v2/manifest/manifest.json",
        evidence_digest="b" * 64,
        environment="docker",
        release_version="1.1.0",
    )
    revised_review = service.submit_template_publication_review(
        session_id=revised.session_id,
        owner="alice",
        expected_version=revised_reproduction.version,
        confirmation_digest=str(revised_reproduction.confirmation_digest),
    )
    revised_completed = service.approve_and_publish_template(
        session_id=revised.session_id,
        owner="alice",
        expected_version=revised_review.version,
        reviewer=TemplateReviewerPrincipal(
            actor="reviewer",
            roles=frozenset({TemplateReviewerRole.REVIEWER}),
        ),
        release_version="1.1.0",
        request_key="publish-reviewed-template-v2",
    )
    assert revised_completed.release_id is not None
    revised_release = templates.get_release(revised_completed.release_id)
    assert revised_release.template_id == release.template_id
    assert revised_release.release_version == "1.1.0"
    assert revised_release.publication["supersedes_release_id"] == release.release_id

    templates.withdraw_release(
        release.release_id,
        actor="alice",
        reason="superseded by reviewed 1.1.0",
    )
    assert templates.get_release(release.release_id).withdrawn_at is not None
    assert templates.get_release(revised_release.release_id).withdrawn_at is None


def test_equivalent_bundle_creates_verification_not_duplicate_release(
    tmp_path: Path,
) -> None:
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
    sessions = SQLiteMarketSessionStore(database)
    service = TemplatePublicationService(
        store=sessions,
        run_store=runs,
        contract_service=contracts,
        run_publications=RunPublicationStore(
            database,
            run_store=runs,
            contract_service=contracts,
        ),
        template_market=templates,
    )
    publication_metadata = {
        "license": "MIT",
        "attribution": "Reusable example",
        "dataset_access": "No external dataset",
        "risk_statement": "Runs in a bounded allocation",
    }

    first_contract = contracts.create(owner="alice", payload=_contract_payload("alice"))
    first_run = runs.create_run(
        run_id="run-bundle-first",
        owner="alice",
        workdir="/public/home/alice/project",
        script="python train.py",
        contract_id=first_contract.contract_id,
    )
    with runs.connect() as connection:
        connection.execute(
            "UPDATE runs SET state = 'SUCCEEDED', terminal_state = 'COMPLETED', "
            "exit_code = '0:0', collection_state = 'succeeded', job_id = '101' "
            "WHERE run_id = ?",
            (first_run.run_id,),
        )
    first = service.start_template_publication(
        owner="alice",
        source_run_id=first_run.run_id,
        request_key="first-equivalent-bundle",
        title="First catalog title",
        description="First catalog description",
        visibility=TemplateVisibility.CAMPUS,
        scope_key=None,
        compatibility={"partitions": ["debug"], "gpu": False},
        publication_metadata=publication_metadata,
    )
    reproduced = service.record_template_reproduction(
        session_id=first.session_id,
        owner="alice",
        expected_version=first.version,
        evidence_ref="evidence://runs/reproduce-first/manifest/manifest.json",
        evidence_digest="a" * 64,
        environment="docker",
        release_version="1.0.0",
    )
    submitted = service.submit_template_publication_review(
        session_id=reproduced.session_id,
        owner="alice",
        expected_version=reproduced.version,
        confirmation_digest=str(reproduced.confirmation_digest),
    )
    published = service.approve_and_publish_template(
        session_id=submitted.session_id,
        owner="alice",
        expected_version=submitted.version,
        reviewer=TemplateReviewerPrincipal(
            actor="reviewer",
            roles=frozenset({TemplateReviewerRole.REVIEWER}),
        ),
        release_version="1.0.0",
        request_key="publish-first-equivalent-bundle",
    )
    assert published.release_id is not None

    second_contract = contracts.create(owner="alice", payload=_contract_payload("alice"))
    second_run = runs.create_run(
        run_id="run-bundle-second",
        owner="alice",
        workdir="/public/home/alice/project",
        script="python train.py",
        contract_id=second_contract.contract_id,
    )
    with runs.connect() as connection:
        connection.execute(
            "UPDATE runs SET state = 'SUCCEEDED', terminal_state = 'COMPLETED', "
            "exit_code = '0:0', collection_state = 'succeeded', job_id = '1' "
            "WHERE run_id = ?",
            (second_run.run_id,),
        )

    equivalent = service.start_template_publication(
        owner="alice",
        source_run_id=second_run.run_id,
        request_key="second-equivalent-bundle",
        title="Metadata-only new title",
        description="Metadata-only new description",
        visibility=TemplateVisibility.CAMPUS,
        scope_key=None,
        compatibility={"partitions": ["debug"], "gpu": False},
        publication_metadata=publication_metadata,
        source_evidence_ref="evidence://runs/run-bundle-second/manifest/manifest.json",
        source_evidence_digest="b" * 64,
        environment="docker",
    )

    assert equivalent.state == "completed"
    assert equivalent.release_id == published.release_id
    assert equivalent.verification_id is not None
    assert equivalent.draft_id is None
    releases, _ = templates.list_market_page(actor="alice", limit=20)
    assert [item.release.release_id for item in releases] == [published.release_id]
    verifications = templates.list_verifications(published.release_id)
    assert len(verifications) == 1
    assert verifications[0].run_id == second_run.run_id
    assert verifications[0].detail["verification_kind"] == "equivalent_bundle"


def test_template_publication_http_session_requires_reproduction_then_confirmation(
    tmp_path: Path,
) -> None:
    database = tmp_path / "pilot107.db"
    runs = RunStore(database)
    contracts = ContractService(catalog=RecipeCatalog(), store=ContractStore(database))
    templates = TemplateMarketStore(
        database,
        publication_gate=TemplatePublicationGate(contracts),
        contract_service=contracts,
    )
    service = TemplatePublicationService(
        store=SQLiteMarketSessionStore(database),
        run_store=runs,
        contract_service=contracts,
        run_publications=RunPublicationStore(
            database,
            run_store=runs,
            contract_service=contracts,
        ),
        template_market=templates,
    )
    routes = MarketSessionRoutes(applications=None, publications=service)
    contract = contracts.create(owner="alice", payload=_contract_payload("alice"))
    run = runs.create_run(
        run_id="run-publication-http",
        owner="alice",
        workdir="/public/home/alice/project",
        script="python train.py",
        contract_id=contract.contract_id,
    )
    with runs.connect() as connection:
        connection.execute(
            "UPDATE runs SET state = 'SUCCEEDED', terminal_state = 'COMPLETED', "
            "exit_code = '0:0', collection_state = 'succeeded' WHERE run_id = ?",
            (run.run_id,),
        )
    identity = UserIdentity(username="alice")
    started = routes.handle_post(
        ["runs", run.run_id, "template-publication-sessions"],
        body=json.dumps(
            {
                "request_key": "http-publication",
                "title": "HTTP publication",
                "description": "A session-owned publication",
                "visibility": "campus",
                "compatibility": {"partitions": ["debug"], "gpu": False},
                "publication": {
                    "license": "MIT",
                    "attribution": "Reusable example",
                    "dataset_access": "No external dataset",
                    "risk_statement": "Runs in a bounded allocation",
                },
            }
        ).encode(),
        identity=identity,
    )
    assert started is not None
    assert started.status == 201
    session_id = started.payload["session_id"]

    reproduced = routes.handle_post(
        ["template-publication-sessions", session_id, "responses"],
        body=json.dumps(
            {
                "expected_version": started.payload["version"],
                "evidence_ref": "evidence://runs/reproduce-http/manifest/manifest.json",
                "evidence_digest": "c" * 64,
                "environment": "docker",
                "release_version": "1.0.0",
            }
        ).encode(),
        identity=identity,
    )
    assert reproduced is not None
    assert reproduced.payload["state"] == "awaiting_confirmation"

    submitted = routes.handle_post(
        ["template-publication-sessions", session_id, "confirmation"],
        body=json.dumps(
            {
                "expected_version": reproduced.payload["version"],
                "confirmation_digest": reproduced.payload["confirmation_digest"],
            }
        ).encode(),
        identity=identity,
    )
    assert submitted is not None
    assert submitted.payload["state"] == "submitted"
    assert submitted.payload["review_id"] is not None

    fetched = routes.handle_get(
        ["template-publication-sessions", session_id],
        params={},
        identity=identity,
    )
    hidden = routes.handle_get(
        ["template-publication-sessions", session_id],
        params={},
        identity=UserIdentity(username="bob"),
    )
    assert fetched is not None and fetched.status == 200
    assert hidden is not None and hidden.status == 404


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
