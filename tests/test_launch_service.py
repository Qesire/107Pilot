from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from pilot107.core.contracts import ContractRecord, ContractValidationResult
from pilot107.core.launch import (
    LaunchCandidateRecord,
    LaunchConflict,
    LaunchPreflightRecord,
    LaunchRecord,
)
from pilot107.core.resources import PreflightFinding, PreflightSeverity, ResourcePlan
from pilot107.core.run_service import RunSubmitRequest
from pilot107.core.run_store import RunRecord
from pilot107.core.states import (
    CapsuleState,
    CollectionState,
    DiagnosisState,
    ResultStatus,
    RunState,
)
from pilot107.services.launch_service import LaunchService

OWNER = "alice"
WORKAREA_ID = "workarea-test"
CONTRACT_ID = "contract-test"
CANDIDATE_ID = "launchcand-test"


def _contract() -> ContractRecord:
    return ContractRecord(
        contract_id=CONTRACT_ID,
        owner=OWNER,
        recipe_version_id="recipe_python_cpu@1.0.0",
        payload={},
        field_sources=[],
        created_at="2026-09-06T00:00:00Z",
        updated_at="2026-09-06T00:00:00Z",
    )


def _candidate() -> LaunchCandidateRecord:
    return LaunchCandidateRecord(
        candidate_id=CANDIDATE_ID,
        workarea_id=WORKAREA_ID,
        owner=OWNER,
        request_key="candidate-request",
        contract_id=CONTRACT_ID,
        title="test launch",
        note="",
        candidate_digest="candidate-digest",
        created_at="2026-09-06T00:00:00Z",
        updated_at="2026-09-06T00:00:00Z",
    )


def _preflight(*, digest: str = "preflight-stable", status: str = "OK") -> LaunchPreflightRecord:
    return LaunchPreflightRecord(
        preflight_id=f"preflight-{digest}",
        candidate_id=CANDIDATE_ID,
        owner=OWNER,
        candidate_digest="candidate-digest",
        status=status,
        findings=(),
        effective_request={"assessment": digest},
        assessment_digest=digest,
        created_at="2026-09-06T00:00:01Z",
    )


def _run(run_id: str, state: RunState) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        owner=OWNER,
        state=state,
        collection_state=CollectionState.PENDING,
        diagnosis_state=DiagnosisState.PENDING,
        capsule_state=CapsuleState.PENDING,
        result_status=ResultStatus.UNKNOWN,
        job_id=None,
        workdir="/public/home/alice/project",
        script="#!/bin/bash\npython train.py\n",
        exit_code=None,
        terminal_state=None,
        submit_strategy=None,
        submit_response={},
        created_at="2026-09-06T00:00:02Z",
        updated_at="2026-09-06T00:00:02Z",
        contract_id=CONTRACT_ID,
    )


class FakeWorkAreas:
    def __init__(self) -> None:
        self.contract_links: list[str] = []
        self.run_links: list[str] = []

    def get(self, workarea_id: str, *, owner: str) -> object:
        assert workarea_id == WORKAREA_ID
        assert owner == OWNER
        return object()

    def link_contract(self, workarea_id: str, *, owner: str, contract_id: str) -> None:
        self.get(workarea_id, owner=owner)
        self.contract_links.append(contract_id)

    def link_run(self, workarea_id: str, *, owner: str, run_id: str) -> None:
        self.get(workarea_id, owner=owner)
        if run_id not in self.run_links:
            self.run_links.append(run_id)


class FakeContracts:
    def __init__(self, *, status: str = "OK") -> None:
        self.contract = _contract()
        self.status = status

    def get(self, contract_id: str) -> ContractRecord:
        assert contract_id == CONTRACT_ID
        return self.contract

    def preflight(self, contract: ContractRecord) -> ContractValidationResult:
        findings = []
        if self.status == "BLOCK":
            findings.append(
                PreflightFinding(
                    severity=PreflightSeverity.BLOCK,
                    code="TEST.BLOCK",
                    message="blocked for test",
                    source_authority="test",
                )
            )
        return ContractValidationResult(
            status=self.status,
            findings=findings,
            effective_request={
                "workdir": "/public/home/alice/project",
                "script": "#!/bin/bash\npython train.py\n",
            },
            risk_lint=[],
            platform_snapshot={"snapshot_id": "platform-test"},
        )

    def to_submit_request(self, contract: ContractRecord) -> RunSubmitRequest:
        assert contract.contract_id == CONTRACT_ID
        return RunSubmitRequest(
            owner=OWNER,
            workdir=Path("/public/home/alice/project"),
            script="#!/bin/bash\npython train.py\n",
            resource_plan=ResourcePlan(
                partition="Students",
                qos="qos_stu_medium_2gpu",
                nodes=1,
                ntasks=1,
                cpus_per_task=8,
                gpus_per_node=1,
                time_limit="01:00:00",
            ),
            job_name="launch-test",
            contract_id=CONTRACT_ID,
        )


