from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from pilot107.adapters.slurm import (
    DockerComposeExecutor,
    DockerComposeTarget,
    DockerSimulatorCommandBackend,
)
from pilot107.api.evidence_query import EvidenceQueryService
from pilot107.api.http_app import ApiResponse, Pilot107HttpApi
from pilot107.core.contracts import ContractService, ContractStore, RecipeCatalog
from pilot107.core.control_repository import SQLiteControlRepository
from pilot107.core.postgres_control_repository import PostgresControlRepository
from pilot107.core.postgres_domain_schema import persisted_table_names
from pilot107.core.postgres_domain_stores import (
    PostgresContractStore,
    PostgresRemediationStore,
    PostgresRunStore,
    PostgresTemplateMarketStore,
)
from pilot107.core.remediation_store import RemediationStore
from pilot107.core.run_service import RunService
from pilot107.core.run_store import RunRecord, RunStore
from pilot107.core.states import CapsuleState, CollectionState, RunState
from pilot107.core.template_market import TemplateMarketStore
from pilot107.core.template_policy import (
    TemplatePublicationGate,
    TemplateRoleDirectory,
)
from pilot107.core.template_verification import TemplateVerificationService
from pilot107.worker.capsule import (
    CapsuleVerifyResult,
    RawCapsuleService,
    verify_raw_capsule,
)
from pilot107.worker.evidence import DockerSlurmEvidenceCollector, EvidenceStore
from pilot107.worker.runtime_worker import RuntimeReconcileWorker


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    compose_dir = root / "simulator" / "compose"
    executor = DockerComposeExecutor(
        DockerComposeTarget(
            compose_file=compose_dir / "compose.yml",
            env_file=compose_dir / ".env.example",
            workdir=compose_dir,
        )
    )
    stamp = str(time.time_ns())
    workdir = f"/public/home/bob/pilot107-phase3c-{stamp}"
    setup = executor.run(["mkdir", "-p", workdir], user="bob", timeout_seconds=10.0)
    if setup.returncode != 0:
        raise RuntimeError(f"failed to create Phase 3C workdir: {setup.stderr}")

    with tempfile.TemporaryDirectory(prefix="pilot107-phase3c-") as temp_dir:
        runtime_root = Path(temp_dir)
        db_path = runtime_root / "pilot107.db"
        evidence_store = EvidenceStore(runtime_root / "evidence")
        postgres_dsn = _test_postgres_dsn()
        control_repository: SQLiteControlRepository | PostgresControlRepository
        if postgres_dsn is None:
            run_store = RunStore(db_path)
            contract_store = ContractStore(db_path)
            remediation_store = RemediationStore(db_path)
            control_repository = SQLiteControlRepository(db_path)
            storage_label = "sqlite"
        else:
            # This smoke intentionally uses an explicitly disposable database.
            # It must never silently clear an operator's production PostgreSQL.
            run_store = PostgresRunStore(postgres_dsn, compatibility_path=db_path)
            contract_store = PostgresContractStore(postgres_dsn, compatibility_path=db_path)
            remediation_store = PostgresRemediationStore(
                postgres_dsn,
                compatibility_path=db_path,
            )
            control_repository = PostgresControlRepository(postgres_dsn)
            _reset_test_postgres(run_store)
            storage_label = "postgres"
        catalog = RecipeCatalog(store=contract_store)
        contract_service = ContractService(catalog=catalog, store=contract_store)
        publication_gate = TemplatePublicationGate(contract_service)
        if postgres_dsn is None:
            template_store = TemplateMarketStore(
                db_path,
                publication_gate=publication_gate,
                contract_service=contract_service,
            )
        else:
            template_store = PostgresTemplateMarketStore(
                postgres_dsn,
                compatibility_path=db_path,
                publication_gate=publication_gate,
                contract_service=contract_service,
            )
        backend = DockerSimulatorCommandBackend(
            executor=executor,
            allowed_roots=["/public/home/alice", "/public/home/bob"],
            timeout_seconds=20.0,
        )
        run_service = RunService(
            store=run_store,
            backend=backend,
            control_repository=control_repository,
            dispatcher_id="smoke-phase3c-dispatcher",
            contract_store=contract_store,
            evidence_store=evidence_store,
        )
        collector = DockerSlurmEvidenceCollector(
            store=evidence_store,
            executor=executor,
            allowed_roots=["/public/home/alice", "/public/home/bob"],
            run_store=run_store,
            timeout_seconds=20.0,
            contract_store=contract_store,
        )
        worker = RuntimeReconcileWorker(
            service=run_service,
            batch_size=20,
            task_handler=collector,
            worker_id="smoke-phase3c-worker",
        )
        capsule_service = RawCapsuleService(
            store=run_store,
            evidence_store=evidence_store,
            capsule_root=runtime_root / "capsules",
        )
        api = Pilot107HttpApi(
            store=run_store,
            control_repository=control_repository,
            evidence_query=EvidenceQueryService(
                store=run_store,
                evidence_store=evidence_store,
            ),
            run_service=run_service,
            recipe_catalog=catalog,
            contract_service=contract_service,
            contract_store=contract_store,
            remediation_store=remediation_store,
            evidence_store=evidence_store,
            capsule_service=capsule_service,
            template_market_store=template_store,
            template_role_directory=TemplateRoleDirectory(reviewers=frozenset({"reviewer"})),
            template_verification_service=TemplateVerificationService(
                template_store=template_store,
                run_store=run_store,
                environment="docker",
                capsule_root=runtime_root / "capsules",
                evidence_root=evidence_store.root,
            ),
            auth_required=True,
        )

        created = _post(
            api,
            "/api/v1/template-drafts",
            _template_payload(workdir),
            actor="alice",
            expected=201,
        )
        draft_id = str(created.payload["draft_id"])
        reviewed = _post(
            api,
            f"/api/v1/template-drafts/{draft_id}/reviews",
            {"expected_version": 1},
            actor="alice",
            expected=201,
        )
        review_id = str(reviewed.payload["review_id"])
        _post(
            api,
            f"/api/v1/template-reviews/{review_id}/decision",
            {"expected_version": 1, "approve": True, "note": "docker smoke"},
            actor="reviewer",
            expected=200,
        )
        published = _post(
            api,
            f"/api/v1/template-drafts/{draft_id}/publish",
            {
                "review_id": review_id,
                "release_version": "1.0.0",
                "request_key": f"publish-{stamp}",
            },
            actor="alice",
            expected=201,
        )
        template_id = str(published.payload["template_id"])
        market = api.handle_get(
            "/api/v1/templates?q=Phase%203C&partition=Students&gpu=false",
            headers=_headers("bob"),
        )
        _require_status(market, 200)
        if [item["release_id"] for item in market.payload["items"]] != [
            published.payload["release_id"]
        ]:
            raise RuntimeError(f"market search did not return release: {market.payload!r}")

        adopted = _post(
            api,
            f"/api/v1/templates/{template_id}/releases/1.0.0/adopt",
            {"request_key": f"adopt-{stamp}"},
            actor="bob",
            expected=201,
        )
        adopted_draft_id = str(adopted.payload["target_draft_id"])
        adopted_contract_id = str(adopted.payload["target_contract_id"])
        updated = api.handle_patch(
            f"/api/v1/template-drafts/{adopted_draft_id}",
            body=_json({"expected_version": 1, "description": "student-owned copy"}),
            headers=_headers("bob"),
        )
        _require_status(updated, 200)
        raw_sbatch = updated.payload["payload"]["extensions"]["advanced"]["raw_sbatch"]
        if raw_sbatch != "#SBATCH --exclusive":
            raise RuntimeError("advanced template field was lost during adoption")

        preflight = _post(
            api,
            f"/api/v1/contracts/{adopted_contract_id}/preflight",
            {},
            actor="bob",
            expected=200,
        )
        if preflight.payload["status"] != "OK":
            raise RuntimeError(f"adopted Contract preflight blocked: {preflight.payload!r}")
        prepared = _post(
            api,
            "/api/v1/runs/prepare",
            {"contract_id": adopted_contract_id},
            actor="bob",
            expected=201,
        )
        run_id = str(prepared.payload["run_id"])
        _post(
            api,
            f"/api/v1/runs/{run_id}/submit",
            {},
            actor="bob",
            expected=200,
        )
        final_run = _wait_for_evidence(worker, run_store, run_id)
        if final_run.state != RunState.SUCCEEDED or final_run.exit_code != "0:0":
            raise RuntimeError(
                f"adopted Contract Run failed: {final_run.state} {final_run.exit_code}"
            )

        capsule = _post(
            api,
            f"/api/v1/runs/{run_id}/capsule",
            {},
            actor="bob",
            expected=200,
        )
        capsule_dir = runtime_root / "capsules" / "runs" / run_id / "raw"
        capsule_check = verify_published_capsule(
            capsule_dir,
            run_store=run_store,
            evidence_store=evidence_store,
            run_id=run_id,
        )
        if not capsule_check.valid:
            raise RuntimeError(f"raw Capsule verification failed: {capsule_check.errors}")
        if run_store.get_run(run_id).capsule_state != CapsuleState.READY:
            raise RuntimeError("raw Capsule did not reach ready state")

        verification = _post(
            api,
            f"/api/v1/templates/{template_id}/releases/1.0.0/verify",
            {"run_id": run_id, "request_key": f"verify-{stamp}"},
            actor="bob",
            expected=201,
        )
        if verification.payload["status"] != "passed":
            raise RuntimeError(f"verification did not pass: {verification.payload!r}")
        verified_market = api.handle_get(
            "/api/v1/templates?verified=true&verification_environment=docker",
            headers=_headers("bob"),
        )
        _require_status(verified_market, 200)
        metrics = verified_market.payload["items"][0]["metrics"]
        if metrics["adoption_count"] != 1 or metrics["verification_passed"] != 1:
            raise RuntimeError(f"market metrics are incorrect: {metrics!r}")

    print(
        f"phase3c template smoke passed storage={storage_label} "
        f"template={template_id} contract={adopted_contract_id} run={run_id} "
        f"capsule={capsule.payload['capsule']['capsule_id']} verification="
        f"{verification.payload['verification_id']}"
    )
    return 0


