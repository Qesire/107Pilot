"""D1 two-owner smoke for Agent-mediated market lifecycles."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from uuid import uuid4

from smoke_pilot_agent_a4_live import _run_checked, _wait_run

from pilot107.adapters.slurm import (
    DockerComposeExecutor,
    DockerComposeTarget,
    DockerSimulatorCommandBackend,
)
from pilot107.agent.market_sessions import (
    MarketApplicationError,
    MarketApplicationService,
    MarketAssurance,
    SQLiteMarketSessionStore,
    TemplatePublicationService,
)
from pilot107.agent.project import ExperimentProjectOrigin
from pilot107.agent.project_store import SQLiteProjectStore
from pilot107.agent.sandbox import SandboxExecutor
from pilot107.core.contracts import ContractService, ContractStore, RecipeCatalog
from pilot107.core.control_repository import SQLiteControlRepository
from pilot107.core.run_publications import (
    RunPublicationShareManifest,
    RunPublicationStore,
    RunPublicationVisibility,
)
from pilot107.core.run_service import RunService
from pilot107.core.run_store import RunRecord, RunStore
from pilot107.core.states import RunState
from pilot107.core.template_market import TemplateMarketStore, TemplateVisibility
from pilot107.core.template_policy import (
    TemplatePublicationGate,
    TemplateReviewerPrincipal,
    TemplateReviewerRole,
)
from pilot107.services.project_agent_service import ProjectAgentService
from pilot107.worker.evidence import EvidenceStore

PUBLICATION_METADATA: dict[str, object] = {
    "license": "MIT",
    "attribution": "D1 reusable training example",
    "dataset_access": "No external dataset",
    "risk_statement": "Runs in a bounded Slurm allocation",
}


def _contract_payload(workdir: str, *, cpus: int = 1) -> dict[str, object]:
    return {
        "recipe_version_id": "recipe_python_cpu@1.0.0",
        "project": {"name": "agent-market-d1", "workdir": workdir},
        "entry": {
            "command": "printf 'market-d1-success\\n' | tee result.txt",
            "expected_outputs": ["result.txt"],
        },
        "resources": {
            "partition": "Students",
            "qos": "qos_stu_default",
            "nodes": 1,
            "ntasks": 1,
            "cpus_per_task": cpus,
            "memory": "128M",
            "gpus_total": 0,
            "gpu_type": None,
            "time_limit": "00:02:00",
        },
        "extensions": {
            "source_owner_note": "prepared by alice",
        },
    }


def _submit_success(
    *,
    contracts: ContractService,
    runs: RunService,
    workdir: str,
    cpus: int = 1,
) -> RunRecord:
    contract = contracts.create(
        owner="alice",
        payload=_contract_payload(workdir, cpus=cpus),
    )
    run = runs.submit(contracts.to_submit_request(contract))
    terminal = _wait_run(runs, run.run_id)
    if terminal.state is not RunState.SUCCEEDED or not (terminal.exit_code or "").startswith("0:"):
        raise RuntimeError(f"market D1 source Run failed: {terminal.state.value}")
    return terminal


def _evidence(
    evidence: EvidenceStore,
    *,
    run: RunRecord,
    name: str,
) -> tuple[str, str]:
    artifact = evidence.write_json(
        run_id=run.run_id,
        logical_path=f"market/{name}.json",
        payload={
            "run_id": run.run_id,
            "job_id": run.job_id,
            "scheduler_state": run.state.value,
            "exit_code": run.exit_code,
            "scientific_validity": "not_assessed",
        },
    )
    return (
        f"evidence://runs/{run.run_id}/{artifact.logical_path}",
        artifact.sha256,
    )


def _publish_template(
    service: TemplatePublicationService,
    *,
    run: RunRecord,
    evidence: tuple[str, str],
    request_key: str,
    release_version: str,
    reviewer: TemplateReviewerPrincipal,
    base_release_id: str | None = None,
) -> tuple[str, str, str]:
    started = service.start_template_publication(
        owner="alice",
        source_run_id=run.run_id,
        request_key=request_key,
        title="D1 reviewed training template",
        description=f"D1 immutable release {release_version}",
        visibility=TemplateVisibility.CAMPUS,
        scope_key=None,
        compatibility={"partitions": ["Students"], "gpu": False},
        publication_metadata=PUBLICATION_METADATA,
        base_release_id=base_release_id,
    )
    reproduced = service.record_template_reproduction(
        session_id=started.session_id,
        owner="alice",
        expected_version=started.version,
        evidence_ref=evidence[0],
        evidence_digest=evidence[1],
        environment="docker",
        release_version=release_version,
    )
    submitted = service.submit_template_publication_review(
        session_id=started.session_id,
        owner="alice",
        expected_version=reproduced.version,
        confirmation_digest=str(reproduced.confirmation_digest),
    )
    completed = service.approve_and_publish_template(
        session_id=started.session_id,
        owner="alice",
        expected_version=submitted.version,
        reviewer=reviewer,
        release_version=release_version,
        request_key=f"publish:{request_key}",
    )
    if completed.release_id is None:
        raise RuntimeError("template publication did not create a release")
    return completed.release_id, started.bundle_digest, completed.session_id


def main() -> int:
    repository = Path(__file__).resolve().parents[1]
    compose_dir = repository / "simulator" / "compose"
    executor = DockerComposeExecutor(
        DockerComposeTarget(
            compose_file=compose_dir / "compose.yml",
            env_file=compose_dir / ".env.example",
            workdir=compose_dir,
        )
    )
    backend = DockerSimulatorCommandBackend(
        executor=executor,
        allowed_roots=["/public/home/alice"],
        timeout_seconds=20,
    )
    suffix = uuid4().hex[:10]
    workdir = f"/public/home/alice/pilot107-market-d1-{suffix}"
    _run_checked(executor, ["mkdir", "-p", workdir], user="alice")
    try:
        with tempfile.TemporaryDirectory(prefix="pilot107-market-d1-") as temporary:
            root = Path(temporary)
            database = root / "pilot107.db"
            contract_store = ContractStore(database)
            contracts = ContractService(
                catalog=RecipeCatalog(
                    store=contract_store,
                    partition_qos={"Students": ("qos_stu_default",)},
                    default_partition="Students",
                    default_qos="qos_stu_default",
                ),
                store=contract_store,
                partition_qos={"Students": ("qos_stu_default",)},
            )
            run_store = RunStore(database)
            run_service = RunService(
                store=run_store,
                backend=backend,
                control_repository=SQLiteControlRepository(database),
                dispatcher_id="market-d1-run",
                submission_retry_delay_seconds=0,
            )
            publications = RunPublicationStore(
                database,
                run_store=run_store,
                contract_service=contracts,
            )
            templates = TemplateMarketStore(
                database,
                publication_gate=TemplatePublicationGate(contracts),
                contract_service=contracts,
            )
            sessions = SQLiteMarketSessionStore(database)
            project_store = SQLiteProjectStore(database)
            projects = ProjectAgentService(
                store=project_store,
                workspace_root=root / "agent-workspaces",
                sandbox=SandboxExecutor(store=project_store),
                contract_service=contracts,
            )
            applications = MarketApplicationService(
                store=sessions,
                contract_service=contracts,
                run_publications=publications,
                template_market=templates,
                project_service=projects,
            )
            template_publications = TemplatePublicationService(
                store=sessions,
                run_store=run_store,
                contract_service=contracts,
                run_publications=publications,
                template_market=templates,
            )
            evidence_store = EvidenceStore(root / "evidence")
            reviewer = TemplateReviewerPrincipal(
                actor="reviewer",
                roles=frozenset({TemplateReviewerRole.REVIEWER}),
            )

            first_run = _submit_success(
                contracts=contracts,
                runs=run_service,
                workdir=workdir,
            )
            template_publications.observe_successful_run(first_run)
            if (
                publications.get_for_source_run(
                    source_run_id=first_run.run_id,
                    owner="alice",
                )
                is not None
            ):
                raise RuntimeError("successful Run was shared without a ShareManifest")

            first_evidence = _evidence(
                evidence_store,
                run=first_run,
                name="reproduction-v1",
            )
            release_v1_id, bundle_v1, _ = _publish_template(
                template_publications,
                run=first_run,
                evidence=first_evidence,
                request_key="d1-template-v1",
                release_version="1.0.0",
                reviewer=reviewer,
            )
            release_v1 = templates.get_release(release_v1_id)
            if "alice" in json.dumps(release_v1.payload, sort_keys=True):
                raise RuntimeError("sanitized release retained the source username")

            equivalent_run = _submit_success(
                contracts=contracts,
                runs=run_service,
                workdir=workdir,
            )
            equivalent_evidence = _evidence(
                evidence_store,
                run=equivalent_run,
                name="equivalent-verification",
            )
            equivalent = template_publications.start_template_publication(
                owner="alice",
                source_run_id=equivalent_run.run_id,
                request_key="d1-equivalent-bundle",
                title="Metadata-only alternate title",
                description="Equivalent semantic bundle",
                visibility=TemplateVisibility.CAMPUS,
                scope_key=None,
                compatibility={"partitions": ["Students"], "gpu": False},
                publication_metadata=PUBLICATION_METADATA,
                source_evidence_ref=equivalent_evidence[0],
                source_evidence_digest=equivalent_evidence[1],
                environment="docker",
            )
            if (
                equivalent.release_id != release_v1_id
                or equivalent.bundle_digest != bundle_v1
                or equivalent.verification_id is None
            ):
                raise RuntimeError("equivalent bundle created a duplicate release")

            revised_run = _submit_success(
                contracts=contracts,
                runs=run_service,
                workdir=workdir,
                cpus=2,
            )
            release_v2_id, _, _ = _publish_template(
                template_publications,
                run=revised_run,
                evidence=_evidence(evidence_store, run=revised_run, name="reproduction-v2"),
                request_key="d1-template-v2",
                release_version="1.1.0",
                reviewer=reviewer,
                base_release_id=release_v1_id,
            )
            release_v2 = templates.get_release(release_v2_id)
            if release_v2.template_id != release_v1.template_id:
                raise RuntimeError("new release version escaped its template family")

            curated = applications.start_template_application(
                owner="bob",
                release_id=release_v2_id,
                user_intent="apply the reviewed release in Bob's private project",
                request_key="bob-curated-d1",
            )
            if curated.application.assurance is not MarketAssurance.CURATED:
                raise RuntimeError("curated assurance was weakened")
            project = project_store.get_project(
                str(curated.application.project_id),
                owner="bob",
            )
            if project.origin is not ExperimentProjectOrigin.TEMPLATE:
                raise RuntimeError("curated application did not create a template Project")
            curated_completed = applications.finalize_template_application(
                session_id=curated.application.session_id,
                owner="bob",
                expected_version=curated.application.version,
                confirmation_digest=curated.confirmation_digest,
                request_key="bob-curated-contract-d1",
            )
            curated_contract = contracts.get(str(curated_completed.application.target_contract_id))
            curated_lineage = curated_contract.field_sources[0]
            curated_adoption = templates.get_adoption_for_contract(
                release_id=release_v2_id,
                adopter="bob",
                contract_id=curated_contract.contract_id,
            )
            if (
                curated_contract.owner != "bob"
                or curated_contract.derivation_reason != "template_application"
                or curated_lineage.get("market_application_session_id")
                != curated.application.session_id
                or curated_lineage.get("assurance") != "curated"
                or curated_adoption.adoption_id != curated_completed.application.adoption_id
            ):
                raise RuntimeError("curated application isolation or lineage failed")

            manifest = RunPublicationShareManifest(
                title="D1 successful Run reference",
                visibility=RunPublicationVisibility.CAMPUS,
                description=True,
                result_summary=True,
                contract_for_adaptation=True,
            )
            reference_publication = template_publications.publish_run_reference(
                source_run_id=first_run.run_id,
                owner="alice",
                request_key="alice-reference-d1",
                manifest=manifest,
                description="Reference-only; portability remains unverified",
            )
            reference = applications.start_reference_adaptation(
                owner="bob",
                publication_id=reference_publication.publication_id,
                user_intent="adapt the explicitly shared reference Contract",
                request_key="bob-reference-d1",
            )
            reference_completed = applications.finalize_reference_adaptation(
                session_id=reference.application.session_id,
                owner="bob",
                expected_version=reference.application.version,
                confirmation_digest=reference.confirmation_digest,
                request_key="bob-reference-contract-d1",
            )
            reference_contract = contracts.get(
                str(reference_completed.application.target_contract_id)
            )
            if (
                reference.application.assurance is not MarketAssurance.REFERENCE_ONLY
                or reference_contract.owner != "bob"
                or reference_contract.derivation_reason != "run_publication_adaptation"
            ):
                raise RuntimeError("reference-only assurance or lineage failed")

            templates.withdraw_release(
                release_v1_id,
                actor="alice",
                reason="superseded by reviewed 1.1.0",
            )
            try:
                applications.start_template_application(
                    owner="carol",
                    release_id=release_v1_id,
                    user_intent="must not apply a withdrawn release",
                    request_key="carol-withdrawn-d1",
                )
            except MarketApplicationError as exc:
                if exc.code != "MARKET.SOURCE_WITHDRAWN":
                    raise
            else:
                raise RuntimeError("withdrawn release remained applicable")

            releases, _ = templates.list_market_page(actor="bob", limit=20)
            if [item.release.release_id for item in releases] != [release_v2_id]:
                raise RuntimeError("withdrawal/version market visibility is incorrect")
            if len(templates.list_verifications(release_v1_id)) != 1:
                raise RuntimeError("equivalent-bundle verification feedback is missing")

            print(
                json.dumps(
                    {
                        "status": "ok",
                        "source_run_id": first_run.run_id,
                        "release_v1_id": release_v1_id,
                        "release_v2_id": release_v2_id,
                        "equivalent_verification_id": equivalent.verification_id,
                        "bob_curated_contract_id": curated_contract.contract_id,
                        "bob_reference_contract_id": reference_contract.contract_id,
                        "share_manifest_digest": manifest.manifest_digest,
                        "default_private": True,
                        "withdrawal_enforced": True,
                    },
                    sort_keys=True,
                )
            )
    finally:
        executor.run(
            ["python3", "-c", "import shutil,sys; shutil.rmtree(sys.argv[1])", workdir],
            user="alice",
            timeout_seconds=20,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
