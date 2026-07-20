"""Service builder for the Phase 0A HTTP API."""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pilot107.adapters.rest_token import (
    SimulatorRestTokenProvider,
    TokenValidityProbe,
)
from pilot107.adapters.rest_token_backend import (
    DEFAULT_JOB_NAME_MARKER,
    TokenMintingRestBackend,
)
from pilot107.adapters.slurm import (
    CommandSubmitBackend,
    DemoSlurmBackend,
    DockerComposeExecutor,
    DockerComposeTarget,
    DockerSimulatorCommandBackend,
    HttpCommandGatewayExecutor,
    InMemorySlurmBackend,
    RestAuthStyle,
    RestNativeSlurmBackend,
    SimulatorPathChecker,
    SlurmBackend,
    UrllibHttpTransport,
)
from pilot107.adapters.slurmrest_snapshot import SlurmrestSnapshotCollector
from pilot107.api.evidence_query import EvidenceQueryService
from pilot107.api.http_app import Pilot107HttpApi
from pilot107.api.metrics import ControlPlaneMetrics
from pilot107.core.agent import AgentExplainService, OpenAICompatibleLLMProvider
from pilot107.core.contracts import ContractService, ContractStore, RecipeCatalog
from pilot107.core.control_repository import ControlRepository
from pilot107.core.control_repository_factory import build_control_repository
from pilot107.core.evidence_binding import EvidenceBinder
from pilot107.core.identity import is_safe_username
from pilot107.core.platform import (
    CapabilityProfile,
    docker_sim_capability_profile,
    load_capability_profile,
)
from pilot107.core.platform_snapshot import ObservationSourceType
from pilot107.core.platform_snapshot_store import PlatformSnapshotStore
from pilot107.core.preflight import LocalPathChecker
from pilot107.core.proxy_auth import load_proxy_hmac_secret
from pilot107.core.run_service import RunService
from pilot107.core.run_store import RunStore
from pilot107.core.template_market import TemplateMarketStore
from pilot107.core.template_market_seed import seed_preset_recipes
from pilot107.core.template_policy import (
    TemplatePublicationGate,
    TemplateRoleDirectory,
)
from pilot107.core.template_verification import TemplateVerificationService
from pilot107.core.user_entitlement_store import UserEntitlementStore
from pilot107.services.platform_snapshot_freshness import SnapshotCollectionMonitor
from pilot107.worker.capsule import RawCapsuleService
from pilot107.worker.evidence import EvidenceStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ApiServiceConfig:
    db_path: Path
    evidence_root: Path
    capsule_root: Path
    control_postgres_dsn: str | None = field(default=None, repr=False)
    backend: str = "none"
    allowed_roots: tuple[str, ...] = ("/public/home/alice",)
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
    slurm_token_refresh_margin_seconds: int = 60
    workdir_preflight_enabled: bool = True
    idempotency_reconcile_enabled: bool = True
    auth_required: bool = False
    trusted_user_header: str = "X-Pilot107-User"
    proxy_hmac_secret: bytes | None = field(default=None, repr=False)
    proxy_signature_max_age_seconds: int = 30
    max_request_body_bytes: int = 2 * 1024 * 1024
    max_response_body_bytes: int = 8 * 1024 * 1024
    rate_limit_requests: int = 600
    rate_limit_window_seconds: int = 60
    contract_profile: str = "generic"
    capability_profile_path: Path | None = None
    allow_gpu_recipes: bool = True
    llm_base_url: str | None = None
    llm_api_key: str | None = None
    llm_model: str | None = None
    llm_timeout_seconds: float = 20.0
    llm_max_tokens: int = 700
    llm_structured_output_mode: str = "prompt_json"
    llm_max_attempts: int = 2
    worker_metrics_root: Path | None = None
    template_reviewers: frozenset[str] = frozenset()
    template_admins: frozenset[str] = frozenset()
    template_course_instructors: Mapping[str, frozenset[str]] = field(default_factory=dict)
    template_course_tas: Mapping[str, frozenset[str]] = field(default_factory=dict)
    template_course_members: Mapping[str, frozenset[str]] = field(default_factory=dict)
    template_verified_container_digests: frozenset[str] = frozenset()
    template_verification_environment: str | None = None


