"""Service entrypoint for the Phase 0A runtime worker."""

from __future__ import annotations

import argparse
import os
import signal
import socket
import sys
import time
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from threading import Event

from pilot107.adapters.rest_token import SimulatorRestTokenProvider
from pilot107.adapters.rest_token_backend import (
    DEFAULT_JOB_NAME_MARKER,
    TokenMintingRestBackend,
)
from pilot107.adapters.slurm import (
    DemoSlurmBackend,
    DockerComposeExecutor,
    DockerComposeTarget,
    DockerSimulatorCommandBackend,
    HttpCommandGatewayExecutor,
    InMemorySlurmBackend,
    RestAuthStyle,
    RestNativeSlurmBackend,
    SimulatorExecutor,
    SimulatorPathChecker,
    SlurmBackend,
    UrllibHttpTransport,
)
from pilot107.core.advice import AgentAdviceService, AgentPolicyEngine
from pilot107.core.agent import AgentExplainService
from pilot107.core.contracts import ContractService, ContractStore, RecipeCatalog
from pilot107.core.control_repository import SQLiteControlRepository
from pilot107.core.diagnosis import DiagnosisService
from pilot107.core.evidence_binding import EvidenceBinder
from pilot107.core.preflight import LocalPathChecker, PathChecker
from pilot107.core.redaction import redact_sensitive_structure, redact_sensitive_text
from pilot107.core.remediation_store import RemediationStore
from pilot107.core.run_service import RunService
from pilot107.core.run_store import RunStore
from pilot107.services.remediation_service import RemediationService
from pilot107.worker.evidence import (
    AuthorizedFilesystemEvidenceTransport,
    CollectionTaskHandler,
    DemoEvidenceCollector,
    DockerSlurmEvidenceCollector,
    DockerVolumeEvidenceTransport,
    EvidenceStore,
    EvidenceTransport,
)
from pilot107.worker.runtime_worker import RuntimeReconcileWorker, WorkerTickResult
from pilot107.worker.telemetry import (
    WorkerTelemetryError,
    WorkerTelemetryStore,
    write_json_atomic,
)


@dataclass(frozen=True)
class WorkerServiceConfig:
    db_path: Path
    evidence_root: Path
    backend: str = "docker-compose-command"
    allowed_roots: tuple[str, ...] = ("/public/home/alice",)
    worker_id: str = "runtime-worker"
    batch_size: int = 50
    interval_seconds: float = 1.0
    task_lease_seconds: int = 300
    command_timeout_seconds: float = 20.0
    compose_file: Path | None = None
    compose_env_file: Path | None = None
    compose_workdir: Path | None = None
    compose_service: str = "login-node-sim"
    command_gateway_url: str = "http://pilot107-command-gateway:8090"
    command_gateway_token: str | None = None
    slurmrestd_url: str = "http://slurmrestd:6820"
    slurm_api_version: str = "v0.0.41"
    slurm_token: str | None = None
    rest_auth_style: RestAuthStyle = RestAuthStyle.BEARER
    slurm_username: str | None = None
    rest_token_provider_enabled: bool = False
    workdir_preflight_enabled: bool = True
    idempotency_reconcile_enabled: bool = True
    health_path: Path | None = None
    metrics_root: Path | None = None
    enable_docker_volume_evidence_transport: bool = False


@dataclass(frozen=True)
class WorkerServiceStack:
    store: RunStore
    service: RunService
    worker: RuntimeReconcileWorker
    remediation_service: RemediationService


