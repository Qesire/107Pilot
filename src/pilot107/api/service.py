"""Service builder for the Phase 0A HTTP API."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pilot107.adapters.rest_token import SimulatorRestTokenProvider
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
from pilot107.api.evidence_query import EvidenceQueryService
from pilot107.api.http_app import Pilot107HttpApi
from pilot107.core.agent import AgentExplainService, OpenAICompatibleLLMProvider
from pilot107.core.contracts import ContractService, ContractStore, RecipeCatalog
from pilot107.core.control_repository import ControlRepository, SQLiteControlRepository
from pilot107.core.evidence_binding import EvidenceBinder
from pilot107.core.identity import is_safe_username
from pilot107.core.platform import (
    CapabilityProfile,
    docker_sim_capability_profile,
    load_capability_profile,
)
from pilot107.core.platform_snapshot_store import PlatformSnapshotStore
from pilot107.core.preflight import LocalPathChecker
from pilot107.core.run_service import RunService
from pilot107.core.run_store import RunStore
from pilot107.core.template_market import TemplateMarketStore
from pilot107.core.template_policy import (
    TemplatePublicationGate,
    TemplateRoleDirectory,
)
from pilot107.core.template_verification import TemplateVerificationService
from pilot107.core.user_entitlement_store import UserEntitlementStore
from pilot107.worker.capsule import RawCapsuleService
from pilot107.worker.evidence import EvidenceStore


@dataclass(frozen=True)
class ApiServiceConfig:
    db_path: Path
    evidence_root: Path
    capsule_root: Path
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
    workdir_preflight_enabled: bool = True
    idempotency_reconcile_enabled: bool = True
    auth_required: bool = False
    trusted_user_header: str = "X-Pilot107-User"
    contract_profile: str = "generic"
    capability_profile_path: Path | None = None
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
        workdir_preflight_enabled=_bool(values, "PILOT107_WORKDIR_PREFLIGHT", True),
        idempotency_reconcile_enabled=_bool(
            values, "PILOT107_IDEMPOTENCY_RECONCILE", True
        ),
        auth_required=_bool(values, "PILOT107_AUTH_REQUIRED", False),
        trusted_user_header=values.get("PILOT107_TRUSTED_USER_HEADER", "X-Pilot107-User"),
        contract_profile=values.get("PILOT107_CONTRACT_PROFILE", "generic"),
        capability_profile_path=_optional_path(values, "PILOT107_CAPABILITY_PROFILE_PATH"),
        llm_base_url=values.get("PILOT107_LLM_BASE_URL") or None,
        llm_api_key=values.get("PILOT107_LLM_API_KEY") or None,
        llm_model=values.get("PILOT107_LLM_MODEL") or None,
        llm_timeout_seconds=_float(values, "PILOT107_LLM_TIMEOUT_SECONDS", 20.0),
        llm_max_tokens=_int(values, "PILOT107_LLM_MAX_TOKENS", 700),
        llm_structured_output_mode=values.get(
            "PILOT107_LLM_STRUCTURED_OUTPUT_MODE", "prompt_json"
        ),
        llm_max_attempts=_int(values, "PILOT107_LLM_MAX_ATTEMPTS", 2),
        worker_metrics_root=_optional_path(
            values,
            "PILOT107_WORKER_METRICS_ROOT",
            runtime_dir / "worker-metrics",
        ),
        template_reviewers=frozenset(
            _validated_usernames(values.get("PILOT107_TEMPLATE_REVIEWERS", ""))
        ),
        template_admins=frozenset(
            _validated_usernames(values.get("PILOT107_TEMPLATE_ADMINS", ""))
        ),
        template_course_instructors=_scoped_memberships(
            values.get("PILOT107_TEMPLATE_COURSE_INSTRUCTORS", "")
        ),
        template_course_tas=_scoped_memberships(
            values.get("PILOT107_TEMPLATE_COURSE_TAS", "")
        ),
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
    control_repository = SQLiteControlRepository(config.db_path)
    contract_store = ContractStore(config.db_path)
    catalog = RecipeCatalog(store=contract_store)
    run_service = _build_run_service(config, store, control_repository)
    capability_profile = _build_capability_profile(config)
    platform_snapshot_store = PlatformSnapshotStore(config.db_path)
    user_entitlement_store = UserEntitlementStore(config.db_path)
    contract_service = ContractService(
        catalog=catalog,
        store=contract_store,
        partition_qos=_contract_partition_qos(config.contract_profile, capability_profile),
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
    return Pilot107HttpApi(
        store=store,
        control_repository=control_repository,
        worker_metrics_root=config.worker_metrics_root,
        evidence_query=EvidenceQueryService(
            store=store,
            evidence_store=EvidenceStore(config.evidence_root),
        ),
        capsule_service=RawCapsuleService(
            store=store,
            evidence_store=EvidenceStore(config.evidence_root),
            capsule_root=config.capsule_root,
            creator="pilot107-api",
        ),
        run_service=run_service,
        recipe_catalog=catalog,
        contract_service=contract_service,
        template_market_store=template_market_store,
        template_role_directory=template_role_directory,
        template_verification_service=template_verification_service,
        capability_profile=capability_profile,
        platform_snapshot_store=platform_snapshot_store,
        user_entitlement_store=user_entitlement_store,
        agent_explain_service=AgentExplainService(
            store=store,
            llm_provider=_build_llm_provider(config),
            evidence_binder=EvidenceBinder(
                store=store,
                evidence_root=config.evidence_root,
            ),
        ),
        auth_required=config.auth_required,
        trusted_user_header=config.trusted_user_header,
    )


def _build_run_service(
    config: ApiServiceConfig,
    store: RunStore,
    control_repository: ControlRepository,
) -> RunService | None:
    if config.backend == "none":
        return None
    if config.backend == "in-memory":
        return RunService(
            store=store,
            backend=InMemorySlurmBackend(),
            control_repository=control_repository,
            **_run_flags(config, backend_kind="in-memory"),
        )
    if config.backend == "demo":
        return RunService(
            store=store,
            backend=DemoSlurmBackend(),
            control_repository=control_repository,
            **_run_flags(config, backend_kind="demo"),
        )
    if config.backend == "rest-native":
        return RunService(
            store=store,
            control_repository=control_repository,
            **_rest_native_kwargs(config),
        )
    if config.backend == "command":
        return RunService(
            store=store,
            control_repository=control_repository,
            backend=CommandSubmitBackend(
                allowed_roots=[Path(root) for root in config.allowed_roots],
                timeout_seconds=config.command_timeout_seconds,
            ),
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
            **_run_flags(
                config,
                backend_kind="command-gateway",
                gateway_executor=gateway_executor,
            ),
        )
    raise ValueError(f"unsupported API backend: {config.backend}")


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


def _rest_native_kwargs(config: ApiServiceConfig) -> dict[str, Any]:
    """Build the REST-native backend plus wiring for preflight + reconcile."""
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
        provider = SimulatorRestTokenProvider(
            executor=DockerComposeExecutor(_compose_target(config))
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
    if profile == "real107-sim":
        return capability_profile.partition_qos()
    raise ValueError(f"unknown contract profile: {profile}")


def _build_llm_provider(config: ApiServiceConfig) -> OpenAICompatibleLLMProvider | None:
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