def config_from_env(
    env: Mapping[str, str] | None = None,
    *,
    project_root: Path | None = None,
) -> ApiServiceConfig:
    values = os.environ if env is None else env
    root = project_root or Path(__file__).resolve().parents[3]
    compose_workdir = _path(values, "PILOT107_COMPOSE_WORKDIR", root / "simulator" / "compose")
    runtime_dir = root / "data" / "phase0"
    return ApiServiceConfig(
        db_path=_path(values, "PILOT107_DB_PATH", runtime_dir / "pilot107.db"),
        evidence_root=_path(values, "PILOT107_EVIDENCE_ROOT", runtime_dir / "evidence"),
        capsule_root=_path(values, "PILOT107_CAPSULE_ROOT", runtime_dir / "capsules"),
        control_postgres_dsn=values.get("PILOT107_CONTROL_POSTGRES_DSN") or None,
        backend=values.get("PILOT107_API_BACKEND", "none"),
        allowed_roots=tuple(_split_csv(values.get("PILOT107_ALLOWED_ROOTS", "/public/home/alice"))),
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
        slurm_token_refresh_margin_seconds=_int(
            values, "PILOT107_SLURM_TOKEN_REFRESH_MARGIN_SECONDS", 60
        ),
        workdir_preflight_enabled=_bool(values, "PILOT107_WORKDIR_PREFLIGHT", True),
        idempotency_reconcile_enabled=_bool(values, "PILOT107_IDEMPOTENCY_RECONCILE", True),
        auth_required=_bool(values, "PILOT107_AUTH_REQUIRED", False),
        trusted_user_header=values.get("PILOT107_TRUSTED_USER_HEADER", "X-Pilot107-User"),
        proxy_hmac_secret=load_proxy_hmac_secret(
            secret=values.get("PILOT107_PROXY_HMAC_SECRET"),
            secret_file=values.get("PILOT107_PROXY_HMAC_SECRET_FILE"),
        ),
        proxy_signature_max_age_seconds=_int(
            values, "PILOT107_PROXY_SIGNATURE_MAX_AGE_SECONDS", 30
        ),
        max_request_body_bytes=_int(values, "PILOT107_MAX_REQUEST_BODY_BYTES", 2 * 1024 * 1024),
        max_response_body_bytes=_int(values, "PILOT107_MAX_RESPONSE_BODY_BYTES", 8 * 1024 * 1024),
        rate_limit_requests=_int(values, "PILOT107_RATE_LIMIT_REQUESTS", 600),
        rate_limit_window_seconds=_int(values, "PILOT107_RATE_LIMIT_WINDOW_SECONDS", 60),
        contract_profile=values.get("PILOT107_CONTRACT_PROFILE", "generic"),
        capability_profile_path=_optional_path(values, "PILOT107_CAPABILITY_PROFILE_PATH"),
        allow_gpu_recipes=_bool(values, "PILOT107_ALLOW_GPU_RECIPES", True),
        llm_base_url=values.get("PILOT107_LLM_BASE_URL") or None,
        llm_api_key=values.get("PILOT107_LLM_API_KEY") or None,
        llm_model=values.get("PILOT107_LLM_MODEL") or None,
        llm_timeout_seconds=_float(values, "PILOT107_LLM_TIMEOUT_SECONDS", 20.0),
        llm_max_tokens=_int(values, "PILOT107_LLM_MAX_TOKENS", 700),
        llm_structured_output_mode=values.get("PILOT107_LLM_STRUCTURED_OUTPUT_MODE", "prompt_json"),
        llm_max_attempts=_int(values, "PILOT107_LLM_MAX_ATTEMPTS", 2),
        worker_metrics_root=_optional_path(
            values,
            "PILOT107_WORKER_METRICS_ROOT",
            runtime_dir / "worker-metrics",
        ),
        template_reviewers=frozenset(
            _validated_usernames(values.get("PILOT107_TEMPLATE_REVIEWERS", ""))
        ),
        template_admins=frozenset(_validated_usernames(values.get("PILOT107_TEMPLATE_ADMINS", ""))),
        template_course_instructors=_scoped_memberships(
            values.get("PILOT107_TEMPLATE_COURSE_INSTRUCTORS", "")
        ),
        template_course_tas=_scoped_memberships(values.get("PILOT107_TEMPLATE_COURSE_TAS", "")),
        template_course_members=_scoped_memberships(
            values.get("PILOT107_TEMPLATE_COURSE_MEMBERS", "")
        ),
        template_verified_container_digests=frozenset(
            _validated_container_digests(
                values.get("PILOT107_TEMPLATE_VERIFIED_CONTAINER_DIGESTS", "")
            )
        ),
        template_verification_environment=_verification_environment(
            values.get("PILOT107_TEMPLATE_VERIFICATION_ENVIRONMENT")
        ),
    )