def verify_published_capsule(
    capsule_dir: Path,
    *,
    run_store: RunStore,
    evidence_store: EvidenceStore,
    run_id: str,
) -> CapsuleVerifyResult:
    """Verify a smoke Capsule against its live server-side Evidence authority."""

    return verify_raw_capsule(
        capsule_dir,
        store=run_store,
        evidence_root=evidence_store.root,
        run_id=run_id,
    )


def _test_postgres_dsn() -> str | None:
    """Return only an explicitly resettable, dedicated PostgreSQL smoke DSN."""

    dsn = os.environ.get("PILOT107_TEST_POSTGRES_DSN")
    if dsn is None:
        return None
    if os.environ.get("PILOT107_TEST_POSTGRES_ALLOW_RESET") != "1":
        raise RuntimeError(
            "PILOT107_TEST_POSTGRES_ALLOW_RESET=1 is required before the "
            "Phase 3C smoke may truncate its dedicated PostgreSQL database"
        )
    return dsn


def _reset_test_postgres(store: PostgresRunStore) -> None:
    """Clear the test-only PG database after native migrations are present."""

    with store.connect() as conn:
        conn.execute(
            "TRUNCATE "
            + ", ".join(reversed(persisted_table_names()))
            + " RESTART IDENTITY CASCADE"
        )


