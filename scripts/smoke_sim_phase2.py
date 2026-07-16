from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

from pilot107.adapters.slurm import (
    DockerComposeExecutor,
    DockerComposeTarget,
    DockerSimulatorCommandBackend,
)
from pilot107.core.advice import AgentAdviceService, AgentPolicyEngine
from pilot107.core.agent import AgentExplainService
from pilot107.core.contracts import ContractService, ContractStore, RecipeCatalog
from pilot107.core.evidence_binding import EvidenceBinder
from pilot107.core.resources import ResourcePlan
from pilot107.core.run_service import RunService, RunSubmitRequest, WorkflowPolicy
from pilot107.core.run_store import RunRecord, RunStore
from pilot107.core.states import RunState
from pilot107.worker.evidence import EvidenceStore

_TERMINAL = {RunState.SUCCEEDED, RunState.FAILED, RunState.CANCELLED}


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
    backend = DockerSimulatorCommandBackend(
        executor=executor,
        allowed_roots=["/public/home/alice"],
        timeout_seconds=20.0,
    )
    stamp = str(time.time_ns())
    shared_workdir = f"/public/home/alice/pilot107-phase2-{stamp}"
    kit_root = f"{shared_workdir}/kit"
    setup = executor.run(
        ["mkdir", "-p", shared_workdir, kit_root, f"{shared_workdir}/logs"],
        user="alice",
        timeout_seconds=10.0,
    )
    if setup.returncode != 0:
        return _fail(f"failed to create shared workdir: {setup.stderr}")

    with tempfile.TemporaryDirectory(prefix="pilot107-phase2-") as temp_dir:
        db_path = Path(temp_dir) / "pilot107.db"
        run_store = RunStore(db_path)
        contract_store = ContractStore(db_path)
        contract_service = ContractService(
            catalog=RecipeCatalog(store=contract_store),
            store=contract_store,
        )
        run_service = RunService(store=run_store, backend=backend)

        dependency_error = _verify_real_dependency(
            run_service=run_service,
            shared_workdir=shared_workdir,
        )
        if dependency_error is not None:
            return _fail(dependency_error)

        evidence_store = EvidenceStore(Path(temp_dir) / "evidence")
        explain_service = AgentExplainService(
            store=run_store,
            evidence_binder=EvidenceBinder(
                store=run_store,
                evidence_root=evidence_store.root,
            ),
        )
        advice_service = AgentAdviceService(
            store=run_store,
            explain_service=explain_service,
            policy_engine=AgentPolicyEngine(contract_service=contract_service),
            contract_service=contract_service,
            run_service=run_service,
        )
        agent_error = _verify_agent_remediation(
            run_store=run_store,
            run_service=run_service,
            contract_service=contract_service,
            advice_service=advice_service,
            evidence_store=evidence_store,
            executor=executor,
            shared_workdir=shared_workdir,
            kit_root=kit_root,
        )
        if agent_error is not None:
            return _fail(agent_error)

    print(f"phase2 simulator smoke passed workdir={shared_workdir}")
    return 0


def _verify_real_dependency(
    *,
    run_service: RunService,
    shared_workdir: str,
) -> str | None:
    plan = _resource_plan()
    parent = run_service.submit(
        RunSubmitRequest(
            owner="alice",
            workdir=Path(shared_workdir),
            script=(
                "#!/bin/bash\n"
                "set -Eeuo pipefail\n"
                "sleep 4\n"
                "printf parent-ready > parent.done\n"
            ),
            resource_plan=plan,
        )
    )
    child = run_service.submit(
        RunSubmitRequest(
            owner="alice",
            workdir=Path(shared_workdir),
            script=(
                "#!/bin/bash\n"
                "set -Eeuo pipefail\n"
                "test -s parent.done\n"
                "printf dependency-ok > dependency.done\n"
            ),
            resource_plan=plan,
            workflow=WorkflowPolicy(dependencies=(parent.run_id,)),
        )
    )
    parent = _wait_for_terminal(run_service, parent)
    child = _wait_for_terminal(run_service, child)
    dependency_event = next(
        (
            event
            for event in run_service.store.list_events(child.run_id)
            if event.event_type == "workflow.dependencies_resolved"
        ),
        None,
    )
    argv = child.submit_response.get("argv", [])
    if parent.state != RunState.SUCCEEDED or child.state != RunState.SUCCEEDED:
        return f"real afterok workflow failed: parent={parent.state} child={child.state}"
    if dependency_event is None or dependency_event.payload.get("dependency_job_ids") != [
        parent.job_id
    ]:
        return f"afterok resolution event is incomplete: {dependency_event!r}"
    expected_dependency = f"afterok:{parent.job_id}"
    if "--dependency" not in argv or expected_dependency not in argv:
        return f"sbatch dependency argument is incomplete: {argv!r}"
    return None