def build_api_service(config: ApiServiceConfig) -> Pilot107HttpApi:
    store = RunStore(config.db_path)
    control_repository = build_control_repository(
        sqlite_path=config.db_path,
        postgres_dsn=config.control_postgres_dsn,
    )
    metrics = ControlPlaneMetrics(
        control_repository=control_repository,
        worker_metrics_root=config.worker_metrics_root or config.db_path.parent / "worker-metrics",
    )
    contract_store = ContractStore(config.db_path)
    capability_profile = _build_capability_profile(config)
    partition_qos = _contract_partition_qos(config.contract_profile, capability_profile)
    catalog = RecipeCatalog(
        store=contract_store,
        allow_gpu=config.allow_gpu_recipes,
        partition_qos=partition_qos,
    )
    run_service, token_validity_probe = _build_run_service_and_probe(
        config, store, control_repository, contract_store, config.evidence_root
    )
    platform_snapshot_store = PlatformSnapshotStore(config.db_path)
    user_entitlement_store = UserEntitlementStore(config.db_path)
    contract_service = ContractService(
        catalog=catalog,
        store=contract_store,
        partition_qos=partition_qos,
        qos_limits=capability_profile.qos_limits(),
        platform_snapshot_store=platform_snapshot_store,
        user_entitlement_store=user_entitlement_store,
    )
    template_market_store = TemplateMarketStore(
        config.db_path,
        publication_gate=TemplatePublicationGate(
            contract_service,
            verified_container_digests=config.template_verified_container_digests,
        ),
        contract_service=contract_service,
    )
    template_role_directory = TemplateRoleDirectory(
        reviewers=config.template_reviewers,
        admins=config.template_admins,
        course_instructors=config.template_course_instructors,
        course_tas=config.template_course_tas,
        course_members=config.template_course_members,
    )
    template_verification_service = (
        None
        if config.template_verification_environment is None
        else TemplateVerificationService(
            template_store=template_market_store,
            run_store=store,
            environment=config.template_verification_environment,
            capsule_root=config.capsule_root,
        )
    )
    # --- A-1: Slurm REST snapshot auto-collect (startup + background refresh) ---
    snapshot_transport = UrllibHttpTransport(
        base_url=config.slurmrestd_url,
        timeout_seconds=5.0,
    )
    snapshot_collector = SlurmrestSnapshotCollector(
        transport=snapshot_transport,
        api_version="v0.0.41",
        token=config.slurm_token,
    )
    # Per-process monitor: records each collection outcome so the readiness
    # check can surface a stale / failing collector as DEGRADED without
    # blocking deploy. State is per-process (no leader election); each API
    # process reports its own last collection.
    snapshot_freshness_monitor = SnapshotCollectionMonitor()

    def _collect_and_store_snapshot() -> None:
        try:
            snapshot = snapshot_collector.collect()
        except Exception as exc:  # noqa: BLE001 - startup must not crash on snapshot failure
            # Stop silently swallowing: record + log so operators can see that
            # collection stopped (UI would otherwise show stale platform facts).
            snapshot_freshness_monitor.record_failure(exc)
            logger.warning(
                "slurmrestd snapshot collection failed: %s: %s",
                type(exc).__name__,
                exc,
                exc_info=False,
            )
            return
        try:
            platform_snapshot_store.create(
                owner=config.slurm_username or "pilot107-system",
                snapshot=snapshot,
                source_type=ObservationSourceType.REST,
                source_name="slurmrestd-auto",
                expires_at=(datetime.now(UTC) + timedelta(seconds=300)).isoformat(),
            )
        except Exception as exc:  # noqa: BLE001 - startup must not crash on store failure
            snapshot_freshness_monitor.record_failure(exc)
            logger.warning(
                "platform snapshot store failed: %s: %s",
                type(exc).__name__,
                exc,
                exc_info=False,
            )
            return
        snapshot_freshness_monitor.record_success()

    # Initial collection at startup (non-blocking on failure)
    _collect_and_store_snapshot()

    # Background refresh thread (daemon, 5min interval)
    def _refresh_loop() -> None:
        while True:
            threading.Event().wait(timeout=300.0)
            _collect_and_store_snapshot()

    refresh_thread = threading.Thread(
        target=_refresh_loop, name="slurmrest-snapshot-refresh", daemon=True
    )
    refresh_thread.start()

    # --- A-2: Template market seed (idempotent, fault-tolerant) ---
    try:
        seed_report = seed_preset_recipes(
            catalog=catalog,
            store=template_market_store,
            role_directory=template_role_directory,
        )
        if seed_report.errors:
            print(
                f"[phase-a-seed] published={seed_report.published} "
                f"skipped={seed_report.skipped} gate_blocked={seed_report.gate_blocked} "
                f"errors={seed_report.errors}",
                flush=True,
            )
        else:
            print(
                f"[phase-a-seed] published={seed_report.published} "
                f"skipped={seed_report.skipped} gate_blocked={seed_report.gate_blocked}",
                flush=True,
            )
    except Exception as exc:  # noqa: BLE001 - startup must not crash on seed failure
        print(f"[phase-a-seed] FATAL: {type(exc).__name__}: {exc}", flush=True)

    # P1-1 (round 5 audit): inject contract_store + a shared EvidenceStore into
    # Pilot107HttpApi so the API process's RemediationService can perform strict
    # expected-output verification (same as the Worker path). Without this, API
    # manual /advance falls back to legacy VERIFIED_SUCCESS without checking
    # expected outputs. Reuse one EvidenceStore instance for evidence_query,
    # capsule_service, and the remediation path to avoid divergent roots.
    shared_evidence_store = EvidenceStore(config.evidence_root)

    return Pilot107HttpApi(
        store=store,
        control_repository=control_repository,
        worker_metrics_root=config.worker_metrics_root,
        metrics=metrics,
        evidence_query=EvidenceQueryService(
            store=store,
            evidence_store=shared_evidence_store,
        ),
        capsule_service=RawCapsuleService(
            store=store,
            evidence_store=shared_evidence_store,
            capsule_root=config.capsule_root,
            creator="pilot107-api",
        ),
        run_service=run_service,
        recipe_catalog=catalog,
        contract_service=contract_service,
        contract_store=contract_store,
        evidence_store=shared_evidence_store,
        template_market_store=template_market_store,
        template_role_directory=template_role_directory,
        template_verification_service=template_verification_service,
        capability_profile=capability_profile,
        platform_snapshot_store=platform_snapshot_store,
        user_entitlement_store=user_entitlement_store,
        agent_explain_service=AgentExplainService(
            store=store,
            llm_provider=_build_llm_provider(config, observer=metrics),
            evidence_binder=EvidenceBinder(
                store=store,
                evidence_root=config.evidence_root,
            ),
        ),
        auth_required=config.auth_required,
        trusted_user_header=config.trusted_user_header,
        proxy_hmac_secret=config.proxy_hmac_secret,
        proxy_signature_max_age_seconds=config.proxy_signature_max_age_seconds,
        max_request_body_bytes=config.max_request_body_bytes,
        max_response_body_bytes=config.max_response_body_bytes,
        rate_limit_requests=config.rate_limit_requests,
        rate_limit_window_seconds=config.rate_limit_window_seconds,
    )


