"""Runtime reconciliation worker for Phase 0A."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum

from pilot107.adapters.slurm import SlurmAuthError, SlurmBackendError, SlurmTransportError
from pilot107.core.diagnosis import DiagnosisService
from pilot107.core.run_service import RunService
from pilot107.core.states import TERMINAL_RUN_STATES
from pilot107.worker.evidence import CollectionTaskHandler


class WorkerErrorCode(StrEnum):
    AUTH_EXPIRED = "AUTH.EXPIRED"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    AUTH_FORBIDDEN = "AUTH.FORBIDDEN"
    SLURM_BACKEND_ERROR = "SLURM.BACKEND_ERROR"
    EVIDENCE_COLLECTION_ERROR = "EVIDENCE.COLLECTION_ERROR"


@dataclass(frozen=True)
class WorkerErrorClassification:
    code: WorkerErrorCode
    retryable: bool
    auth_required: bool = False


@dataclass(frozen=True)
class WorkerRunError:
    run_id: str
    message: str
    code: str = WorkerErrorCode.SLURM_BACKEND_ERROR.value
    retryable: bool = True
    auth_required: bool = False


@dataclass(frozen=True)
class WorkerTaskError:
    task_id: int
    run_id: str
    task_type: str
    message: str
    code: str = WorkerErrorCode.EVIDENCE_COLLECTION_ERROR.value
    retryable: bool = True
    auth_required: bool = False


@dataclass(frozen=True)
class WorkerDiagnosisError:
    run_id: str
    message: str
    code: str = "DIAGNOSIS.ERROR"
    retryable: bool = True


@dataclass(frozen=True)
class WorkerTickResult:
    checked: int
    terminal: int
    errors: list[WorkerRunError] = field(default_factory=list)
    tasks_checked: int = 0
    tasks_succeeded: int = 0
    task_errors: list[WorkerTaskError] = field(default_factory=list)
    diagnoses_checked: int = 0
    diagnoses_succeeded: int = 0
    diagnosis_errors: list[WorkerDiagnosisError] = field(default_factory=list)
    submissions_checked: int = 0
    submissions_succeeded: int = 0
    submission_errors: list[WorkerRunError] = field(default_factory=list)


class RuntimeReconcileWorker:
    """Poll active runs and reconcile their Slurm state into the RunStore."""

    def __init__(
        self,
        *,
        service: RunService,
        batch_size: int = 50,
        task_handler: CollectionTaskHandler | None = None,
        diagnosis_service: DiagnosisService | None = None,
        worker_id: str = "runtime-worker",
        task_lease_seconds: int = 300,
    ) -> None:
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        if task_lease_seconds <= 0:
            raise ValueError("task_lease_seconds must be positive")
        self.service = service
        self.batch_size = batch_size
        self.task_handler = task_handler
        self.diagnosis_service = diagnosis_service
        self.worker_id = worker_id
        self.task_lease_seconds = task_lease_seconds

    def tick(self) -> WorkerTickResult:
        submission_batch = self.service.dispatch_due_submissions(limit=self.batch_size)
        submission_errors = [
            WorkerRunError(
                run_id=error.run_id,
                message=error.message,
                code="SUBMISSION.DISPATCH_ERROR",
                retryable=True,
            )
            for error in submission_batch.errors
        ]
        runs = self.service.store.list_active_job_runs(limit=self.batch_size)
        terminal = 0
        errors: list[WorkerRunError] = []

        for run in runs:
            try:
                reconciled = self.service.reconcile_once(run.run_id)
            except SlurmBackendError as exc:
                classification = classify_worker_exception(
                    exc,
                    default_code=WorkerErrorCode.SLURM_BACKEND_ERROR,
                    default_retryable=True,
                )
                self.service.store.append_event(
                    run_id=run.run_id,
                    event_type="worker.run_error",
                    payload={
                        "code": classification.code.value,
                        "message": str(exc),
                        "retryable": classification.retryable,
                        "auth_required": classification.auth_required,
                    },
                )
                errors.append(
                    WorkerRunError(
                        run_id=run.run_id,
                        message=str(exc),
                        code=classification.code.value,
                        retryable=classification.retryable,
                        auth_required=classification.auth_required,
                    )
                )
                continue
            if reconciled.state in TERMINAL_RUN_STATES:
                terminal += 1

        retry_runs = self.service.store.list_due_workflow_retries(limit=self.batch_size)
        for retry_run in retry_runs:
            try:
                if self.service.control_repository is None:
                    self.service.submit_prepared(retry_run.run_id)
                else:
                    self.service.enqueue_submission(retry_run.run_id)
            except SlurmBackendError as exc:
                classification = classify_worker_exception(
                    exc,
                    default_code=WorkerErrorCode.SLURM_BACKEND_ERROR,
                    default_retryable=True,
                )
                self.service.store.append_event(
                    run_id=retry_run.run_id,
                    event_type="workflow.retry_submit_failed",
                    payload={
                        "code": classification.code.value,
                        "message": str(exc),
                        "retryable": classification.retryable,
                    },
                )
                errors.append(
                    WorkerRunError(
                        run_id=retry_run.run_id,
                        message=str(exc),
                        code=classification.code.value,
                        retryable=classification.retryable,
                        auth_required=classification.auth_required,
                    )
                )

        tasks_checked = 0
        tasks_succeeded = 0
        task_errors: list[WorkerTaskError] = []
        if self.task_handler is not None:
            tasks = self.service.store.acquire_due_collection_tasks(
                lease_owner=self.worker_id,
                limit=self.batch_size,
                lease_seconds=self.task_lease_seconds,
            )
            tasks_checked = len(tasks)
            for task in tasks:
                run = self.service.store.get_run(task.run_id)
                try:
                    result = self.task_handler.collect(run=run, task_type=task.task_type)
                except Exception as exc:
                    classification = classify_worker_exception(
                        exc,
                        default_code=WorkerErrorCode.EVIDENCE_COLLECTION_ERROR,
                        default_retryable=True,
                    )
                    self.service.store.mark_collection_task_failed(
                        task.task_id,
                        message=str(exc),
                        retryable=classification.retryable,
                        lease_owner=self.worker_id,
                        error_code=classification.code.value,
                        auth_required=classification.auth_required,
                        retry_delay_seconds=_retry_delay_seconds(task.attempts)
                        if classification.retryable
                        else None,
                    )
                    task_errors.append(
                        WorkerTaskError(
                            task_id=task.task_id,
                            run_id=task.run_id,
                            task_type=task.task_type,
                            message=str(exc),
                            code=classification.code.value,
                            retryable=classification.retryable,
                            auth_required=classification.auth_required,
                        )
                    )
                    continue
                self.service.store.mark_collection_task_succeeded(
                    task.task_id,
                    lease_owner=self.worker_id,
                    payload={
                        "artifacts": [artifact.logical_path for artifact in result.artifacts],
                        "warnings": result.warnings,
                    },
                )
                tasks_succeeded += 1

        diagnoses_checked = 0
        diagnoses_succeeded = 0
        diagnosis_errors: list[WorkerDiagnosisError] = []
        if self.diagnosis_service is not None:
            ready_runs = self.service.store.list_runs_ready_for_diagnosis(limit=self.batch_size)
            diagnoses_checked = len(ready_runs)
            for run in ready_runs:
                try:
                    records = self.diagnosis_service.diagnose(run.run_id)
                except Exception as exc:
                    self.service.store.append_event(
                        run_id=run.run_id,
                        event_type="diagnosis.failed",
                        payload={"message": str(exc), "retryable": True},
                    )
                    diagnosis_errors.append(
                        WorkerDiagnosisError(run_id=run.run_id, message=str(exc))
                    )
                    continue
                self.service.store.append_event(
                    run_id=run.run_id,
                    event_type="diagnosis.worker_completed",
                    payload={"diagnosis_count": len(records), "worker_id": self.worker_id},
                )
                diagnoses_succeeded += 1

        return WorkerTickResult(
            checked=len(runs) + len(retry_runs),
            terminal=terminal,
            errors=errors,
            tasks_checked=tasks_checked,
            tasks_succeeded=tasks_succeeded,
            task_errors=task_errors,
            diagnoses_checked=diagnoses_checked,
            diagnoses_succeeded=diagnoses_succeeded,
            diagnosis_errors=diagnosis_errors,
            submissions_checked=submission_batch.checked,
            submissions_succeeded=len(submission_batch.succeeded),
            submission_errors=submission_errors,
        )

    def run_until_idle(
        self,
        *,
        max_ticks: int,
        interval_seconds: float = 1.0,
    ) -> WorkerTickResult:
        if max_ticks <= 0:
            raise ValueError("max_ticks must be positive")
        aggregate = WorkerTickResult(checked=0, terminal=0, errors=[])
        for _ in range(max_ticks):
            result = self.tick()
            aggregate = WorkerTickResult(
                checked=aggregate.checked + result.checked,
                terminal=aggregate.terminal + result.terminal,
                errors=[*aggregate.errors, *result.errors],
                tasks_checked=aggregate.tasks_checked + result.tasks_checked,
                tasks_succeeded=aggregate.tasks_succeeded + result.tasks_succeeded,
                task_errors=[*aggregate.task_errors, *result.task_errors],
                diagnoses_checked=aggregate.diagnoses_checked + result.diagnoses_checked,
                diagnoses_succeeded=aggregate.diagnoses_succeeded + result.diagnoses_succeeded,
                diagnosis_errors=[*aggregate.diagnosis_errors, *result.diagnosis_errors],
                submissions_checked=(aggregate.submissions_checked + result.submissions_checked),
                submissions_succeeded=(
                    aggregate.submissions_succeeded + result.submissions_succeeded
                ),
                submission_errors=[
                    *aggregate.submission_errors,
                    *result.submission_errors,
                ],
            )
            if (
                result.checked == 0
                and result.tasks_checked == 0
                and result.diagnoses_checked == 0
                and result.submissions_checked == 0
            ):
                break
            time.sleep(interval_seconds)
        return aggregate


def classify_worker_exception(
    exc: Exception,
    *,
    default_code: WorkerErrorCode,
    default_retryable: bool,
) -> WorkerErrorClassification:
    message = str(exc).lower()
    if isinstance(exc, SlurmAuthError):
        return WorkerErrorClassification(
            code=WorkerErrorCode.AUTH_FORBIDDEN,
            retryable=False,
            auth_required=True,
        )
    if _looks_like_auth_expired(message):
        return WorkerErrorClassification(
            code=WorkerErrorCode.AUTH_EXPIRED,
            retryable=False,
            auth_required=True,
        )
    if _looks_like_auth_required(message):
        return WorkerErrorClassification(
            code=WorkerErrorCode.AUTH_REQUIRED,
            retryable=False,
            auth_required=True,
        )
    if isinstance(exc, SlurmTransportError):
        return WorkerErrorClassification(code=default_code, retryable=default_retryable)
    return WorkerErrorClassification(code=default_code, retryable=default_retryable)


def _retry_delay_seconds(attempts: int) -> int:
    normalized_attempts = max(1, attempts)
    return int(min(60, 2 ** min(normalized_attempts - 1, 6)))


def _looks_like_auth_expired(message: str) -> bool:
    return (
        "auth.expired" in message
        or "token expired" in message
        or "expired token" in message
        or "jwt expired" in message
        or ("401" in message and "expired" in message)
    )


def _looks_like_auth_required(message: str) -> bool:
    return (
        "auth_required" in message
        or "auth.required" in message
        or "auth missing" in message
        or "missing token" in message
        or "unauthorized" in message
        or "401" in message
    )