class WorkerService:
    def __init__(self, *, config: WorkerServiceConfig, stack: WorkerServiceStack) -> None:
        self.config = config
        self.stack = stack
        self.last_remediation_checked = 0
        self.last_remediation_advanced = 0
        self.last_remediation_errors: list[str] = []
        self.last_tick_duration_seconds = 0.0
        self.last_telemetry_error: str | None = None
        self.cumulative_metrics: dict[str, object] | None = None
        self.telemetry = (
            None
            if config.metrics_root is None
            else WorkerTelemetryStore(root=config.metrics_root, worker_id=config.worker_id)
        )

    def run_once(self) -> WorkerTickResult:
        started = time.monotonic()
        result = self.stack.worker.tick()
        self._advance_remediations()
        self.last_tick_duration_seconds = time.monotonic() - started
        self._record_telemetry(result)
        self.write_health(result)
        return result

    def run_ticks(self, *, max_ticks: int, stop_when_idle: bool = False) -> WorkerTickResult:
        if max_ticks <= 0:
            raise ValueError("max_ticks must be positive")
        aggregate = WorkerTickResult(checked=0, terminal=0)
        for _ in range(max_ticks):
            result = self.run_once()
            aggregate = _merge_tick_results(aggregate, result)
            if (
                stop_when_idle
                and result.checked == 0
                and result.tasks_checked == 0
                and result.diagnoses_checked == 0
                and result.submissions_checked == 0
                and result.agent_executions_checked == 0
                and self.last_remediation_advanced == 0
            ):
                break
            time.sleep(self.config.interval_seconds)
        return aggregate

    def run_forever(self, *, stop_event: Event) -> None:
        try:
            while not stop_event.is_set():
                self.run_once()
                stop_event.wait(self.config.interval_seconds)
        finally:
            if self.telemetry is not None:
                with suppress(OSError, WorkerTelemetryError, ValueError):
                    self.telemetry.mark_stopped()

    def write_health(self, result: WorkerTickResult) -> None:
        if self.config.health_path is None:
            return
        payload = {
            "ok": not (
                result.errors
                or result.task_errors
                or result.diagnosis_errors
                or result.submission_errors
                or result.agent_execution_errors
                or self.last_remediation_errors
                or self.last_telemetry_error
            ),
            "worker_id": self.config.worker_id,
            "backend": self.config.backend,
            "last_tick_unix": time.time(),
            "last_tick_duration_seconds": round(self.last_tick_duration_seconds, 6),
            "checked": result.checked,
            "terminal": result.terminal,
            "tasks_checked": result.tasks_checked,
            "tasks_succeeded": result.tasks_succeeded,
            "diagnoses_checked": result.diagnoses_checked,
            "diagnoses_succeeded": result.diagnoses_succeeded,
            "submissions_checked": result.submissions_checked,
            "submissions_succeeded": result.submissions_succeeded,
            "errors": redact_sensitive_structure([error.__dict__ for error in result.errors]),
            "task_errors": redact_sensitive_structure(
                [error.__dict__ for error in result.task_errors]
            ),
            "diagnosis_errors": redact_sensitive_structure(
                [error.__dict__ for error in result.diagnosis_errors]
            ),
            "submission_errors": redact_sensitive_structure(
                [error.__dict__ for error in result.submission_errors]
            ),
            "agent_executions_checked": result.agent_executions_checked,
            "agent_executions_succeeded": result.agent_executions_succeeded,
            "agent_execution_errors": redact_sensitive_structure(
                [error.__dict__ for error in result.agent_execution_errors]
            ),
            "remediation_checked": self.last_remediation_checked,
            "remediation_advanced": self.last_remediation_advanced,
            "remediation_errors": redact_sensitive_structure(self.last_remediation_errors),
            "telemetry_error": self.last_telemetry_error,
            "cumulative_metrics": self.cumulative_metrics,
        }
        write_json_atomic(self.config.health_path, payload)

    def _record_telemetry(self, result: WorkerTickResult) -> None:
        if self.telemetry is None:
            self.last_telemetry_error = None
            self.cumulative_metrics = None
            return
        increments = {
            "ticks_total": 1,
            "reconcile_checked_total": result.checked,
            "reconcile_terminal_total": result.terminal,
            "reconcile_errors_total": len(result.errors),
            "collection_checked_total": result.tasks_checked,
            "collection_succeeded_total": result.tasks_succeeded,
            "collection_errors_total": len(result.task_errors),
            "diagnosis_checked_total": result.diagnoses_checked,
            "diagnosis_succeeded_total": result.diagnoses_succeeded,
            "diagnosis_errors_total": len(result.diagnosis_errors),
            "submission_checked_total": result.submissions_checked,
            "submission_succeeded_total": result.submissions_succeeded,
            "submission_errors_total": len(result.submission_errors),
            "agent_execution_checked_total": result.agent_executions_checked,
            "agent_execution_succeeded_total": result.agent_executions_succeeded,
            "agent_execution_errors_total": len(result.agent_execution_errors),
            "remediation_checked_total": self.last_remediation_checked,
            "remediation_advanced_total": self.last_remediation_advanced,
            "remediation_errors_total": len(self.last_remediation_errors),
        }
        try:
            self.cumulative_metrics = self.telemetry.update(
                increments=increments,
                tick_duration_seconds=self.last_tick_duration_seconds,
            )
        except (OSError, WorkerTelemetryError, ValueError) as exc:
            self.cumulative_metrics = None
            self.last_telemetry_error = redact_sensitive_text(
                f"{type(exc).__name__}:{exc}"
            )
        else:
            self.last_telemetry_error = None

    def _advance_remediations(self) -> None:
        sessions = self.stack.remediation_service.remediation_store.list_actionable_sessions(
            limit=self.config.batch_size
        )
        advanced = 0
        errors: list[str] = []
        for session in sessions:
            try:
                updated = self.stack.remediation_service.advance(
                    session.session_id,
                    worker_id=self.config.worker_id,
                )
                if updated.version != session.version or updated.state != session.state:
                    advanced += 1
            except Exception as exc:
                errors.append(f"{session.session_id}:{type(exc).__name__}:{exc}")
        self.last_remediation_checked = len(sessions)
        self.last_remediation_advanced = advanced
        self.last_remediation_errors = errors