def _build_run_service(
    config: ApiServiceConfig,
    store: RunStore,
    control_repository: ControlRepository,
    contract_store: ContractStore | None = None,
    evidence_root: Path | None = None,
    *,
    rest_token_provider: SimulatorRestTokenProvider | None = None,
) -> RunService | None:
    evidence_store: EvidenceStore | None = (
        EvidenceStore(evidence_root) if evidence_root is not None else None
    )
    baseline_kwargs: dict[str, Any] = {
        "contract_store": contract_store,
        "evidence_store": evidence_store,
    }
    if config.backend == "none":
        return None
    if config.backend == "in-memory":
        return RunService(
            store=store,
            backend=InMemorySlurmBackend(),
            control_repository=control_repository,
            **baseline_kwargs,
            **_run_flags(config, backend_kind="in-memory"),
        )
    if config.backend == "demo":
        return RunService(
            store=store,
            backend=DemoSlurmBackend(),
            control_repository=control_repository,
            **baseline_kwargs,
            **_run_flags(config, backend_kind="demo"),
        )
    if config.backend == "rest-native":
        return RunService(
            store=store,
            control_repository=control_repository,
            **baseline_kwargs,
            **_rest_native_kwargs(config, provider=rest_token_provider),
        )
    if config.backend == "command":
        return RunService(
            store=store,
            control_repository=control_repository,
            backend=CommandSubmitBackend(
                allowed_roots=[Path(root) for root in config.allowed_roots],
                timeout_seconds=config.command_timeout_seconds,
            ),
            **baseline_kwargs,
            **_run_flags(config, backend_kind="command"),
        )
    if config.backend == "docker-compose-command":
        if (
            config.compose_file is None
            or config.compose_env_file is None
            or config.compose_workdir is None
        ):
            raise ValueError("docker-compose-command backend requires compose paths")
        executor = DockerComposeExecutor(
            DockerComposeTarget(
                compose_file=config.compose_file,
                env_file=config.compose_env_file,
                workdir=config.compose_workdir,
                service=config.compose_service,
            )
        )
        return RunService(
            store=store,
            control_repository=control_repository,
            backend=DockerSimulatorCommandBackend(
                executor=executor,
                allowed_roots=list(config.allowed_roots),
                timeout_seconds=config.command_timeout_seconds,
            ),
            baseline_executor=executor,
            **baseline_kwargs,
            **_run_flags(config, backend_kind="docker-compose-command"),
        )
    if config.backend == "command-gateway":
        gateway_executor = HttpCommandGatewayExecutor(
            base_url=config.command_gateway_url,
            token=config.command_gateway_token,
            timeout_seconds=config.command_timeout_seconds,
        )
        return RunService(
            store=store,
            control_repository=control_repository,
            backend=DockerSimulatorCommandBackend(
                executor=gateway_executor,
                allowed_roots=list(config.allowed_roots),
                timeout_seconds=config.command_timeout_seconds,
            ),
            baseline_executor=gateway_executor,
            **baseline_kwargs,
            **_run_flags(
                config,
                backend_kind="command-gateway",
                gateway_executor=gateway_executor,
            ),
        )
    raise ValueError(f"unsupported API backend: {config.backend}")


