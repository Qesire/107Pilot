"""Runtime reconciliation worker for Phase 0A."""

from __future__ import annotations

import time
from contextlib import suppress
from dataclasses import dataclass, field
from enum import StrEnum
from threading import Event, Thread

from pilot107.adapters.slurm import (
    SlurmAuthError,
    SlurmBackendError,
    SlurmBackendOwnershipError,
    SlurmTransportError,
)
from pilot107.core.advice import AgentAdviceService
from pilot107.core.control_repository import (
    ControlRepository,
    ControlRepositoryConflict,
    OutboxMessage,
)
from pilot107.core.diagnosis import DiagnosisService
from pilot107.core.run_service import RunService
from pilot107.core.run_store import CollectionTaskFenceConflict, CollectionTaskRecord
from pilot107.core.states import TERMINAL_RUN_STATES, CapsuleState, CollectionState
from pilot107.observability.adapters import RunObservationTarget
from pilot107.observability.collector import (
    ObservabilityCollector,
    ObservabilityTickResult,
)
from pilot107.runtime_watch.service import RuntimeWatchService
from pilot107.services.agent_session_service import AgentSessionService
from pilot107.worker.agent_turn_worker import AgentTurnWorker
from pilot107.worker.capsule import CapsuleError, RawCapsuleService
from pilot107.worker.evidence import CollectionTaskHandler


class WorkerErrorCode(StrEnum):
    AUTH_EXPIRED = "AUTH.EXPIRED"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    AUTH_FORBIDDEN = "AUTH.FORBIDDEN"
    SLURM_BACKEND_ERROR = "SLURM.BACKEND_ERROR"
    EVIDENCE_COLLECTION_ERROR = "EVIDENCE.COLLECTION_ERROR"
    CAPSULE_AUTO_BUILD_ERROR = "CAPSULE.AUTO_BUILD_ERROR"
    SLURM_BACKEND_OWNERSHIP_LOST = "SLURM.BACKEND_OWNERSHIP_LOST"


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
    agent_executions_checked: int = 0
    agent_executions_succeeded: int = 0
    agent_execution_errors: list[WorkerRunError] = field(default_factory=list)
    agent_turns_checked: int = 0
    agent_turns_succeeded: int = 0
    agent_turn_errors: list[WorkerRunError] = field(default_factory=list)
    capsule_builds_attempted: int = 0
    capsule_builds_succeeded: int = 0
    capsule_errors: list[WorkerRunError] = field(default_factory=list)
    runtime_watches_checked: int = 0
    runtime_watches_with_data: int = 0
    runtime_watch_bytes_read: int = 0
    runtime_watch_errors: list[str] = field(default_factory=list)
    observability_cycles: int = 0
    observability_samples: int = 0
    observability_summaries: int = 0
    observability_commands: int = 0
    observability_budget_skipped: bool = False
    observability_errors: list[str] = field(default_factory=list)