def _wait_for_evidence(
    worker: RuntimeReconcileWorker,
    store: RunStore,
    run_id: str,
) -> RunRecord:
    current = store.get_run(run_id)
    task_errors: list[str] = []
    for _ in range(60):
        result = worker.tick()
        task_errors = [error.message for error in result.task_errors]
        current = store.get_run(run_id)
        if (
            current.state in {RunState.SUCCEEDED, RunState.FAILED, RunState.CANCELLED}
            and current.collection_state == CollectionState.SUCCEEDED
        ):
            return current
        time.sleep(1)
    raise RuntimeError(
        f"Run Evidence did not complete: run={current!r} task_errors={task_errors!r}"
    )


def _post(
    api: Pilot107HttpApi,
    path: str,
    payload: dict[str, Any],
    *,
    actor: str,
    expected: int,
) -> ApiResponse:
    response = api.handle_post(
        path,
        body=_json(payload),
        headers=_headers(actor),
    )
    _require_status(response, expected)
    return response


def _require_status(response: ApiResponse, expected: int) -> None:
    if response.status != expected:
        raise RuntimeError(
            f"unexpected API response: expected={expected} "
            f"actual={response.status} payload={response.payload!r}"
        )


def _headers(actor: str) -> dict[str, str]:
    return {"X-Pilot107-User": actor}


def _json(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload).encode()


def _template_payload(workdir: str) -> dict[str, Any]:
    return {
        "title": "Phase 3C Docker CPU template",
        "description": "Published and adopted in a live Docker Slurm smoke",
        "visibility": "public",
        "payload": {
            "recipe_version_id": "recipe_python_cpu@1.0.0",
            "project": {"name": "phase3c-live", "workdir": workdir},
            "entry": {
                "command": (
                    "mkdir -p phase3c-output && "
                    "printf 'phase3c-template-ok\\n' > phase3c-output/result.txt"
                )
            },
            "resources": {
                "partition": "Students",
                "qos": "qos_stu_default",
                "nodes": 1,
                "ntasks": 1,
                "cpus_per_task": 1,
                "time_limit": "00:05:00",
            },
            "outputs": {
                "expected": ["phase3c-output/result.txt"],
                "success_conditions": ["slurm_exit_code_zero"],
            },
            "extensions": {"advanced": {"raw_sbatch": "#SBATCH --exclusive"}},
        },
        "compatibility": {"partitions": ["Students"], "gpu": False},
        "publication": {
            "license": "MIT",
            "attribution": "107Pilot Phase 3C live smoke",
            "dataset_access": "No external dataset",
            "risk_statement": "Writes one text file inside the adopter workdir",
        },
    }


if __name__ == "__main__":
    raise SystemExit(main())