def config_from_env(
    env: Mapping[str, str] | None = None,
    *,
    project_root: Path | None = None,
) -> WorkerServiceConfig:
    values = os.environ if env is None else env
    root = project_root or Path(__file__).resolve().parents[3]
    compose_workdir = _path(values, "PILOT107_COMPOSE_WORKDIR", root / "simulator" / "compose")
    runtime_dir = root / "data" / "phase0"
    return WorkerServiceConfig(
        db_path=_path(values, "PILOT107_DB_PATH", runtime_dir / "pilot107.db"),
        evidence_root=_path(values, "PILOT107_EVIDENCE_ROOT", runtime_dir / "evidence"),
        backend=values.get("PILOT107_WORKER_BACKEND", "docker-compose-command"),
        allowed_roots=tuple(
            _split_csv(values.get("PILOT107_ALLOWED_ROOTS", "/public/home/alice"))
        ),
        worker_id=values.get("PILOT107_WORKER_ID", f"runtime-worker-{socket.gethostname()}"),
        batch_size=_int(values, "PILOT107_WORKER_BATCH_SIZE", 50),
        interval_seconds=_float(values, "PILOT107_WORKER_INTERVAL_SECONDS", 1.0),
        task_lease_seconds=_int(values, "PILOT107_WORKER_TASK_LEASE_SECONDS", 300),
        command_timeout_seconds=_float(values, "PILOT107_COMMAND_TIMEOUT_SECONDS", 20.0),
        compose_file=_path(values, "PILOT107_COMPOSE_FILE", compose_workdir / "compose.yml"),
        compose_env_file=_path(
            values,
            "PILOT107_COMPOSE_ENV_FILE",
            compose_workdir / ".env.example",
        ),
        compose_workdir=compose_workdir,
        compose_service=values.get("PILOT107_COMPOSE_SERVICE", "login-node-sim"),
        command_gateway_url=values.get(
            "PILOT107_COMMAND_GATEWAY_URL",
            "http://pilot107-command-gateway:8090",
        ),
        command_gateway_token=values.get("PILOT107_COMMAND_GATEWAY_TOKEN"),
        slurmrestd_url=values.get("PILOT107_SLURMRESTD_URL", "http://slurmrestd:6820"),
        slurm_api_version=values.get("PILOT107_SLURM_API_VERSION", "v0.0.41"),
        slurm_token=values.get("PILOT107_SLURM_TOKEN"),
        rest_auth_style=RestAuthStyle(
            values.get("PILOT107_REST_AUTH_STYLE", RestAuthStyle.BEARER.value)
        ),
        slurm_username=values.get("PILOT107_SLURM_USER_NAME"),
        rest_token_provider_enabled=_bool(values, "PILOT107_REST_TOKEN_PROVIDER", False),
        workdir_preflight_enabled=_bool(values, "PILOT107_WORKDIR_PREFLIGHT", True),
        idempotency_reconcile_enabled=_bool(
            values, "PILOT107_IDEMPOTENCY_RECONCILE", True
        ),
        health_path=_optional_path(
            values,
            "PILOT107_WORKER_HEALTH_PATH",
            runtime_dir / "worker-health.json",
        ),
        metrics_root=_optional_path(
            values,
            "PILOT107_WORKER_METRICS_ROOT",
            runtime_dir / "worker-metrics",
        ),
        enable_docker_volume_evidence_transport=_bool(
            values,
            "PILOT107_ENABLE_DOCKER_VOLUME_EVIDENCE_TRANSPORT",
            False,
        ),
    )