def _build_run_service_and_probe(
    config: ApiServiceConfig,
    store: RunStore,
    control_repository: ControlRepository,
    contract_store: ContractStore | None = None,
    evidence_root: Path | None = None,
) -> tuple[RunService | None, TokenValidityProbe | None]:
    """Build the RunService and the token-validity probe together.

    For rest-native with the token-provider path enabled, the same
    ``SimulatorRestTokenProvider`` is shared between the backend (which mints
    per REST call) and the readiness probe (which reports the cached token's
    remaining lifespan). ``SimulatorRestTokenProvider`` directly implements the
    ``TokenValidityProbe`` protocol via its ``validity()`` method. For all other
    backends the probe is ``None``.
    """
    if config.backend != "rest-native" or not config.rest_token_provider_enabled:
        return (
            _build_run_service(
                config, store, control_repository, contract_store, evidence_root
            ),
            None,
        )
    provider: TokenValidityProbe = SimulatorRestTokenProvider(
        executor=DockerComposeExecutor(_compose_target(config)),
        refresh_margin_seconds=config.slurm_token_refresh_margin_seconds,
    )
    run_service = _build_run_service(
        config,
        store,
        control_repository,
        contract_store,
        evidence_root,
        rest_token_provider=provider,  # type: ignore[arg-type]
    )
    return run_service, provider