class FakeLaunches:
    def __init__(self, *, latest: LaunchPreflightRecord, assessed_digest: str | None = None) -> None:
        self.candidate = _candidate()
        self.latest = latest
        self.assessed_digest = assessed_digest or latest.assessment_digest
        self.launch: LaunchRecord | None = None
        self.run_ids: list[str] = []

    def get_candidate(self, candidate_id: str, *, owner: str) -> LaunchCandidateRecord:
        assert candidate_id == CANDIDATE_ID
        assert owner == OWNER
        return self.candidate

    def latest_preflight(self, candidate_id: str, *, owner: str) -> LaunchPreflightRecord | None:
        self.get_candidate(candidate_id, owner=owner)
        return self.latest

    def save_preflight(
        self,
        *,
        candidate: LaunchCandidateRecord,
        status: str,
        findings: list[dict[str, Any]],
        effective_request: dict[str, Any],
    ) -> LaunchPreflightRecord:
        assert candidate == self.candidate
        assessment = LaunchPreflightRecord(
            preflight_id=f"preflight-{self.assessed_digest}",
            candidate_id=candidate.candidate_id,
            owner=candidate.owner,
            candidate_digest=candidate.candidate_digest,
            status=status,
            findings=tuple(findings),
            effective_request=effective_request,
            assessment_digest=self.assessed_digest,
            created_at="2026-09-06T00:00:03Z",
        )
        self.latest = assessment
        return assessment

    def commit(
        self,
        *,
        candidate: LaunchCandidateRecord,
        preflight: LaunchPreflightRecord,
        request_key: str,
    ) -> LaunchRecord:
        if self.launch is None:
            self.launch = LaunchRecord(
                launch_id="launch-test",
                candidate_id=candidate.candidate_id,
                preflight_id=preflight.preflight_id,
                workarea_id=candidate.workarea_id,
                owner=candidate.owner,
                contract_id=candidate.contract_id,
                request_key=request_key,
                candidate_digest=candidate.candidate_digest,
                preflight_digest=preflight.assessment_digest,
                committed_at="2026-09-06T00:00:04Z",
                submitted_at=None,
                submit_error=None,
                run_ids=tuple(self.run_ids),
            )
        elif self.launch.request_key != request_key:
            raise LaunchConflict("commit request key changed")
        return self.launch

    def attach_run(
        self,
        launch_id: str,
        *,
        owner: str,
        run_id: str,
        ordinal: int = 0,
    ) -> None:
        assert launch_id == "launch-test"
        assert owner == OWNER
        assert ordinal == 0
        if run_id not in self.run_ids:
            self.run_ids.append(run_id)
        assert self.launch is not None
        self.launch = replace(self.launch, run_ids=tuple(self.run_ids))

    def get(self, launch_id: str, *, owner: str) -> LaunchRecord:
        assert launch_id == "launch-test"
        assert owner == OWNER
        assert self.launch is not None
        return self.launch

    def mark_submitted(self, launch_id: str, *, owner: str) -> None:
        current = self.get(launch_id, owner=owner)
        self.launch = replace(current, submitted_at="2026-09-06T00:00:05Z", submit_error=None)

    def mark_submit_error(
        self,
        launch_id: str,
        *,
        owner: str,
        error: dict[str, Any],
    ) -> None:
        current = self.get(launch_id, owner=owner)
        self.launch = replace(current, submit_error=error)


class FakeRunStore:
    def __init__(self) -> None:
        self.runs: dict[str, RunRecord] = {}

    def get_run(self, run_id: str) -> RunRecord:
        return self.runs[run_id]