def build_worker_service(config: WorkerServiceConfig) -> WorkerService:
    store = RunStore(config.db_path)
    control_repository = SQLiteControlRepository(config.db_path)
    evidence_store = EvidenceStore(config.evidence_root)
    backend, task_handler, reconcile_backend = _build_backend_and_task_handler(
        config, store, evidence_store
    )
    path_checker, path_checker_factory = _worker_preflight_checkers(config)
    run_service = RunService(
        store=store,
        backend=backend,
        workdir_preflight_enabled=config.workdir_preflight_enabled,
        preflight_allowed_roots=config.allowed_roots,
        preflight_shared_roots=_worker_shared_roots(config),
        preflight_local_roots=_worker_local_roots(config),
        preflight_path_checker=path_checker,
        preflight_path_checker_factory=path_checker_factory,
        idempotency_reconcile_enabled=config.idempotency_reconcile_enabled,
        reconcile_backend=reconcile_backend,
        job_name_marker=DEFAULT_JOB_NAME_MARKER,
        control_repository=control_repository,
        dispatcher_id=config.worker_id,
    )
    contract_store = ContractStore(config.db_path)
    contract_service = ContractService(
        catalog=RecipeCatalog(store=contract_store),
        store=contract_store,
    )
    explain_service = AgentExplainService(
        store=store,
        llm_provider=None,
        evidence_binder=EvidenceBinder(
            store=store,
            evidence_root=config.evidence_root,
        ),
    )
    advice_service = AgentAdviceService(
        store=store,
        explain_service=explain_service,
        policy_engine=AgentPolicyEngine(contract_service=contract_service),
        contract_service=contract_service,
        run_service=run_service,
    )
    remediation_service = RemediationService(
        run_store=store,
        remediation_store=RemediationStore(config.db_path),
        advice_service=advice_service,
    )
    worker = RuntimeReconcileWorker(
        service=run_service,
        batch_size=config.batch_size,
        task_handler=task_handler,
        diagnosis_service=DiagnosisService(store=store),
        worker_id=config.worker_id,
        task_lease_seconds=config.task_lease_seconds,
        agent_advice_service=advice_service,
    )
    return WorkerService(
        config=config,
        stack=WorkerServiceStack(
            store=store,
            service=run_service,
            worker=worker,
            remediation_service=remediation_service,
        ),
    )


def _worker_preflight_checkers(
    config: WorkerServiceConfig,
) -> tuple[PathChecker | None, Callable[[str], PathChecker] | None]:
    if not config.workdir_preflight_enabled:
        return None, None
    if config.backend in {"in-memory", "demo"}:
        # These backends never access the filesystem. Match the API builder's
        # pure-path authorization contract instead of evaluating container UID
        # permissions for a simulated user home.
        return None, None
    if config.backend == "command-gateway":
        executor = HttpCommandGatewayExecutor(
            base_url=config.command_gateway_url,
            token=config.command_gateway_token,
            timeout_seconds=config.command_timeout_seconds,
        )
        return (
            None,
            lambda user: SimulatorPathChecker(
                executor=executor,
                user=user,
                timeout_seconds=config.command_timeout_seconds,
            ),
        )
    return LocalPathChecker(), None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the 107Pilot runtime worker.")
    parser.add_argument("--once", action="store_true", help="Run exactly one worker tick and exit.")
    parser.add_argument(
        "--until-idle",
        action="store_true",
        help="Run until one idle tick or max ticks.",
    )
    parser.add_argument("--max-ticks", type=int, default=60)
    args = parser.parse_args(argv)

    worker_service = build_worker_service(config_from_env())
    if args.once:
        result = worker_service.run_once()
    elif args.until_idle:
        result = worker_service.run_ticks(max_ticks=args.max_ticks, stop_when_idle=True)
    else:
        stop_event = Event()

        def request_stop(_signum: int, _frame: object) -> None:
            stop_event.set()

        signal.signal(signal.SIGTERM, request_stop)
        signal.signal(signal.SIGINT, request_stop)
        worker_service.run_forever(stop_event=stop_event)
        return 0

    print(_tick_summary(result))
    return (
        1
        if (
            result.errors
            or result.task_errors
            or result.diagnosis_errors
            or result.submission_errors
            or result.agent_execution_errors
            or worker_service.last_remediation_errors
        )
        else 0
    )