@dataclass
class _CapsuleBuildStats:
    attempted: int = 0
    succeeded: int = 0
    errors: list[WorkerRunError] = field(default_factory=list)


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
        collection_max_attempts: int = 5,
        agent_advice_service: AgentAdviceService | None = None,
        agent_session_service: AgentSessionService | None = None,
        agent_turn_worker: AgentTurnWorker | None = None,
        capsule_service: RawCapsuleService | None = None,
        runtime_watch_service: RuntimeWatchService | None = None,
        observability_collector: ObservabilityCollector | None = None,
        observability_connection_id: str = "default",
    ) -> None:
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        if task_lease_seconds <= 0:
            raise ValueError("task_lease_seconds must be positive")
        if collection_max_attempts <= 0:
            raise ValueError("collection_max_attempts must be positive")
        self.service = service
        self.batch_size = batch_size
        self.task_handler = task_handler
        self.diagnosis_service = diagnosis_service
        self.worker_id = worker_id
        self.task_lease_seconds = task_lease_seconds
        self.collection_max_attempts = collection_max_attempts
        self.agent_advice_service = agent_advice_service
        self.agent_session_service = agent_session_service
        self.agent_turn_worker = agent_turn_worker
        self.capsule_service = capsule_service
        self.runtime_watch_service = runtime_watch_service
        self.observability_collector = observability_collector
        self.observability_connection_id = observability_connection_id

    def tick(self) -> WorkerTickResult:
        runtime_watch_result = (
            self.runtime_watch_service.tick() if self.runtime_watch_service is not None else None
        )
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
        agent_batch = (
            self.agent_advice_service.dispatch_due_executions(limit=self.batch_size)
            if self.agent_advice_service is not None
            else None
        )
        agent_execution_errors = [
            WorkerRunError(
                run_id=error.execution_id,
                message=error.message,
                code="AGENT.EXECUTION_DISPATCH_ERROR",
                retryable=True,
            )
            for error in (agent_batch.errors if agent_batch is not None else [])
        ]
        agent_turn_batch = None
        agent_turn_errors: list[WorkerRunError] = []
        if self.agent_session_service is not None and self.agent_turn_worker is not None:
            try:
                self.agent_session_service.recover_pending_turns(limit=self.batch_size)
                agent_turn_batch = self.agent_turn_worker.dispatch_due(limit=self.batch_size)
            except Exception:
                agent_turn_errors.append(
                    WorkerRunError(
                        run_id="agent-turn-recovery",
                        message="Agent Turn recovery failed",
                        code="AGENT.TURN_RECOVERY_ERROR",
                        retryable=True,
                    )
                )
            else:
                agent_turn_errors.extend(
                    WorkerRunError(
                        run_id=error.turn_id,
                        message=error.message,
                        code=error.code,
                        retryable=error.retryable,
                    )
                    for error in agent_turn_batch.errors
                )
        runs = self.service.store.list_active_job_runs(limit=self.batch_size)
        terminal = 0
        errors: list[WorkerRunError] = []
        observability_errors: list[str] = []

        for run in runs:
            try:
                reconciled = self.service.reconcile_once(run.run_id)
            except SlurmBackendOwnershipError:
                self.service.store.mark_backend_orphaned(
                    run.run_id,
                    backend=type(self.service.backend).__name__,
                    job_id=run.job_id or "",
                )
                terminal += 1
                continue
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
            if self.observability_collector is not None and reconciled.job_id is not None:
                try:
                    self.observability_collector.observe_run(
                        RunObservationTarget(
                            connection_id=self.observability_connection_id,
                            owner=reconciled.owner,
                            run_id=reconciled.run_id,
                            job_id=reconciled.job_id,
                            attempt=reconciled.attempt,
                        ),
                        state=reconciled.state.value,
                    )
                except Exception as exc:
                    observability_errors.append(
                        f"{reconciled.run_id}:{type(exc).__name__}"
                    )
            if self.runtime_watch_service is not None and reconciled.job_id is not None:
                self.runtime_watch_service.ensure_run(
                    run_id=reconciled.run_id,
                    owner=reconciled.owner,
                )
                if (
                    reconciled.state in TERMINAL_RUN_STATES
                    and self.runtime_watch_service.on_run_terminal(
                        run_id=reconciled.run_id,
                        owner=reconciled.owner,
                    )
                ):
                    self.service.store.defer_logs_finalize_for_runtime_watch(reconciled.run_id)

        observability_result: ObservabilityTickResult | None = None
        if self.observability_collector is not None:
            try:
                observability_result = self.observability_collector.tick(
                    self.observability_connection_id
                )
            except Exception as exc:
                observability_errors.append(f"collector:{type(exc).__name__}")
            else:
                observability_errors.extend(observability_result.errors)

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
        capsule_stats = _CapsuleBuildStats()
        if self.task_handler is not None:
            tasks_checked, tasks_succeeded, task_errors = self._dispatch_collection_tasks(
                capsule_stats
            )

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
            agent_executions_checked=agent_batch.checked if agent_batch is not None else 0,
            agent_executions_succeeded=(
                len(agent_batch.succeeded) if agent_batch is not None else 0
            ),
            agent_execution_errors=agent_execution_errors,
            agent_turns_checked=(agent_turn_batch.checked if agent_turn_batch is not None else 0),
            agent_turns_succeeded=(
                agent_turn_batch.succeeded if agent_turn_batch is not None else 0
            ),
            agent_turn_errors=agent_turn_errors,
            capsule_builds_attempted=capsule_stats.attempted,
            capsule_builds_succeeded=capsule_stats.succeeded,
            capsule_errors=capsule_stats.errors,
            runtime_watches_checked=(
                runtime_watch_result.watches_checked if runtime_watch_result else 0
            ),
            runtime_watches_with_data=(
                runtime_watch_result.watches_with_data if runtime_watch_result else 0
            ),
            runtime_watch_bytes_read=(
                runtime_watch_result.bytes_read if runtime_watch_result else 0
            ),
            runtime_watch_errors=(
                list(runtime_watch_result.errors) if runtime_watch_result else []
            ),
            observability_cycles=(
                len(observability_result.cycles) if observability_result else 0
            ),
            observability_samples=(
                len(observability_result.run_samples) if observability_result else 0
            ),
            observability_summaries=(
                len(observability_result.summaries) if observability_result else 0
            ),
            observability_commands=(
                observability_result.command_count if observability_result else 0
            ),
            observability_budget_skipped=(
                observability_result.skipped_budget if observability_result else False
            ),
            observability_errors=observability_errors,
        )

    def _dispatch_collection_tasks(
        self,
        capsule_stats: _CapsuleBuildStats,
    ) -> tuple[int, int, list[WorkerTaskError]]:
        if self.task_handler is None:
            return 0, 0, []
        repository = self.service.control_repository
        if repository is None:
            return self._dispatch_legacy_collection_tasks(capsule_stats)

        for task in self.service.store.list_collection_tasks_for_dispatch(limit=self.batch_size):
            repository.enqueue(
                message_id=_collection_message_id(task),
                topic="collection.execute",
                aggregate_id=task.run_id,
                payload={
                    "task_id": task.task_id,
                    "run_id": task.run_id,
                    "task_type": task.task_type,
                    "generation": task.generation,
                },
            )
        checked = 0
        succeeded = 0
        errors: list[WorkerTaskError] = []
        for _ in range(self.batch_size):
            claimed = repository.claim_outbox(
                owner=self.worker_id,
                limit=1,
                lease_seconds=self.task_lease_seconds,
                topics=("collection.execute",),
            )
            if not claimed:
                break
            message = claimed[0]
            checked += 1
            try:
                completed, error, run_id = self._execute_collection_message(message)
            except Exception as exc:
                task_id, run_id, task_type, _ = _collection_message_identity(
                    message,
                    strict=False,
                )
                with suppress(ControlRepositoryConflict, RuntimeError):
                    repository.retry(
                        message_id=message.message_id,
                        owner=message.lease_owner or self.worker_id,
                        fencing_token=message.fencing_token,
                        error=str(exc),
                        delay_seconds=1,
                        max_attempts=self.collection_max_attempts,
                    )
                errors.append(
                    WorkerTaskError(
                        task_id=task_id,
                        run_id=run_id,
                        task_type=task_type,
                        message=str(exc),
                    )
                )
                continue
            succeeded += int(completed)
            if error is not None:
                errors.append(error)
            if completed and run_id is not None:
                self._record_auto_capsule(run_id, capsule_stats)
        return checked, succeeded, errors

    def _execute_collection_message(
        self,
        message: OutboxMessage,
    ) -> tuple[bool, WorkerTaskError | None, str | None]:
        repository = self.service.control_repository
        if repository is None or self.task_handler is None:
            raise RuntimeError("collection outbox dependencies are unavailable")
        task_id, run_id, task_type, generation = _collection_message_identity(message)
        task = self.service.store.get_collection_task(task_id)
        if task.run_id != run_id or task.task_type != task_type or task.generation != generation:
            self._acknowledge_collection(message)
            return False, None, None
        if task.state == "succeeded":
            self._acknowledge_collection(message)
            return True, None, run_id
        if task.state == "failed_permanent":
            self._acknowledge_collection(message)
            return False, None, None
        if message.lease_owner is None or message.lease_expires_at is None:
            raise RuntimeError("collection outbox message has no active lease")
        claimed = self.service.store.claim_collection_task(
            task_id,
            lease_owner=message.lease_owner,
            fencing_token=message.fencing_token,
            generation=generation,
            lease_expires_at=message.lease_expires_at,
        )
        if claimed is None:
            raise CollectionTaskFenceConflict(f"collection task is fenced: {task_id}")

        run = self.service.store.get_run(run_id)
        try:
            with _OutboxHeartbeat(
                repository=repository,
                message=message,
                lease_seconds=self.task_lease_seconds,
            ):
                result = self.task_handler.collect(run=run, task_type=task_type)
        except Exception as exc:
            classification = classify_worker_exception(
                exc,
                default_code=WorkerErrorCode.EVIDENCE_COLLECTION_ERROR,
                default_retryable=True,
            )
            can_retry = classification.retryable and message.attempts < self.collection_max_attempts
            retry_delay = _retry_delay_seconds(message.attempts) if can_retry else None
            self.service.store.mark_collection_task_failed(
                task_id,
                message=str(exc),
                retryable=can_retry,
                lease_owner=message.lease_owner,
                fencing_token=message.fencing_token,
                error_code=classification.code.value,
                auth_required=classification.auth_required,
                retry_delay_seconds=retry_delay,
            )
            repository.retry(
                message_id=message.message_id,
                owner=message.lease_owner,
                fencing_token=message.fencing_token,
                error=str(exc),
                delay_seconds=retry_delay or 0,
                max_attempts=(self.collection_max_attempts if classification.retryable else 1),
            )
            return (
                False,
                WorkerTaskError(
                    task_id=task_id,
                    run_id=run_id,
                    task_type=task_type,
                    message=str(exc),
                    code=classification.code.value,
                    retryable=can_retry,
                    auth_required=classification.auth_required,
                ),
                run_id,
            )

        self.service.store.mark_collection_task_succeeded(
            task_id,
            lease_owner=message.lease_owner,
            fencing_token=message.fencing_token,
            payload={
                "artifacts": [artifact.logical_path for artifact in result.artifacts],
                "warnings": result.warnings,
            },
        )
        self._acknowledge_collection(message)
        return True, None, run_id

    def _acknowledge_collection(self, message: OutboxMessage) -> None:
        repository = self.service.control_repository
        if repository is None or message.lease_owner is None:
            return
        with suppress(ControlRepositoryConflict):
            repository.acknowledge(
                message_id=message.message_id,
                owner=message.lease_owner,
                fencing_token=message.fencing_token,
            )

    def _dispatch_legacy_collection_tasks(
        self,
        capsule_stats: _CapsuleBuildStats,
    ) -> tuple[int, int, list[WorkerTaskError]]:
        assert self.task_handler is not None
        tasks = self.service.store.acquire_due_collection_tasks(
            lease_owner=self.worker_id,
            limit=self.batch_size,
            lease_seconds=self.task_lease_seconds,
        )
        succeeded = 0
        errors: list[WorkerTaskError] = []
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
                errors.append(
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
            succeeded += 1
            self._record_auto_capsule(task.run_id, capsule_stats)
        return len(tasks), succeeded, errors

    def _record_auto_capsule(
        self,
        run_id: str,
        capsule_stats: _CapsuleBuildStats,
    ) -> None:
        """Best-effort, idempotent auto-capsule build after collection succeeds.

        Non-fatal: CapsuleError is swallowed and only recorded as an event so
        the tick continues. Other exceptions are recorded both as an event and
        a WorkerRunError so health reflects the failure without crashing. The
        explicit ``POST /runs/{id}/capsule`` endpoint remains authoritative.
        """
        if self.capsule_service is None:
            return
        try:
            run = self.service.store.get_run(run_id)
        except Exception:
            return
        if run.collection_state != CollectionState.SUCCEEDED:
            return
        if run.capsule_state == CapsuleState.READY:
            return
        capsule_stats.attempted += 1
        try:
            self.capsule_service.build_raw_capsule(run_id)
        except CapsuleError as exc:
            self.service.store.append_event(
                run_id=run_id,
                event_type="capsule.auto_build_skipped",
                payload={"message": str(exc), "non_fatal": True},
            )
            return
        except Exception as exc:
            self.service.store.append_event(
                run_id=run_id,
                event_type="capsule.auto_build_failed",
                payload={"message": str(exc), "retryable": True},
            )
            capsule_stats.errors.append(
                WorkerRunError(
                    run_id=run_id,
                    message=str(exc),
                    code=WorkerErrorCode.CAPSULE_AUTO_BUILD_ERROR.value,
                    retryable=True,
                )
            )
            return
        capsule_stats.succeeded += 1
        self.service.store.append_event(
            run_id=run_id,
            event_type="capsule.auto_build_completed",
            payload={"worker_id": self.worker_id},
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
                agent_executions_checked=(
                    aggregate.agent_executions_checked + result.agent_executions_checked
                ),
                agent_executions_succeeded=(
                    aggregate.agent_executions_succeeded + result.agent_executions_succeeded
                ),
                agent_execution_errors=[
                    *aggregate.agent_execution_errors,
                    *result.agent_execution_errors,
                ],
                capsule_builds_attempted=(
                    aggregate.capsule_builds_attempted + result.capsule_builds_attempted
                ),
                capsule_builds_succeeded=(
                    aggregate.capsule_builds_succeeded + result.capsule_builds_succeeded
                ),
                capsule_errors=[*aggregate.capsule_errors, *result.capsule_errors],
                runtime_watches_checked=(
                    aggregate.runtime_watches_checked + result.runtime_watches_checked
                ),
                runtime_watches_with_data=(
                    aggregate.runtime_watches_with_data + result.runtime_watches_with_data
                ),
                runtime_watch_bytes_read=(
                    aggregate.runtime_watch_bytes_read + result.runtime_watch_bytes_read
                ),
                runtime_watch_errors=[
                    *aggregate.runtime_watch_errors,
                    *result.runtime_watch_errors,
                ],
                observability_cycles=(
                    aggregate.observability_cycles + result.observability_cycles
                ),
                observability_samples=(
                    aggregate.observability_samples + result.observability_samples
                ),
                observability_summaries=(
                    aggregate.observability_summaries + result.observability_summaries
                ),
                observability_commands=(
                    aggregate.observability_commands + result.observability_commands
                ),
                observability_budget_skipped=(
                    aggregate.observability_budget_skipped
                    or result.observability_budget_skipped
                ),
                observability_errors=[
                    *aggregate.observability_errors,
                    *result.observability_errors,
                ],
            )
            if (
                result.checked == 0
                and result.tasks_checked == 0
                and result.diagnoses_checked == 0
                and result.submissions_checked == 0
                and result.agent_executions_checked == 0
                and result.runtime_watches_checked == 0
                and result.observability_cycles == 0
            ):
                break
            time.sleep(interval_seconds)
        return aggregate


def _collection_message_id(task: CollectionTaskRecord) -> str:
    return f"collection:{task.task_id}:{task.generation}"


def _collection_message_identity(
    message: OutboxMessage,
    *,
    strict: bool = True,
) -> tuple[int, str, str, int]:
    task_id = message.payload.get("task_id")
    run_id = message.payload.get("run_id")
    task_type = message.payload.get("task_type")
    generation = message.payload.get("generation")
    valid = (
        message.topic == "collection.execute"
        and isinstance(task_id, int)
        and not isinstance(task_id, bool)
        and task_id > 0
        and isinstance(run_id, str)
        and bool(run_id)
        and message.aggregate_id == run_id
        and isinstance(task_type, str)
        and bool(task_type)
        and isinstance(generation, int)
        and not isinstance(generation, bool)
        and generation > 0
    )
    if not valid and strict:
        raise ValueError("collection outbox identity is invalid")
    return (
        task_id if isinstance(task_id, int) and not isinstance(task_id, bool) else 0,
        run_id if isinstance(run_id, str) and run_id else message.aggregate_id,
        task_type if isinstance(task_type, str) and task_type else "unknown",
        generation if isinstance(generation, int) and not isinstance(generation, bool) else 0,
    )


class _OutboxHeartbeat:
    def __init__(
        self,
        *,
        repository: ControlRepository,
        message: OutboxMessage,
        lease_seconds: int,
    ) -> None:
        if message.lease_owner is None:
            raise ValueError("heartbeat requires an owned outbox message")
        self.repository = repository
        self.message = message
        self.lease_seconds = lease_seconds
        self.interval_seconds = max(0.1, lease_seconds / 3)
        self.stop_event = Event()
        self.error: Exception | None = None
        self.thread = Thread(target=self._run, daemon=True)

    def __enter__(self) -> _OutboxHeartbeat:
        self.thread.start()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.stop_event.set()
        self.thread.join(timeout=max(1.0, self.interval_seconds + 1))
        if exc_type is None and self.error is not None:
            raise CollectionTaskFenceConflict(str(self.error)) from self.error

    def _run(self) -> None:
        assert self.message.lease_owner is not None
        while not self.stop_event.wait(self.interval_seconds):
            try:
                self.repository.renew_outbox(
                    message_id=self.message.message_id,
                    owner=self.message.lease_owner,
                    fencing_token=self.message.fencing_token,
                    lease_seconds=self.lease_seconds,
                )
            except Exception as exc:
                self.error = exc
                self.stop_event.set()
                return


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