def _run_flags(
    config: ApiServiceConfig,
    *,
    backend_kind: str,
    gateway_executor: HttpCommandGatewayExecutor | None = None,
) -> dict[str, Any]:
    """RunService kwargs for non-REST backends (preflight + reconcile flags).

    Reconciliation only applies to REST (needs a ``ReconcileBackend``); for
    command/in-memory/demo backends ``reconcile_backend`` stays ``None`` and
    ``idempotency_reconcile_enabled`` is effectively inert.

    The FS path checker is injected only for backends that touch a real
    filesystem (command, docker-compose-command, command-gateway, rest-native).
    In-memory and demo backends never stat the workdir, so pure-path preflight
    suffices and tests using non-existent edge-case workdirs are not blocked
    by WORKDIR_PARENT_NOT_FOUND.
    """
    use_local_fs_checker = config.workdir_preflight_enabled and backend_kind in {
        "command",
        "docker-compose-command",
    }
    flags: dict[str, Any] = {
        "workdir_preflight_enabled": config.workdir_preflight_enabled,
        "preflight_allowed_roots": config.allowed_roots,
        "preflight_shared_roots": _capability_shared_roots(config),
        "preflight_local_roots": _capability_local_roots(config),
        "preflight_path_checker": LocalPathChecker() if use_local_fs_checker else None,
        "idempotency_reconcile_enabled": False,
    }
    if config.workdir_preflight_enabled and backend_kind == "command-gateway":
        if gateway_executor is None:
            raise ValueError("command-gateway preflight requires its gateway executor")
        flags["preflight_path_checker_factory"] = lambda user: SimulatorPathChecker(
            executor=gateway_executor,
            user=user,
            timeout_seconds=config.command_timeout_seconds,
        )
    return flags


def _rest_native_kwargs(
    config: ApiServiceConfig,
    *,
    provider: SimulatorRestTokenProvider | None = None,
) -> dict[str, Any]:
    """Build the REST-native backend plus wiring for preflight + reconcile.

    ``provider`` may be supplied by the caller so the same
    :class:`SimulatorRestTokenProvider` instance is shared between the
    :class:`TokenMintingRestBackend` (which mints per REST call) and the
    readiness token-validity probe (which reports the provider's cache). When
    ``None`` and the provider path is enabled, a fresh provider is constructed
    here (backward-compatible for callers that do not care about the probe).
    """
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
    backend: SlurmBackend
    reconcile_backend = None
    if config.rest_token_provider_enabled and _has_compose_paths(config):
        if provider is None:
            provider = SimulatorRestTokenProvider(
                executor=DockerComposeExecutor(_compose_target(config)),
                refresh_margin_seconds=config.slurm_token_refresh_margin_seconds,
            )
        wrapper = TokenMintingRestBackend(inner=inner, provider=provider)
        backend = wrapper
        reconcile_backend = wrapper
    else:
        # Static-token path (backward compat for probes/tests that inject a
        # pre-minted token via PILOT107_SLURM_TOKEN).
        backend = inner
    return {
        "backend": backend,
        "workdir_preflight_enabled": config.workdir_preflight_enabled,
        "preflight_allowed_roots": config.allowed_roots,
        "preflight_shared_roots": _capability_shared_roots(config),
        "preflight_local_roots": _capability_local_roots(config),
        "preflight_path_checker": LocalPathChecker() if config.workdir_preflight_enabled else None,
        "idempotency_reconcile_enabled": config.idempotency_reconcile_enabled,
        "reconcile_backend": reconcile_backend,
        "job_name_marker": DEFAULT_JOB_NAME_MARKER,
    }


def _has_compose_paths(config: ApiServiceConfig) -> bool:
    return all(
        path is not None
        for path in (config.compose_file, config.compose_env_file, config.compose_workdir)
    )