def _verify_agent_remediation(
    *,
    run_store: RunStore,
    run_service: RunService,
    contract_service: ContractService,
    advice_service: AgentAdviceService,
    evidence_store: EvidenceStore,
    executor: DockerComposeExecutor,
    shared_workdir: str,
    kit_root: str,
) -> str | None:
    contract = contract_service.create(
        owner="alice",
        payload={
            "recipe_version_id": "recipe_student_cpu_basic@1.0.0",
            "project": {"name": "phase2-agent", "workdir": shared_workdir},
            "entry": {"command": "exit 7"},
            "runtime": {"environment": {"KIT_ROOT": kit_root}},
            "resources": {
                "partition": "Students",
                "qos": "qos_stu_cpu_long",
                "nodes": 1,
                "ntasks": 1,
                "cpus_per_task": 1,
                "time_limit": "00:05:00",
            },
        },
    )
    validation = contract_service.preflight(contract)
    rendered = validation.effective_request.get("script")
    if validation.status != "OK" or not isinstance(rendered, str):
        return f"packaged contract did not materialize: {validation.status}"
    if "#SBATCH --partition=Students" not in rendered or "export KIT_ROOT=" not in rendered:
        return "packaged contract omitted Slurm or runtime directives"

    source = run_service.submit(contract_service.to_submit_request(contract))
    source = _wait_for_terminal(run_service, source)
    if source.state != RunState.FAILED or source.exit_code != "7:0":
        return f"expected source failure was not observed: {source.state} {source.exit_code}"

    artifact = evidence_store.write_text(
        run_id=source.run_id,
        logical_path="logs/stderr.tail.txt",
        content="command exited with status 7\n",
        content_type="text/plain",
    )
    evidence_ref = f"evidence://runs/{source.run_id}/{artifact.logical_path}"
    run_store.upsert_evidence_objects(
        source.run_id,
        [
            {
                "object_id": f"ev_{source.run_id}",
                "category": "logs",
                "logical_path": artifact.logical_path,
                "store_path": str(artifact.path),
                "source_uri": evidence_ref,
                "sha256": artifact.sha256,
                "size_bytes": artifact.size_bytes,
                "mime_type": artifact.content_type,
                "collection_status": "collected",
                "mutable_during_run": False,
            }
        ],
    )
    run_store.replace_diagnoses(
        source.run_id,
        [
            {
                "diagnosis_id": f"diag_{source.run_id}",
                "rule_id": "RUNTIME.NONZERO_EXIT",
                "severity": "error",
                "summary": "entry command failed",
                "evidence_refs": [evidence_ref],
                "suggested_patch": {
                    "entry.command": "printf 'agent-repaired\\n'",
                },
                "retryable": True,
                "confidence": "high",
            }
        ],
    )
    advice = advice_service.advise(source.run_id, idempotency_key="phase2-smoke").record
    actions = advice.payload.get("actions", [])
    if advice.state != "ready" or len(actions) != 1:
        return f"agent did not produce one executable action: {advice.state} {actions!r}"
    action_id = str(actions[0]["action_id"])
    advice_service.approve(
        advice.advice_id,
        expected_version=1,
        action_ids=[action_id],
        actor="alice",
        note="phase2 simulator approval",
    )
    execution = advice_service.execute_action(
        advice.advice_id,
        action_id=action_id,
        actor="alice",
        submit=True,
    )
    if execution.state != "submitted" or execution.run_id is None:
        return f"approved agent action was not submitted: {execution}"
    repaired = _wait_for_terminal(run_service, run_store.get_run(execution.run_id))
    if repaired.state != RunState.SUCCEEDED or repaired.exit_code != "0:0":
        return f"agent remediation did not succeed: {repaired.state} {repaired.exit_code}"
    if repaired.parent_run_id != source.run_id or repaired.lineage_reason != "agent_remediation":
        return "agent remediation run lineage is incomplete"
    derived = contract_service.get(execution.derived_contract_id or "")
    if derived.parent_contract_id != contract.contract_id:
        return "derived contract lineage is incomplete"
    output = executor.run(
        ["cat", f"{shared_workdir}/outputs/result.txt"],
        user="alice",
        timeout_seconds=10.0,
    )
    if output.returncode != 0 or output.stdout.strip() != "agent-repaired":
        return f"agent remediation output is invalid: {output.stdout!r} {output.stderr!r}"
    return None


def _wait_for_terminal(service: RunService, run: RunRecord) -> RunRecord:
    current = run
    last_error: Exception | None = None
    for _ in range(60):
        try:
            current = service.reconcile_once(run.run_id)
        except Exception as exc:  # noqa: BLE001 - smoke reports transient backend failures
            last_error = exc
        if current.state in _TERMINAL:
            return current
        time.sleep(1)
    raise RuntimeError(f"run did not reach terminal state: {run.run_id}; last_error={last_error}")


def _resource_plan() -> ResourcePlan:
    return ResourcePlan(
        partition="Students",
        qos="qos_stu_cpu_long",
        nodes=1,
        ntasks=1,
        cpus_per_task=1,
        time_limit="00:05:00",
    )


def _fail(message: str) -> int:
    print(message, file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