def _build_backend_and_task_handler(
    config: WorkerServiceConfig,
    store: RunStore,
    evidence_store: EvidenceStore,
) -> tuple[SlurmBackend, CollectionTaskHandler | None, TokenMintingRestBackend | None]:
    if config.backend == "in-memory":
        return InMemorySlurmBackend(), None, None
    if config.backend == "demo":
        return (
            DemoSlurmBackend(),
            DemoEvidenceCollector(store=evidence_store, run_store=store),
            None,
        )
    if config.backend == "rest-native":
        transport = UrllibHttpTransport(
            base_url=config.slurmrestd_url,
            timeout_seconds=config.command_timeout_seconds,
            auth_style=config.rest_auth_style,
            slurm_username=config.slurm_username,
        )
        inner = RestNativeSlurmBackend(
            transport=transport,
            api_version=config.slurm_api_version,
            token=config.slurm_token,
        )
        if config.rest_token_provider_enabled and _has_compose_paths(config):
            provider = SimulatorRestTokenProvider(
                executor=DockerComposeExecutor(_compose_target(config))
            )
            wrapper = TokenMintingRestBackend(inner=inner, provider=provider)
            return wrapper, None, wrapper
        return inner, None, None
    if config.backend == "docker-compose-command":
        if (
            config.compose_file is None
            or config.compose_env_file is None
            or config.compose_workdir is None
        ):
            raise ValueError("docker-compose-command backend requires compose paths")
        compose_executor: SimulatorExecutor = DockerComposeExecutor(
            DockerComposeTarget(
                compose_file=config.compose_file,
                env_file=config.compose_env_file,
                workdir=config.compose_workdir,
                service=config.compose_service,
            )
        )
        return (
            DockerSimulatorCommandBackend(
                executor=compose_executor,
                allowed_roots=list(config.allowed_roots),
                timeout_seconds=config.command_timeout_seconds,
            ),
            DockerSlurmEvidenceCollector(
                store=evidence_store,
                executor=compose_executor,
                allowed_roots=list(config.allowed_roots),
                run_store=store,
                evidence_transport=_build_evidence_transport(config),
                timeout_seconds=config.command_timeout_seconds,
            ),
            None,
        )
    if config.backend == "command-gateway":
        gateway_executor: SimulatorExecutor = HttpCommandGatewayExecutor(
            base_url=config.command_gateway_url,
            token=config.command_gateway_token,
            timeout_seconds=config.command_timeout_seconds,
        )
        return (
            DockerSimulatorCommandBackend(
                executor=gateway_executor,
                allowed_roots=list(config.allowed_roots),
                timeout_seconds=config.command_timeout_seconds,
            ),
            DockerSlurmEvidenceCollector(
                store=evidence_store,
                executor=gateway_executor,
                allowed_roots=list(config.allowed_roots),
                run_store=store,
                evidence_transport=_build_evidence_transport(config),
                timeout_seconds=config.command_timeout_seconds,
            ),
            None,
        )
    raise ValueError(f"unsupported worker backend: {config.backend}")


def _has_compose_paths(config: WorkerServiceConfig) -> bool:
    return all(
        path is not None
        for path in (config.compose_file, config.compose_env_file, config.compose_workdir)
    )


def _compose_target(config: WorkerServiceConfig) -> DockerComposeTarget:
    if not _has_compose_paths(config):
        raise ValueError("rest-native token provider requires compose paths")
    return DockerComposeTarget(
        compose_file=config.compose_file,  # type: ignore[arg-type]
        env_file=config.compose_env_file,  # type: ignore[arg-type]
        workdir=config.compose_workdir,  # type: ignore[arg-type]
        service=config.compose_service,
    )