def _compose_target(config: ApiServiceConfig) -> DockerComposeTarget:
    if not _has_compose_paths(config):
        raise ValueError("rest-native token provider requires compose paths")
    return DockerComposeTarget(
        compose_file=config.compose_file,  # type: ignore[arg-type]
        env_file=config.compose_env_file,  # type: ignore[arg-type]
        workdir=config.compose_workdir,  # type: ignore[arg-type]
        service=config.compose_service,
    )


def _capability_shared_roots(config: ApiServiceConfig) -> tuple[str, ...]:
    profile = _build_capability_profile(config)
    return profile.shared_roots


def _capability_local_roots(config: ApiServiceConfig) -> tuple[str, ...]:
    profile = _build_capability_profile(config)
    return profile.local_roots


def _build_capability_profile(config: ApiServiceConfig) -> CapabilityProfile:
    if config.capability_profile_path is not None:
        return load_capability_profile(config.capability_profile_path)
    return docker_sim_capability_profile(slurm_rest_url=config.slurmrestd_url)


def _contract_partition_qos(
    profile: str,
    capability_profile: CapabilityProfile,
) -> dict[str, tuple[str, ...]] | None:
    if profile == "generic":
        return None
    if profile in {"real107-sim", "cpu-only"}:
        return capability_profile.partition_qos()
    raise ValueError(f"unknown contract profile: {profile}")


def _build_llm_provider(
    config: ApiServiceConfig,
    *,
    observer: ControlPlaneMetrics | None = None,
) -> OpenAICompatibleLLMProvider | None:
    if not (config.llm_base_url and config.llm_model):
        return None
    return OpenAICompatibleLLMProvider(
        base_url=config.llm_base_url,
        api_key=config.llm_api_key,
        model=config.llm_model,
        timeout_seconds=config.llm_timeout_seconds,
        max_tokens=config.llm_max_tokens,
        structured_output_mode=config.llm_structured_output_mode,
        max_attempts=config.llm_max_attempts,
        observer=observer,
    )


def _path(values: Mapping[str, str], name: str, default: Path) -> Path:
    value = values.get(name)
    return Path(value).expanduser() if value else default


def _optional_path(
    values: Mapping[str, str],
    name: str,
    default: Path | None = None,
) -> Path | None:
    value = values.get(name)
    if value is None:
        return default
    return Path(value).expanduser() if value else None


def _float(values: Mapping[str, str], name: str, default: float) -> float:
    value = values.get(name)
    return float(value) if value else default


def _int(values: Mapping[str, str], name: str, default: int) -> int:
    value = values.get(name)
    return int(value) if value else default


def _bool(values: Mapping[str, str], name: str, default: bool) -> bool:
    value = values.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _split_csv(value: str) -> list[str]:
    items = [item.strip() for item in value.split(",")]
    return [item for item in items if item]


def _validated_usernames(value: str) -> list[str]:
    usernames = _split_csv(value)
    invalid = [username for username in usernames if not is_safe_username(username)]
    if invalid:
        raise ValueError(f"invalid template role username: {invalid[0]}")
    return usernames


def _scoped_memberships(value: str) -> dict[str, frozenset[str]]:
    resolved: dict[str, set[str]] = {}
    for item in _split_csv(value):
        scope, separator, username = item.partition("=")
        scope = scope.strip()
        username = username.strip()
        if separator != "=" or not scope or not is_safe_username(scope):
            raise ValueError(f"invalid template course membership: {item}")
        if not is_safe_username(username):
            raise ValueError(f"invalid template course member: {username}")
        resolved.setdefault(scope, set()).add(username)
    return {scope: frozenset(users) for scope, users in resolved.items()}


def _validated_container_digests(value: str) -> list[str]:
    digests = _split_csv(value)
    for digest in digests:
        algorithm, separator, encoded = digest.partition(":")
        if (
            separator != ":"
            or algorithm != "sha256"
            or len(encoded) != 64
            or any(char not in "0123456789abcdef" for char in encoded)
        ):
            raise ValueError(f"invalid verified container digest: {digest}")
    return digests


def _verification_environment(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    environment = value.strip()
    if environment not in {"docker", "real107_cpu", "real107_gpu"}:
        raise ValueError(f"invalid template verification environment: {environment}")
    return environment
