"""Application service for the WorkArea -> Launch -> Run vertical slice."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from pilot107.core.contracts import ContractService
from pilot107.core.launch import (
    LaunchCandidateRecord,
    LaunchConflict,
    LaunchPreflightRecord,
    LaunchRecord,
    PostgresLaunchStore,
)
from pilot107.core.resources import PreflightFinding
from pilot107.core.run_service import RunService
from pilot107.core.run_store import RunRecord, RunStore
from pilot107.core.states import RunState
from pilot107.core.workarea import PostgresWorkAreaStore
from pilot107.core.workarea_binding_source import PostgresWorkAreaBindingSourceStore


@dataclass(frozen=True)
class LaunchCommitResult:
    launch: LaunchRecord
    run: RunRecord
    submit_error: dict[str, Any] | None


class LaunchService:
    """Coordinates durable review/commit without becoming a submission authority."""

    def __init__(
        self,
        *,
        workareas: PostgresWorkAreaStore,
        launches: PostgresLaunchStore,
        contracts: ContractService,
        run_service: RunService,
        run_store: RunStore,
        binding_sources: PostgresWorkAreaBindingSourceStore | None = None,
    ) -> None:
        self.workareas = workareas
        self.launches = launches
        self.contracts = contracts
        self.run_service = run_service
        self.run_store = run_store
        self.binding_sources = binding_sources

    def create_candidate(
        self,
        *,
        workarea_id: str,
        owner: str,
        contract_id: str,
        request_key: str,
        title: str = "",
        note: str = "",
    ) -> LaunchCandidateRecord:
        self.workareas.get(workarea_id, owner=owner)
        contract = self.contracts.get(contract_id)
        if contract.owner != owner:
            raise PermissionError("Contract owner does not match WorkArea owner")
        # The Contract becomes visible at WorkArea level because a Launch uses
        # it. That WorkArea edge is inherited unless the user explicitly bound
        # the same Contract earlier; user provenance always has precedence.
        self.workareas.link_contract(workarea_id, owner=owner, contract_id=contract_id)
        if self.binding_sources is not None:
            self.binding_sources.mark(
                workarea_id=workarea_id,
                binding_kind="contract",
                target_ref=contract_id,
                source="inherited",
            )
        return self.launches.create_candidate(
            workarea_id=workarea_id,
            owner=owner,
            request_key=request_key,
            contract_id=contract_id,
            title=title,
            note=note,
        )

    def assess(
        self,
        candidate_id: str,
        *,
        owner: str,
    ) -> LaunchPreflightRecord:
        candidate = self.launches.get_candidate(candidate_id, owner=owner)
        self.workareas.get(candidate.workarea_id, owner=owner)
        contract = self.contracts.get(candidate.contract_id)
        if contract.owner != owner:
            raise PermissionError("Contract owner changed outside WorkArea boundary")

        validation = self.contracts.preflight(contract)
        effective = dict(validation.effective_request)
        effective.update(
            {
                "owner": owner,
                "contract_id": contract.contract_id,
                "workarea_id": candidate.workarea_id,
                "candidate_id": candidate.candidate_id,
                "platform_snapshot": validation.platform_snapshot,
                "risk_lint": validation.risk_lint,
            }
        )
        # On a passing assessment, serialize the exact RunSubmitRequest fields
        # that Commit will feed to RunService. This is the reviewable Slurm
        # request boundary required by the product design.
        if validation.status == "OK":
            submit_request = self.contracts.to_submit_request(contract)
            effective["run_submit_request"] = _submit_request_payload(submit_request)

        return self.launches.save_preflight(
            candidate=candidate,
            status=validation.status,
            findings=[_finding_payload(item) for item in validation.findings],
            effective_request=effective,
        )

    def commit(
        self,
        candidate_id: str,
        *,
        owner: str,
        expected_preflight_digest: str,
        request_key: str,
    ) -> LaunchCommitResult:
        candidate = self.launches.get_candidate(candidate_id, owner=owner)
        previous = self.launches.latest_preflight(candidate_id, owner=owner)
        if previous is None:
            raise LaunchConflict("LaunchCandidate has not been preflighted")
        if previous.assessment_digest != expected_preflight_digest:
            raise LaunchConflict("reviewed preflight is no longer the latest assessment")
        if previous.status != "OK":
            raise LaunchConflict("blocked preflight cannot be committed")

        # Re-evaluate immediately before Commit. Snapshot/freshness changes are
        # observable as PREFLIGHT_STALE instead of silently submitting a request
        # different from the one the user reviewed.
        current = self.assess(candidate_id, owner=owner)
        if current.assessment_digest != expected_preflight_digest:
            raise LaunchConflict("preflight became stale; review the effective request again")
        if current.status != "OK":
            raise LaunchConflict("preflight is now blocked")

        contract = self.contracts.get(candidate.contract_id)
        submit_request = self.contracts.to_submit_request(contract)
        run_id = _run_id(candidate, current)
        run = self.run_service.prepare(submit_request, run_id=run_id, idempotent=True)

        launch = self.launches.commit(
            candidate=candidate,
            preflight=current,
            request_key=request_key,
        )
        self.launches.attach_run(launch.launch_id, owner=owner, run_id=run.run_id, ordinal=0)
        self.workareas.link_run(candidate.workarea_id, owner=owner, run_id=run.run_id)
        if self.binding_sources is not None:
            self.binding_sources.mark(
                workarea_id=candidate.workarea_id,
                binding_kind="run",
                target_ref=run.run_id,
                source="inherited",
            )

        # A retried HTTP Commit must not duplicate submission. If a prior call
        # already advanced the Run, return the durable Launch/Run view.
        run = self.run_store.get_run(run.run_id)
        if run.state != RunState.VALIDATED:
            current_launch = self.launches.get(launch.launch_id, owner=owner)
            return LaunchCommitResult(
                launch=current_launch,
                run=run,
                submit_error=current_launch.submit_error,
            )

        try:
            submitted = self.run_service.submit_prepared(run.run_id)
        except Exception as exc:  # noqa: BLE001 - preserve durable Launch on submit failure
            failed = self.run_store.get_run(run.run_id)
            error = {
                "type": type(exc).__name__,
                "message": str(exc),
                "run_state": failed.state.value,
            }
            self.launches.mark_submit_error(launch.launch_id, owner=owner, error=error)
            return LaunchCommitResult(
                launch=self.launches.get(launch.launch_id, owner=owner),
                run=failed,
                submit_error=error,
            )

        self.launches.mark_submitted(launch.launch_id, owner=owner)
        return LaunchCommitResult(
            launch=self.launches.get(launch.launch_id, owner=owner),
            run=submitted,
            submit_error=None,
        )


def _run_id(candidate: LaunchCandidateRecord, preflight: LaunchPreflightRecord) -> str:
    token = hashlib.sha256(
        f"{candidate.owner}\0{candidate.candidate_id}\0{preflight.assessment_digest}".encode()
    ).hexdigest()[:24]
    return f"run_launch_{token}"


def _finding_payload(finding: PreflightFinding) -> dict[str, Any]:
    return {
        "severity": finding.severity.value,
        "code": finding.code,
        "message": finding.message,
        "source_authority": finding.source_authority,
    }


def _submit_request_payload(request: Any) -> dict[str, Any]:
    plan = request.resource_plan
    array = plan.array
    return {
        "owner": request.owner,
        "workdir": str(request.workdir),
        "script": request.script,
        "job_name": request.job_name,
        "contract_id": request.contract_id,
        "parent_run_id": request.parent_run_id,
        "lineage_reason": request.lineage_reason,
        "remediation_plan_id": request.remediation_plan_id,
        "workflow": request.workflow.to_payload(),
        "resource_plan": {
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
            "array": None
            if array is None
            else {
                "expression": array.expression,
                "max_concurrency": array.max_concurrency,
            },
        },
    }


__all__ = ["LaunchCommitResult", "LaunchService"]