class FakeRunService:
    def __init__(self, store: FakeRunStore) -> None:
        self.store = store
        self.prepare_calls = 0
        self.submit_calls = 0

    def prepare(
        self,
        request: RunSubmitRequest,
        *,
        run_id: str | None = None,
        idempotent: bool = False,
    ) -> RunRecord:
        assert idempotent is True
        assert request.contract_id == CONTRACT_ID
        assert run_id is not None
        self.prepare_calls += 1
        if run_id not in self.store.runs:
            self.store.runs[run_id] = _run(run_id, RunState.VALIDATED)
        return self.store.runs[run_id]

    def submit_prepared(self, run_id: str) -> RunRecord:
        self.submit_calls += 1
        submitted = replace(
            self.store.runs[run_id],
            state=RunState.SUBMITTED,
            job_id="12345",
            updated_at="2026-09-06T00:00:05Z",
        )
        self.store.runs[run_id] = submitted
        return submitted


def _service(
    *,
    latest: LaunchPreflightRecord,
    assessed_digest: str | None = None,
    contract_status: str = "OK",
) -> tuple[LaunchService, FakeLaunches, FakeRunService, FakeWorkAreas]:
    workareas = FakeWorkAreas()
    launches = FakeLaunches(latest=latest, assessed_digest=assessed_digest)
    contracts = FakeContracts(status=contract_status)
    run_store = FakeRunStore()
    run_service = FakeRunService(run_store)
    service = LaunchService(
        workareas=workareas,  # type: ignore[arg-type]
        launches=launches,  # type: ignore[arg-type]
        contracts=contracts,  # type: ignore[arg-type]
        run_service=run_service,  # type: ignore[arg-type]
        run_store=run_store,  # type: ignore[arg-type]
    )
    return service, launches, run_service, workareas


def test_assess_freezes_reviewable_submit_request() -> None:
    service, launches, _, _ = _service(latest=_preflight())

    assessment = service.assess(CANDIDATE_ID, owner=OWNER)

    submit = assessment.effective_request["run_submit_request"]
    assert isinstance(submit, dict)
    assert submit["workdir"] == "/public/home/alice/project"
    assert submit["script"] == "#!/bin/bash\npython train.py\n"
    assert submit["contract_id"] == CONTRACT_ID
    assert submit["resource_plan"] == {
        "partition": "Students",
        "qos": "qos_stu_medium_2gpu",
        "nodes": 1,
        "ntasks": 1,
        "cpus_per_task": 8,
        "memory_value": None,
        "memory_unit": None,
        "gpus_per_node": 1,
        "gpus_total": None,
        "gpu_type": None,
        "time_limit": "01:00:00",
        "array": None,
    }
    assert launches.latest == assessment


def test_blocked_preflight_cannot_commit_or_prepare_run() -> None:
    blocked = _preflight(digest="blocked", status="BLOCK")
    service, _, run_service, _ = _service(latest=blocked, contract_status="BLOCK")

    with pytest.raises(LaunchConflict, match="blocked preflight"):
        service.commit(
            CANDIDATE_ID,
            owner=OWNER,
            expected_preflight_digest="blocked",
            request_key="commit-request",
        )

    assert run_service.prepare_calls == 0
    assert run_service.submit_calls == 0


def test_commit_rejects_preflight_that_changed_since_review() -> None:
    reviewed = _preflight(digest="reviewed")
    service, _, run_service, _ = _service(
        latest=reviewed,
        assessed_digest="changed-after-review",
    )

    with pytest.raises(LaunchConflict, match="preflight became stale"):
        service.commit(
            CANDIDATE_ID,
            owner=OWNER,
            expected_preflight_digest="reviewed",
            request_key="commit-request",
        )

    assert run_service.prepare_calls == 0
    assert run_service.submit_calls == 0


def test_retried_commit_reuses_same_run_without_duplicate_submission() -> None:
    reviewed = _preflight(digest="stable")
    service, launches, run_service, workareas = _service(latest=reviewed)

    first = service.commit(
        CANDIDATE_ID,
        owner=OWNER,
        expected_preflight_digest="stable",
        request_key="commit-request",
    )
    second = service.commit(
        CANDIDATE_ID,
        owner=OWNER,
        expected_preflight_digest="stable",
        request_key="commit-request",
    )

    assert first.run.run_id == second.run.run_id
    assert first.run.state == RunState.SUBMITTED
    assert second.run.state == RunState.SUBMITTED
    assert run_service.prepare_calls == 2
    assert run_service.submit_calls == 1
    assert launches.get("launch-test", owner=OWNER).run_ids == (first.run.run_id,)
    assert workareas.run_links == [first.run.run_id]