def _worker_shared_roots(config: WorkerServiceConfig) -> tuple[str, ...]:
    from pilot107.core.platform import docker_sim_capability_profile

    return docker_sim_capability_profile(slurm_rest_url=config.slurmrestd_url).shared_roots


def _worker_local_roots(config: WorkerServiceConfig) -> tuple[str, ...]:
    from pilot107.core.platform import docker_sim_capability_profile

    return docker_sim_capability_profile(slurm_rest_url=config.slurmrestd_url).local_roots


def _build_evidence_transport(config: WorkerServiceConfig) -> EvidenceTransport | None:
    if not config.enable_docker_volume_evidence_transport:
        return None
    root_paths = [Path(root) for root in config.allowed_roots]
    if not root_paths or not all(path.exists() for path in root_paths):
        return None
    allowed_roots: list[str | Path] = list(root_paths)
    if config.backend in {"docker-compose-command", "command-gateway"}:
        return DockerVolumeEvidenceTransport(allowed_roots=allowed_roots)
    return AuthorizedFilesystemEvidenceTransport(allowed_roots=allowed_roots)


def _merge_tick_results(left: WorkerTickResult, right: WorkerTickResult) -> WorkerTickResult:
    return WorkerTickResult(
        checked=left.checked + right.checked,
        terminal=left.terminal + right.terminal,
        errors=[*left.errors, *right.errors],
        tasks_checked=left.tasks_checked + right.tasks_checked,
        tasks_succeeded=left.tasks_succeeded + right.tasks_succeeded,
        task_errors=[*left.task_errors, *right.task_errors],
        diagnoses_checked=left.diagnoses_checked + right.diagnoses_checked,
        diagnoses_succeeded=left.diagnoses_succeeded + right.diagnoses_succeeded,
        diagnosis_errors=[*left.diagnosis_errors, *right.diagnosis_errors],
        submissions_checked=left.submissions_checked + right.submissions_checked,
        submissions_succeeded=left.submissions_succeeded + right.submissions_succeeded,
        submission_errors=[*left.submission_errors, *right.submission_errors],
        agent_executions_checked=(
            left.agent_executions_checked + right.agent_executions_checked
        ),
        agent_executions_succeeded=(
            left.agent_executions_succeeded + right.agent_executions_succeeded
        ),
        agent_execution_errors=[
            *left.agent_execution_errors,
            *right.agent_execution_errors,
        ],
    )


def _tick_summary(result: WorkerTickResult) -> str:
    return (
        "worker tick "
        f"checked={result.checked} terminal={result.terminal} "
        f"tasks={result.tasks_succeeded}/{result.tasks_checked} "
        f"diagnoses={result.diagnoses_succeeded}/{result.diagnoses_checked} "
        f"submissions={result.submissions_succeeded}/{result.submissions_checked} "
        f"errors={len(result.errors)} task_errors={len(result.task_errors)} "
        f"diagnosis_errors={len(result.diagnosis_errors)} "
        f"submission_errors={len(result.submission_errors)}"
        f" agent_executions={result.agent_executions_succeeded}/"
        f"{result.agent_executions_checked} "
        f"agent_execution_errors={len(result.agent_execution_errors)}"
    )


def _path(values: Mapping[str, str], name: str, default: Path) -> Path:
    value = values.get(name)
    return Path(value).expanduser() if value else default


def _optional_path(values: Mapping[str, str], name: str, default: Path) -> Path | None:
    value = values.get(name)
    if value is None:
        return default
    if value.strip() == "":
        return None
    return Path(value).expanduser()


def _int(values: Mapping[str, str], name: str, default: int) -> int:
    value = values.get(name)
    return int(value) if value else default


def _float(values: Mapping[str, str], name: str, default: float) -> float:
    value = values.get(name)
    return float(value) if value else default


def _bool(values: Mapping[str, str], name: str, default: bool) -> bool:
    value = values.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _split_csv(value: str) -> list[str]:
    items = [item.strip() for item in value.split(",")]
    return [item for item in items if item]


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
