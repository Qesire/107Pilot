"""Minimal stdlib HTTP API for Phase 0A."""

import hashlib
import json
import os
import re
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from enum import StrEnum
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse
from uuid import uuid4

from pilot107.adapters.slurm import SlurmBackendError
from pilot107.api.evidence_query import EvidencePreviewUnavailable, EvidenceQueryService
from pilot107.api.health import ApiHealthService
from pilot107.api.http_types import ApiResponse as ApiResponse
from pilot107.api.metrics import ControlPlaneMetrics, normalize_http_route
from pilot107.api.remediation_routes import RemediationRoutes
from pilot107.api.security import FixedWindowRateLimiter
from pilot107.core.advice import (
    AgentAdviceError,
    AgentAdviceService,
    AgentPolicyEngine,
    advice_payload,
    execution_payload,
)
from pilot107.core.agent import (
    AgentExplainService,
    AgentProviderError,
    OpenAICompatibleLLMProvider,
)
from pilot107.core.contracts import (
    ContractError,
    ContractRecord,
    ContractService,
    ContractStore,
    RecipeCatalog,
    contract_payload,
    contract_schema_payload,
    recipe_summary_payload,
    recipe_version_payload,
    validation_payload,
)
from pilot107.core.control_repository import ControlRepository, SQLiteControlRepository
from pilot107.core.diagnosis import DiagnosisService, KnownErrorRule, load_known_error_rules
from pilot107.core.evidence_binding import EvidenceBinder
from pilot107.core.identity import (
    IdentityResolutionError,
    UserIdentity,
    resolve_trusted_header_identity,
)
from pilot107.core.pagination import (
    CursorError,
    CursorPosition,
    cursor_scope,
    decode_cursor,
    encode_cursor,
)
from pilot107.core.platform import CapabilityProfile, docker_sim_capability_profile
from pilot107.core.platform_preflight import (
    validate_platform_snapshot_resource_plan,
    validate_user_entitlement_resource_plan,
)
from pilot107.core.platform_snapshot import ObservationSourceType, PlatformSnapshotScope
from pilot107.core.platform_snapshot_store import (
    PlatformSnapshotStore,
    SnapshotFreshness,
)
from pilot107.core.proxy_auth import ProxyRequestAuthenticator
from pilot107.core.remediation_store import RemediationStore
from pilot107.core.resources import (
    ArraySpec,
    PreflightFinding,
    PreflightSeverity,
    ResourcePlan,
    validate_resource_plan,
)
from pilot107.core.run_service import (
    RunService,
    RunSubmitRequest,
    SubmissionInProgressError,
    WorkflowDependencyError,
    WorkflowPolicy,
    WorkflowRetryNotReadyError,
)
from pilot107.core.run_store import AgentAdviceRecord, RunEvent, RunRecord, RunStore
from pilot107.core.states import RunState
from pilot107.core.template_market import (
    TemplateMarketError,
    TemplateMarketStore,
    TemplateReleaseRecord,
    TemplateVisibility,
    authorize_template_release,
    template_adoption_payload,
    template_draft_payload,
    template_market_item_payload,
    template_release_payload,
    template_review_payload,
    template_review_queue_payload,
    template_verification_payload,
)
from pilot107.core.template_policy import TemplatePublicationGate, TemplateRoleDirectory
from pilot107.core.template_verification import TemplateVerificationService
from pilot107.core.user_entitlement_store import UserEntitlementStore
from pilot107.services.remediation_service import RemediationService
from pilot107.worker.capsule import CapsuleError, RawCapsuleService
from pilot107.worker.evidence import EvidenceStore, generated_execution_wrapper

_ADVICE_STATES = frozenset(
    {"ready", "needs_input", "no_safe_action", "approved", "rejected", "stale"}
)
_EXECUTION_STATES = frozenset({"executing", "prepared", "submitted", "failed"})
_EVENT_TYPE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_OWNER = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


class Pilot107HttpApi:
    def __init__(
        self,
        *,
        store: RunStore,
        evidence_query: EvidenceQueryService,
        run_service: RunService | None = None,
        contract_service: ContractService | None = None,
        recipe_catalog: RecipeCatalog | None = None,
        capsule_service: RawCapsuleService | None = None,
        diagnosis_service: DiagnosisService | None = None,
        agent_explain_service: AgentExplainService | None = None,
        agent_advice_service: AgentAdviceService | None = None,
        remediation_service: RemediationService | None = None,
        capability_profile: CapabilityProfile | None = None,
        platform_snapshot_store: PlatformSnapshotStore | None = None,
        user_entitlement_store: UserEntitlementStore | None = None,
        template_market_store: TemplateMarketStore | None = None,
        template_role_directory: TemplateRoleDirectory | None = None,
        template_verification_service: TemplateVerificationService | None = None,
        health_service: ApiHealthService | None = None,
        control_repository: ControlRepository | None = None,
        worker_metrics_root: Path | None = None,
        metrics: ControlPlaneMetrics | None = None,
        auth_required: bool = False,
        trusted_user_header: str = "X-Pilot107-User",
        proxy_hmac_secret: bytes | None = None,
        proxy_signature_max_age_seconds: int = 30,
        max_request_body_bytes: int = 2 * 1024 * 1024,
        max_response_body_bytes: int = 8 * 1024 * 1024,
        rate_limit_requests: int = 600,
        rate_limit_window_seconds: int = 60,
    ) -> None:
        self.store = store
        self.evidence_query = evidence_query
        self.control_repository = control_repository or SQLiteControlRepository(store.db_path)
        self.worker_metrics_root = worker_metrics_root or store.db_path.parent / "worker-metrics"
        self.metrics = metrics or ControlPlaneMetrics(
            control_repository=self.control_repository,
            worker_metrics_root=self.worker_metrics_root,
        )
        self.run_service = run_service
        self.contract_service = contract_service
        self.recipe_catalog = recipe_catalog or RecipeCatalog()
        self.capsule_service = capsule_service
        self.diagnosis_service = diagnosis_service or DiagnosisService(store=store)
        self.agent_explain_service = agent_explain_service or AgentExplainService(
            store=store,
            llm_provider=_llm_provider_from_env(observer=self.metrics),
            evidence_binder=EvidenceBinder(
                store=store,
                evidence_root=evidence_query.evidence_store.root,
            ),
        )
        self.agent_advice_service = agent_advice_service or AgentAdviceService(
            store=store,
            explain_service=self.agent_explain_service,
            policy_engine=AgentPolicyEngine(contract_service=contract_service),
            contract_service=contract_service,
            run_service=run_service,
        )
        self.remediation_service = remediation_service or RemediationService(
            run_store=store,
            remediation_store=RemediationStore(store.db_path),
            advice_service=self.agent_advice_service,
        )
        self.remediation_routes = RemediationRoutes(self.remediation_service)
        self.capability_profile = capability_profile or docker_sim_capability_profile()
        self.platform_snapshot_store = platform_snapshot_store
        self.user_entitlement_store = user_entitlement_store
        self.template_market_store = template_market_store
        self.template_role_directory = template_role_directory or TemplateRoleDirectory()
        self.template_verification_service = template_verification_service
        self.health_service = health_service or ApiHealthService(
            store=store,
            evidence_root=evidence_query.evidence_store.root,
            platform_snapshot_store=platform_snapshot_store,
            submission_enabled=run_service is not None,
            llm_enabled=self.agent_explain_service.llm_provider is not None,
            user_entitlement_store=user_entitlement_store,
            worker_health_path=os.environ.get("PILOT107_WORKER_HEALTH_PATH"),
        )
        self.auth_required = auth_required
        self.trusted_user_header = trusted_user_header
        self.proxy_authenticator = (
            ProxyRequestAuthenticator(
                proxy_hmac_secret,
                max_age_seconds=proxy_signature_max_age_seconds,
            )
            if proxy_hmac_secret is not None
            else None
        )
        if (
            min(
                max_request_body_bytes,
                max_response_body_bytes,
                rate_limit_requests,
                rate_limit_window_seconds,
            )
            <= 0
        ):
            raise ValueError("HTTP size and rate limits must be positive")
        self.max_request_body_bytes = max_request_body_bytes
        self.max_response_body_bytes = max_response_body_bytes
        self.rate_limiter = FixedWindowRateLimiter(
            limit=rate_limit_requests,
            window_seconds=rate_limit_window_seconds,
        )

    def handle_get(self, path: str, headers: Mapping[str, str] | None = None) -> ApiResponse:
        request_id = _request_id(headers)
        response = self._proxy_auth_error("GET", path, b"", headers)
        if response is None:
            response = self._handle_get(path, headers=headers)
        return self._finalize_and_trace(
            response,
            method="GET",
            path=path,
            request_id=request_id,
            request_headers=headers,
            enable_etag=True,
        )

    def _handle_verified_get(
        self,
        path: str,
        headers: Mapping[str, str] | None = None,
    ) -> ApiResponse:
        """Handle an internal poll after its transport request was authenticated once."""

        request_id = _request_id(headers)
        return _finalize_response(
            self._handle_get(path, headers=headers),
            request_id=request_id,
            request_headers=headers,
            enable_etag=True,
        )

    def _handle_get(
        self,
        path: str,
        headers: Mapping[str, str] | None = None,
    ) -> ApiResponse:
        parsed = urlparse(path)
        route = parsed.path.rstrip("/") or "/"
        if route == "/healthz":
            return ApiResponse(status=200, payload={"status": "ok"})
        if route in {"/health/live", "/api/v1/health/live"}:
            return ApiResponse(status=200, payload=self.health_service.live_payload())
        if route in {"/health/ready", "/api/v1/health/ready"}:
            ready, payload = self.health_service.ready()
            return ApiResponse(status=200 if ready else 503, payload=payload)
        identity, auth_error = self._resolve_identity(headers)
        if auth_error is not None:
            return auth_error
        params = parse_qs(parsed.query, keep_blank_values=True)

        parts = [unquote(part) for part in route.split("/") if part]
        if len(parts) >= 2 and parts[:2] == ["api", "v1"]:
            parts = parts[2:]
        remediation_response = self.remediation_routes.handle_get(
            parts,
            params=params,
            identity=identity,
        )
        if remediation_response is not None:
            return remediation_response
        if len(parts) == 1 and parts[0] == "recipes":
            return ApiResponse(
                status=200,
                payload={
                    "items": [
                        recipe_summary_payload(summary)
                        for summary in self.recipe_catalog.list_summaries()
                    ]
                },
            )
        if len(parts) == 4 and parts[0] == "recipes" and parts[2] == "versions":
            try:
                recipe = self.recipe_catalog.get(parts[1], parts[3])
            except KeyError:
                return ApiResponse(
                    status=404,
                    payload={
                        "error": {
                            "code": "recipe_not_found",
                            "recipe_id": parts[1],
                            "version": parts[3],
                        }
                    },
                )
            return ApiResponse(status=200, payload=recipe_version_payload(recipe))
        if len(parts) == 2 and parts == ["contracts", "schema"]:
            return ApiResponse(status=200, payload=contract_schema_payload())
        if len(parts) == 1 and parts[0] == "template-drafts":
            return self._list_template_drafts(params=params, identity=identity)
        if len(parts) == 2 and parts[0] == "template-drafts":
            return self._get_template_draft(
                draft_id=parts[1],
                params=params,
                identity=identity,
            )
        if len(parts) == 1 and parts[0] == "template-reviews":
            return self._list_template_reviews(params=params, identity=identity)
        if len(parts) == 1 and parts[0] == "templates":
            return self._list_template_market(params=params, identity=identity)
        if len(parts) == 3 and parts[0] == "templates" and parts[2] == "diff":
            return self._diff_template_releases(
                template_id=parts[1],
                params=params,
                identity=identity,
            )
        if len(parts) == 4 and parts[0] == "templates" and parts[2] == "releases":
            return self._get_template_release(
                template_id=parts[1],
                release_version=parts[3],
                identity=identity,
            )
        if (
            len(parts) == 5
            and parts[0] == "templates"
            and parts[2] == "releases"
            and parts[4] == "verifications"
        ):
            return self._list_template_verifications(
                template_id=parts[1],
                release_version=parts[3],
                params=params,
                identity=identity,
            )
        if len(parts) == 2 and parts == ["platform", "capabilities"]:
            return self._platform_capabilities(params=params, identity=identity)
        if len(parts) == 2 and parts == ["platform", "snapshots"]:
            return self._list_platform_snapshots(params=params, identity=identity)
        if len(parts) == 3 and parts == ["platform", "snapshots", "latest"]:
            return self._latest_platform_snapshot(params=params, identity=identity)
        if len(parts) == 3 and parts[:2] == ["platform", "snapshots"]:
            return self._get_platform_snapshot(
                snapshot_id=parts[2],
                params=params,
                identity=identity,
            )
        if len(parts) == 2 and parts == ["platform", "entitlements"]:
            return self._list_user_entitlements(params=params, identity=identity)
        if len(parts) == 3 and parts == ["platform", "entitlements", "latest"]:
            return self._latest_user_entitlement(params=params, identity=identity)
        if len(parts) == 3 and parts[:2] == ["platform", "entitlements"]:
            return self._get_user_entitlement(
                snapshot_id=parts[2],
                params=params,
                identity=identity,
            )
        if len(parts) == 2 and parts == ["diagnosis", "known-errors"]:
            return ApiResponse(
                status=200,
                payload={
                    "items": [_known_error_summary(rule) for rule in load_known_error_rules()],
                },
            )
        if len(parts) == 3 and parts[:2] == ["diagnosis", "known-errors"]:
            error_id = parts[2]
            for rule in load_known_error_rules():
                if rule.error_id == error_id:
                    return ApiResponse(status=200, payload=_known_error_detail(rule))
            return ApiResponse(
                status=404,
                payload={"error": {"code": "known_error_not_found", "error_id": error_id}},
            )
        if len(parts) == 1 and parts[0] == "contracts":
            return self._list_contracts(params=params, identity=identity)
        if len(parts) == 1 and parts[0] == "runs":
            return self._list_runs(params=params, identity=identity)
        if len(parts) == 2 and parts == ["agent", "advice"]:
            return self._list_agent_advice(params=params, identity=identity)
        if len(parts) == 2 and parts == ["agent", "executions"]:
            return self._list_agent_executions(params=params, identity=identity)
        if len(parts) == 2 and parts[0] == "contracts":
            if self.contract_service is None:
                return ApiResponse(
                    status=503,
                    payload={"error": {"code": "contract_service_unavailable"}},
                )
            contract_id = parts[1]
            try:
                contract = self.contract_service.get(contract_id)
                access_error = _assert_owner_access(identity, contract.owner)
                if access_error is not None:
                    return access_error
                return ApiResponse(status=200, payload=contract_payload(contract))
            except KeyError:
                return ApiResponse(
                    status=404,
                    payload={"error": {"code": "contract_not_found", "contract_id": contract_id}},
                )
        if len(parts) == 2 and parts[0] == "runs":
            run_id = parts[1]
            try:
                run = self.store.get_run(run_id)
                access_error = _assert_run_access(identity, run)
                if access_error is not None:
                    return access_error
                return ApiResponse(status=200, payload=_run_summary(run))
            except KeyError:
                return ApiResponse(
                    status=404,
                    payload={"error": {"code": "run_not_found", "run_id": run_id}},
                )
        if len(parts) == 3 and parts[0] == "runs" and parts[2] == "lineage":
            run_id = parts[1]
            try:
                run = self.store.get_run(run_id)
                access_error = _assert_run_access(identity, run)
                if access_error is not None:
                    return access_error
                return ApiResponse(status=200, payload=self._run_lineage_payload(run))
            except KeyError:
                return ApiResponse(
                    status=404,
                    payload={"error": {"code": "run_not_found", "run_id": run_id}},
                )
        if len(parts) == 3 and parts[0] == "runs" and parts[2] == "events":
            return self._list_run_events(
                run_id=parts[1],
                params=params,
                identity=identity,
            )
        if len(parts) == 3 and parts[:2] == ["agent", "advice"]:
            advice_id = parts[2]
            try:
                advice = self.agent_advice_service.get(advice_id)
                access_error = _assert_owner_access(identity, advice.owner)
                if access_error is not None:
                    return access_error
                return ApiResponse(
                    status=200,
                    payload=advice_payload(
                        advice,
                        self.agent_advice_service.decisions(advice_id),
                        self.agent_advice_service.executions(advice_id),
                    ),
                )
            except KeyError:
                return ApiResponse(
                    status=404,
                    payload={"error": {"code": "agent_advice_not_found"}},
                )
        if len(parts) == 3 and parts[0] == "runs" and parts[2] == "evidence":
            run_id = parts[1]
            try:
                run = self.store.get_run(run_id)
                access_error = _assert_run_access(identity, run)
                if access_error is not None:
                    return access_error
                return ApiResponse(
                    status=200,
                    payload=self.evidence_query.get_evidence_tree(run_id),
                )
            except KeyError:
                return ApiResponse(
                    status=404,
                    payload={"error": {"code": "run_not_found", "run_id": run_id}},
                )
        if (
            len(parts) == 5
            and parts[0] == "runs"
            and parts[2] == "evidence"
            and parts[3] == "objects"
        ):
            run_id = parts[1]
            object_id = parts[4]
            try:
                run = self.store.get_run(run_id)
            except KeyError:
                return ApiResponse(
                    status=404,
                    payload={"error": {"code": "run_not_found", "run_id": run_id}},
                )
            access_error = _assert_run_access(identity, run)
            if access_error is not None:
                return access_error
            try:
                return ApiResponse(
                    status=200,
                    payload=self.evidence_query.get_object_preview(run_id, object_id),
                )
            except KeyError:
                return ApiResponse(
                    status=404,
                    payload={
                        "error": {
                            "code": "evidence_object_not_found",
                            "run_id": run_id,
                            "object_id": object_id,
                        }
                    },
                )
            except EvidencePreviewUnavailable as exc:
                return ApiResponse(
                    status=409,
                    payload={
                        "error": {
                            "code": "evidence_preview_unavailable",
                            "message": str(exc),
                            "run_id": run_id,
                        }
                    },
                )
        if len(parts) == 3 and parts[0] == "runs" and parts[2] == "diagnoses":
            run_id = parts[1]
            try:
                run = self.store.get_run(run_id)
                access_error = _assert_run_access(identity, run)
                if access_error is not None:
                    return access_error
                return ApiResponse(
                    status=200,
                    payload={
                        "run_id": run_id,
                        "diagnosis_state": run.diagnosis_state.value,
                        "items": [
                            _diagnosis_payload(diagnosis)
                            for diagnosis in self.store.list_diagnoses(run_id)
                        ],
                    },
                )
            except KeyError:
                return ApiResponse(
                    status=404,
                    payload={"error": {"code": "run_not_found", "run_id": run_id}},
                )
        if len(parts) == 3 and parts[0] == "runs" and parts[2] == "capsule":
            run_id = parts[1]
            if self.capsule_service is None:
                return ApiResponse(
                    status=503,
                    payload={"error": {"code": "capsule_service_unavailable"}},
                )
            try:
                run = self.store.get_run(run_id)
                access_error = _assert_run_access(identity, run)
                if access_error is not None:
                    return access_error
                capsule = (
                    None
                    if run.capsule_state.value != "ready"
                    else _capsule_read_payload(self.capsule_service.get_raw_capsule(run_id))
                )
                return ApiResponse(
                    status=200,
                    payload={**_run_summary(run), "capsule": capsule},
                )
            except KeyError:
                return ApiResponse(
                    status=404,
                    payload={"error": {"code": "run_not_found", "run_id": run_id}},
                )
            except CapsuleError as exc:
                return ApiResponse(
                    status=409,
                    payload={
                        "error": {
                            "code": "capsule_read_unavailable",
                            "message": str(exc),
                            "run_id": run_id,
                        }
                    },
                )

        return ApiResponse(
            status=404,
            payload={"error": {"code": "not_found", "path": parsed.path}},
        )

    def handle_post(
        self,
        path: str,
        body: bytes = b"",
        headers: Mapping[str, str] | None = None,
    ) -> ApiResponse:
        request_id = _request_id(headers)
        response = self._proxy_auth_error("POST", path, body, headers)
        if response is None:
            response = self._handle_post(path, body=body, headers=headers)
        return self._finalize_and_trace(
            response,
            method="POST",
            path=path,
            request_id=request_id,
            request_headers=headers,
            enable_etag=False,
        )

    def handle_patch(
        self,
        path: str,
        body: bytes = b"",
        headers: Mapping[str, str] | None = None,
    ) -> ApiResponse:
        request_id = _request_id(headers)
        parsed = urlparse(path)
        parts = _route_parts(parsed.path)
        proxy_error = self._proxy_auth_error("PATCH", path, body, headers)
        identity, auth_error = self._resolve_identity(headers)
        if proxy_error is not None:
            response = proxy_error
        elif auth_error is not None:
            response = auth_error
        elif len(parts) == 2 and parts[0] == "template-drafts":
            response = self._update_template_draft(
                draft_id=parts[1],
                body=body,
                identity=identity,
            )
        else:
            response = ApiResponse(
                status=404,
                payload={"error": {"code": "not_found", "path": parsed.path}},
            )
        return self._finalize_and_trace(
            response,
            method="PATCH",
            path=path,
            request_id=request_id,
            request_headers=headers,
            enable_etag=False,
        )

    def _finalize_and_trace(
        self,
        response: ApiResponse,
        *,
        method: str,
        path: str,
        request_id: str,
        request_headers: Mapping[str, str] | None,
        enable_etag: bool,
    ) -> ApiResponse:
        finalized = _finalize_response(
            response,
            request_id=request_id,
            request_headers=request_headers,
            enable_etag=enable_etag,
        )
        parsed_path = urlparse(path).path
        if not parsed_path.startswith("/api/v1/") or parsed_path.startswith("/api/v1/health/"):
            return finalized
        identifiers = _trace_identifiers(parsed_path, finalized.payload)
        run_id = identifiers.get("run_id")
        if run_id is not None and "job_id" not in identifiers:
            try:
                job_id = self.store.get_run(run_id).job_id
            except KeyError:
                job_id = None
            if job_id is not None:
                identifiers["job_id"] = str(job_id)
        try:
            self.control_repository.record_trace(
                trace_id=f"trace_{uuid4().hex}",
                request_id=request_id,
                method=method,
                route=normalize_http_route(parsed_path),
                status=finalized.status,
                actor=_header_value(request_headers, self.trusted_user_header),
                run_id=identifiers.get("run_id"),
                job_id=identifiers.get("job_id"),
                session_id=identifiers.get("session_id"),
            )
        except Exception:
            self.metrics.observe_trace_write(outcome="error")
        else:
            self.metrics.observe_trace_write(outcome="success")
        return finalized

    def _handle_post(
        self,
        path: str,
        body: bytes = b"",
        headers: Mapping[str, str] | None = None,
    ) -> ApiResponse:
        parsed = urlparse(path)
        parts = _route_parts(parsed.path)
        identity, auth_error = self._resolve_identity(headers)
        if auth_error is not None:
            return auth_error
        remediation_response = self.remediation_routes.handle_post(
            parts,
            body=body,
            identity=identity,
        )
        if remediation_response is not None:
            return remediation_response
        if len(parts) == 1 and parts[0] == "template-drafts":
            return self._create_template_draft(body=body, identity=identity)
        if (
            len(parts) == 3
            and parts[0] == "template-drafts"
            and parts[2] in {"validate", "reviews", "publish"}
        ):
            return self._mutate_template_draft(
                draft_id=parts[1],
                action=parts[2],
                body=body,
                identity=identity,
            )
        if len(parts) == 3 and parts[0] == "template-reviews" and parts[2] == "decision":
            return self._decide_template_review(
                review_id=parts[1],
                body=body,
                identity=identity,
            )
        if (
            len(parts) == 5
            and parts[0] == "templates"
            and parts[2] == "releases"
            and parts[4] in {"adopt", "withdraw", "verify"}
        ):
            return self._mutate_template_release(
                template_id=parts[1],
                release_version=parts[3],
                action=parts[4],
                body=body,
                identity=identity,
            )
        if len(parts) == 2 and parts == ["contracts", "validate"]:
            if self.contract_service is None:
                return ApiResponse(
                    status=503,
                    payload={"error": {"code": "contract_service_unavailable"}},
                )
            payload, error = _json_body(body)
            if error is not None:
                return error
            try:
                return ApiResponse(
                    status=200,
                    payload=validation_payload(self.contract_service.validate(payload)),
                )
            except (ContractError, KeyError, TypeError, ValueError) as exc:
                return _contract_error_response(exc)

        if len(parts) == 3 and parts == ["contracts", "agent", "suggest"]:
            return self._contract_agent_suggest(
                body=body, identity=identity,
            )

        if len(parts) == 1 and parts[0] == "contracts":
            if self.contract_service is None:
                return ApiResponse(
                    status=503,
                    payload={"error": {"code": "contract_service_unavailable"}},
                )
            payload, error = _json_body(body)
            if error is not None:
                return error
            owner, error = _owner_from_payload_or_identity(payload, identity)
            if error is not None:
                return error
            try:
                contract = self.contract_service.create(owner=owner, payload=payload)
                return ApiResponse(status=201, payload=contract_payload(contract))
            except (ContractError, KeyError, TypeError, ValueError) as exc:
                return _contract_error_response(exc)

        if len(parts) == 3 and parts[0] == "contracts" and parts[2] == "preflight":
            if self.contract_service is None:
                return ApiResponse(
                    status=503,
                    payload={"error": {"code": "contract_service_unavailable"}},
                )
            contract_id = parts[1]
            try:
                contract = self.contract_service.get(contract_id)
                access_error = _assert_owner_access(identity, contract.owner)
                if access_error is not None:
                    return access_error
                return ApiResponse(
                    status=200,
                    payload=validation_payload(self.contract_service.preflight(contract)),
                )
            except KeyError:
                return ApiResponse(
                    status=404,
                    payload={"error": {"code": "contract_not_found", "contract_id": contract_id}},
                )
            except (ContractError, TypeError, ValueError) as exc:
                return _contract_error_response(exc)

        if len(parts) == 2 and parts == ["runs", "prepare"]:
            if self.run_service is None:
                return ApiResponse(
                    status=503,
                    payload={"error": {"code": "run_service_unavailable"}},
                )
            payload, error = _json_body(body)
            if error is not None:
                return error
            if "contract_id" in payload:
                request, error = self._submit_request_from_contract(payload, identity=identity)
            else:
                request, error = _submit_request_from_payload(payload, identity=identity)
            if error is not None:
                return error
            findings = [
                *validate_resource_plan(
                    request.resource_plan,
                    partition_qos=self.capability_profile.partition_qos(),
                    qos_limits=self.capability_profile.qos_limits(),
                ),
                *self._dynamic_resource_findings(
                    owner=request.owner,
                    resource_plan=request.resource_plan,
                ),
            ]
            blocking = [
                finding for finding in findings if finding.severity == PreflightSeverity.BLOCK
            ]
            if blocking:
                return ApiResponse(
                    status=422,
                    payload={
                        "error": {"code": "preflight_blocked"},
                        "preflight": [_finding_payload(finding) for finding in findings],
                    },
                )
            try:
                run = self.run_service.prepare(request)
            except KeyError as exc:
                return ApiResponse(
                    status=404,
                    payload={
                        "error": {
                            "code": "parent_run_not_found",
                            "parent_run_id": str(exc.args[0]),
                        }
                    },
                )
            except ValueError as exc:
                return ApiResponse(
                    status=400,
                    payload={"error": {"code": "invalid_run_lineage", "message": str(exc)}},
                )
            self.store.append_event(
                run_id=run.run_id,
                event_type="run.preflight",
                payload={
                    "status": "OK",
                    "findings": [_finding_payload(finding) for finding in findings],
                },
            )
            return ApiResponse(
                status=201,
                payload={
                    **_run_summary(run),
                    "script_artifacts": _script_artifacts(run),
                    "preview": {
                        "submitted_script": run.script,
                        "execution_wrapper": generated_execution_wrapper(run),
                    },
                    "risk_lint": [],
                    "preflight": [_finding_payload(finding) for finding in findings],
                },
            )

        if len(parts) == 3 and parts[0] == "runs" and parts[2] == "submit":
            run_id = parts[1]
            if self.run_service is None:
                return ApiResponse(
                    status=503,
                    payload={"error": {"code": "run_service_unavailable"}},
                )
            try:
                run = self.store.get_run(run_id)
                access_error = _assert_run_access(identity, run)
                if access_error is not None:
                    return access_error
                return ApiResponse(
                    status=200,
                    payload={
                        **_run_summary(self.run_service.submit_prepared(run_id)),
                        "submit_state": "submitted",
                    },
                )
            except KeyError:
                return ApiResponse(
                    status=404,
                    payload={"error": {"code": "run_not_found", "run_id": run_id}},
                )
            except SlurmBackendError as exc:
                if isinstance(exc, SubmissionInProgressError):
                    return ApiResponse(
                        status=409,
                        payload={
                            "error": {
                                "code": "submission_in_progress",
                                "message": str(exc),
                                "run_id": run_id,
                            }
                        },
                    )
                if isinstance(exc, (WorkflowDependencyError, WorkflowRetryNotReadyError)):
                    return ApiResponse(
                        status=409,
                        payload={
                            "error": {
                                "code": "workflow_not_ready",
                                "message": str(exc),
                                "run_id": run_id,
                            }
                        },
                    )
                return ApiResponse(
                    status=502,
                    payload={
                        "error": {
                            "code": "slurm_backend_error",
                            "message": str(exc),
                            "run_id": run_id,
                        }
                    },
                )

        if len(parts) == 3 and parts[0] == "runs" and parts[2] == "cancel":
            run_id = parts[1]
            if self.run_service is None:
                return ApiResponse(
                    status=503,
                    payload={"error": {"code": "run_service_unavailable"}},
                )
            try:
                run = self.store.get_run(run_id)
                access_error = _assert_run_access(identity, run)
                if access_error is not None:
                    return access_error
                return ApiResponse(
                    status=200,
                    payload=_run_summary(self.run_service.cancel(run_id)),
                )
            except KeyError:
                return ApiResponse(
                    status=404,
                    payload={"error": {"code": "run_not_found", "run_id": run_id}},
                )
            except SlurmBackendError as exc:
                return ApiResponse(
                    status=502,
                    payload={
                        "error": {
                            "code": "slurm_backend_error",
                            "message": str(exc),
                            "run_id": run_id,
                        }
                    },
                )

        if len(parts) == 3 and parts[0] == "runs" and parts[2] == "capsule":
            run_id = parts[1]
            if self.capsule_service is None:
                return ApiResponse(
                    status=503,
                    payload={"error": {"code": "capsule_service_unavailable"}},
                )
            try:
                run = self.store.get_run(run_id)
                access_error = _assert_run_access(identity, run)
                if access_error is not None:
                    return access_error
                capsule = self.capsule_service.build_raw_capsule(run_id)
                updated_run = self.store.get_run(run_id)
                return ApiResponse(
                    status=200,
                    payload={
                        **_run_summary(updated_run),
                        "capsule": _capsule_payload(capsule),
                    },
                )
            except KeyError:
                return ApiResponse(
                    status=404,
                    payload={"error": {"code": "run_not_found", "run_id": run_id}},
                )
            except CapsuleError as exc:
                return ApiResponse(
                    status=409,
                    payload={
                        "error": {
                            "code": "capsule_not_ready",
                            "message": str(exc),
                            "run_id": run_id,
                        }
                    },
                )

        if len(parts) == 3 and parts[0] == "runs" and parts[2] == "diagnose":
            run_id = parts[1]
            try:
                run = self.store.get_run(run_id)
                access_error = _assert_run_access(identity, run)
                if access_error is not None:
                    return access_error
                records = self.diagnosis_service.diagnose(run_id)
                updated_run = self.store.get_run(run_id)
                return ApiResponse(
                    status=200,
                    payload={
                        "run_id": run_id,
                        "diagnosis_state": updated_run.diagnosis_state.value,
                        "items": [_diagnosis_payload(record) for record in records],
                    },
                )
            except KeyError:
                return ApiResponse(
                    status=404,
                    payload={"error": {"code": "run_not_found", "run_id": run_id}},
                )

        if len(parts) == 4 and parts[0] == "runs" and parts[2:] == ["agent", "explain"]:
            run_id = parts[1]
            payload, error = _json_body(body)
            if error is not None:
                return error
            provider = str(payload.get("provider", "none"))
            try:
                run = self.store.get_run(run_id)
                access_error = _assert_run_access(identity, run)
                if access_error is not None:
                    return access_error
                explanation = self.agent_explain_service.explain(run_id, provider=provider)
                return ApiResponse(status=200, payload=explanation.to_payload())
            except KeyError:
                return ApiResponse(
                    status=404,
                    payload={"error": {"code": "run_not_found", "run_id": run_id}},
                )
            except AgentProviderError as exc:
                return ApiResponse(
                    status=400,
                    payload={
                        "error": {
                            "code": "agent_provider_unsupported",
                            "message": str(exc),
                        }
                    },
                )

        if len(parts) == 4 and parts[0] == "runs" and parts[2:] == ["agent", "advise"]:
            run_id = parts[1]
            payload, error = _json_body(body)
            if error is not None:
                return error
            try:
                run = self.store.get_run(run_id)
                access_error = _assert_run_access(identity, run)
                if access_error is not None:
                    return access_error
                result = self.agent_advice_service.advise(
                    run_id,
                    provider=str(payload.get("provider", "none")),
                    idempotency_key=(
                        None
                        if payload.get("idempotency_key") is None
                        else str(payload["idempotency_key"])
                    ),
                )
                return ApiResponse(
                    status=201 if result.created else 200,
                    payload=advice_payload(result.record),
                )
            except KeyError:
                return ApiResponse(
                    status=404,
                    payload={"error": {"code": "run_not_found", "run_id": run_id}},
                )
            except AgentProviderError as exc:
                return ApiResponse(
                    status=400,
                    payload={"error": {"code": exc.code, "message": str(exc)}},
                )
            except AgentAdviceError as exc:
                return _agent_advice_error_response(exc)

        if (
            len(parts) == 4
            and parts[:2] == ["agent", "advice"]
            and parts[3] in {"approve", "reject"}
        ):
            advice_id = parts[2]
            payload, error = _json_body(body)
            if error is not None:
                return error
            try:
                advice = self.agent_advice_service.get(advice_id)
                access_error = _assert_owner_access(identity, advice.owner)
                if access_error is not None:
                    return access_error
                expected_version = payload.get("expected_version")
                if not isinstance(expected_version, int) or isinstance(expected_version, bool):
                    raise AgentAdviceError(
                        "expected_version must be an integer",
                        code="AGENT.INVALID_VERSION",
                    )
                actor = identity.username if identity is not None else advice.owner
                note = None if payload.get("note") is None else str(payload["note"])
                if parts[3] == "approve":
                    raw_action_ids = payload.get("action_ids")
                    if not isinstance(raw_action_ids, list) or not all(
                        isinstance(item, str) for item in raw_action_ids
                    ):
                        raise AgentAdviceError(
                            "action_ids must be an array of strings",
                            code="AGENT.INVALID_ACTION_SELECTION",
                        )
                    updated = self.agent_advice_service.approve(
                        advice_id,
                        expected_version=expected_version,
                        action_ids=raw_action_ids,
                        actor=actor,
                        note=note,
                    )
                else:
                    updated = self.agent_advice_service.reject(
                        advice_id,
                        expected_version=expected_version,
                        actor=actor,
                        note=note,
                    )
                return ApiResponse(
                    status=200,
                    payload=advice_payload(
                        updated,
                        self.agent_advice_service.decisions(advice_id),
                        self.agent_advice_service.executions(advice_id),
                    ),
                )
            except KeyError:
                return ApiResponse(
                    status=404,
                    payload={"error": {"code": "agent_advice_not_found"}},
                )
            except AgentAdviceError as exc:
                return _agent_advice_error_response(exc)

        if (
            len(parts) == 6
            and parts[:2] == ["agent", "advice"]
            and parts[3] == "actions"
            and parts[5] == "execute"
        ):
            advice_id = parts[2]
            action_id = parts[4]
            payload, error = _json_body(body)
            if error is not None:
                return error
            submit = payload.get("submit", True)
            if not isinstance(submit, bool):
                return ApiResponse(
                    status=400,
                    payload={
                        "error": {
                            "code": "AGENT.INVALID_EXECUTION_REQUEST",
                            "message": "submit must be boolean",
                        }
                    },
                )
            try:
                advice = self.agent_advice_service.get(advice_id)
                access_error = _assert_owner_access(identity, advice.owner)
                if access_error is not None:
                    return access_error
                actor = identity.username if identity is not None else advice.owner
                execution = self.agent_advice_service.execute_action(
                    advice_id,
                    action_id=action_id,
                    actor=actor,
                    submit=submit,
                )
                return ApiResponse(status=200, payload=execution_payload(execution))
            except KeyError:
                return ApiResponse(
                    status=404,
                    payload={"error": {"code": "agent_advice_not_found"}},
                )
            except AgentAdviceError as exc:
                return _agent_advice_error_response(exc)

        return ApiResponse(
            status=404,
            payload={"error": {"code": "not_found", "path": parsed.path}},
        )

    def _platform_capabilities(
        self,
        *,
        params: dict[str, list[str]],
        identity: UserIdentity | None,
    ) -> ApiResponse:
        try:
            _reject_unknown_params(params, {"owner"})
            requested_owner = _optional_query_text(params, "owner", max_length=64)
            owner = _optional_bound_owner(requested_owner, identity)
        except PermissionError:
            return _forbidden_query_response()
        except ValueError as exc:
            return _invalid_query_response(exc)
        payload = self.capability_profile.to_payload()
        if owner is not None and self.platform_snapshot_store is not None:
            latest = self.platform_snapshot_store.latest(owner=owner)
            payload["latest_snapshot"] = None if latest is None else latest.summary_payload()
        else:
            payload["latest_snapshot"] = None
        if owner is not None and self.user_entitlement_store is not None:
            latest_entitlement = self.user_entitlement_store.latest(owner=owner)
            payload["latest_entitlement"] = (
                None if latest_entitlement is None else latest_entitlement.summary_payload()
            )
        else:
            payload["latest_entitlement"] = None
        return ApiResponse(status=200, payload=payload)

    def _list_platform_snapshots(
        self,
        *,
        params: dict[str, list[str]],
        identity: UserIdentity | None,
    ) -> ApiResponse:
        if self.platform_snapshot_store is None:
            return ApiResponse(
                status=503,
                payload={"error": {"code": "platform_snapshot_store_unavailable"}},
            )
        try:
            _reject_unknown_params(
                params,
                {
                    "owner",
                    "scope",
                    "source_type",
                    "freshness",
                    "as_of",
                    "limit",
                    "cursor",
                },
            )
            owner = _query_owner(params, identity)
            snapshot_scope = _optional_query_enum(params, "scope", PlatformSnapshotScope)
            source_type = _optional_query_enum(params, "source_type", ObservationSourceType)
            freshness = _optional_query_enum(params, "freshness", SnapshotFreshness)
            as_of = _optional_query_time(params, "as_of") or datetime.now(UTC).isoformat()
            as_of_datetime = datetime.fromisoformat(as_of)
            limit = _query_limit(params)
            filters = {
                "owner": owner,
                "scope": None if snapshot_scope is None else snapshot_scope.value,
                "source_type": None if source_type is None else source_type.value,
                "freshness": None if freshness is None else freshness.value,
                "as_of": as_of,
            }
            scope_digest = cursor_scope("platform_snapshots", filters)
            cursor = _query_cursor(
                params,
                kind="platform_snapshots",
                scope=scope_digest,
            )
            items, next_position = self.platform_snapshot_store.list_page(
                owner=owner,
                scope=snapshot_scope,
                source_type=source_type,
                freshness=freshness,
                at=as_of_datetime,
                cursor=cursor,
                limit=limit,
            )
        except PermissionError:
            return _forbidden_query_response()
        except (CursorError, ValueError) as exc:
            return _invalid_query_response(exc)
        response = _page_response(
            items=[item.summary_payload(at=as_of_datetime) for item in items],
            limit=limit,
            next_position=next_position,
            kind="platform_snapshots",
            scope=scope_digest,
        )
        response.payload["page"]["as_of"] = as_of
        return response

    def _latest_platform_snapshot(
        self,
        *,
        params: dict[str, list[str]],
        identity: UserIdentity | None,
    ) -> ApiResponse:
        if self.platform_snapshot_store is None:
            return ApiResponse(
                status=503,
                payload={"error": {"code": "platform_snapshot_store_unavailable"}},
            )
        try:
            _reject_unknown_params(params, {"owner", "scope"})
            owner = _query_owner(params, identity)
            snapshot_scope = _optional_query_enum(params, "scope", PlatformSnapshotScope)
            record = self.platform_snapshot_store.latest(owner=owner, scope=snapshot_scope)
        except PermissionError:
            return _forbidden_query_response()
        except ValueError as exc:
            return _invalid_query_response(exc)
        if record is None:
            return ApiResponse(
                status=404,
                payload={"error": {"code": "platform_snapshot_not_found"}},
            )
        return ApiResponse(status=200, payload=record.safe_payload())

    def _dynamic_resource_findings(
        self,
        *,
        owner: str,
        resource_plan: ResourcePlan,
    ) -> list[PreflightFinding]:
        findings: list[PreflightFinding] = []
        if self.platform_snapshot_store is not None:
            findings.extend(
                validate_platform_snapshot_resource_plan(
                    resource_plan,
                    self.platform_snapshot_store.latest(
                        owner=owner,
                        scope=PlatformSnapshotScope.LOGIN_NODE,
                    ),
                )
            )
        if self.user_entitlement_store is not None:
            findings.extend(
                validate_user_entitlement_resource_plan(
                    resource_plan,
                    self.user_entitlement_store.latest(owner=owner),
                )
            )
        return findings

    def _get_platform_snapshot(
        self,
        *,
        snapshot_id: str,
        params: dict[str, list[str]],
        identity: UserIdentity | None,
    ) -> ApiResponse:
        if self.platform_snapshot_store is None:
            return ApiResponse(
                status=503,
                payload={"error": {"code": "platform_snapshot_store_unavailable"}},
            )
        try:
            _reject_unknown_params(params, {"owner"})
            if not snapshot_id or len(snapshot_id) > 128 or "\x00" in snapshot_id:
                raise ValueError("snapshot_id is invalid")
            owner = _query_owner(params, identity)
            record = self.platform_snapshot_store.get(snapshot_id, owner=owner)
        except PermissionError:
            return _forbidden_query_response()
        except ValueError as exc:
            return _invalid_query_response(exc)
        except KeyError:
            return ApiResponse(
                status=404,
                payload={
                    "error": {
                        "code": "platform_snapshot_not_found",
                        "snapshot_id": snapshot_id,
                    }
                },
            )
        return ApiResponse(status=200, payload=record.safe_payload())

    def _list_user_entitlements(
        self,
        *,
        params: dict[str, list[str]],
        identity: UserIdentity | None,
    ) -> ApiResponse:
        if self.user_entitlement_store is None:
            return ApiResponse(
                status=503,
                payload={"error": {"code": "user_entitlement_store_unavailable"}},
            )
        try:
            _reject_unknown_params(
                params,
                {"owner", "freshness", "as_of", "limit", "cursor"},
            )
            owner = _query_owner(params, identity)
            freshness = _optional_query_enum(params, "freshness", SnapshotFreshness)
            as_of = _optional_query_time(params, "as_of") or datetime.now(UTC).isoformat()
            as_of_datetime = datetime.fromisoformat(as_of)
            limit = _query_limit(params)
            filters = {
                "owner": owner,
                "freshness": None if freshness is None else freshness.value,
                "as_of": as_of,
            }
            scope_digest = cursor_scope("user_entitlements", filters)
            cursor = _query_cursor(
                params,
                kind="user_entitlements",
                scope=scope_digest,
            )
            items, next_position = self.user_entitlement_store.list_page(
                owner=owner,
                freshness=freshness,
                at=as_of_datetime,
                cursor=cursor,
                limit=limit,
            )
        except PermissionError:
            return _forbidden_query_response()
        except (CursorError, ValueError) as exc:
            return _invalid_query_response(exc)
        response = _page_response(
            items=[item.summary_payload(at=as_of_datetime) for item in items],
            limit=limit,
            next_position=next_position,
            kind="user_entitlements",
            scope=scope_digest,
        )
        response.payload["page"]["as_of"] = as_of
        return response

    def _latest_user_entitlement(
        self,
        *,
        params: dict[str, list[str]],
        identity: UserIdentity | None,
    ) -> ApiResponse:
        if self.user_entitlement_store is None:
            return ApiResponse(
                status=503,
                payload={"error": {"code": "user_entitlement_store_unavailable"}},
            )
        try:
            _reject_unknown_params(params, {"owner"})
            owner = _query_owner(params, identity)
            record = self.user_entitlement_store.latest(owner=owner)
        except PermissionError:
            return _forbidden_query_response()
        except ValueError as exc:
            return _invalid_query_response(exc)
        if record is None:
            return ApiResponse(
                status=404,
                payload={"error": {"code": "user_entitlement_not_found"}},
            )
        return ApiResponse(status=200, payload=record.safe_payload())

    def _get_user_entitlement(
        self,
        *,
        snapshot_id: str,
        params: dict[str, list[str]],
        identity: UserIdentity | None,
    ) -> ApiResponse:
        if self.user_entitlement_store is None:
            return ApiResponse(
                status=503,
                payload={"error": {"code": "user_entitlement_store_unavailable"}},
            )
        try:
            _reject_unknown_params(params, {"owner"})
            owner = _query_owner(params, identity)
            record = self.user_entitlement_store.get(snapshot_id, owner=owner)
        except PermissionError:
            return _forbidden_query_response()
        except ValueError as exc:
            return _invalid_query_response(exc)
        except KeyError:
            return ApiResponse(
                status=404,
                payload={
                    "error": {
                        "code": "user_entitlement_not_found",
                        "snapshot_id": snapshot_id,
                    }
                },
            )
        return ApiResponse(status=200, payload=record.safe_payload())

    def _list_runs(
        self,
        *,
        params: dict[str, list[str]],
        identity: UserIdentity | None,
    ) -> ApiResponse:
        try:
            _reject_unknown_params(
                params,
                {
                    "owner",
                    "state",
                    "contract_id",
                    "recipe_version_id",
                    "created_after",
                    "created_before",
                    "q",
                    "limit",
                    "cursor",
                },
            )
            owner = _query_owner(params, identity)
            states = _enum_values(params, "state", {state.value for state in RunState})
            contract_id = _optional_query_text(params, "contract_id", max_length=128)
            recipe_version_id = _optional_query_text(params, "recipe_version_id", max_length=256)
            created_after = _optional_query_time(params, "created_after")
            created_before = _optional_query_time(params, "created_before")
            query = _optional_query_text(params, "q", max_length=256)
            limit = _query_limit(params)
            filters = {
                "owner": owner,
                "states": list(states),
                "contract_id": contract_id,
                "recipe_version_id": recipe_version_id,
                "created_after": created_after,
                "created_before": created_before,
                "query": query,
            }
            scope = cursor_scope("runs", filters)
            cursor = _query_cursor(params, kind="runs", scope=scope)
            if recipe_version_id is not None and self.contract_service is None:
                return ApiResponse(
                    status=503,
                    payload={"error": {"code": "contract_service_unavailable"}},
                )
            items, next_position = self.store.list_runs_page(
                owner=owner,
                states=states,
                contract_id=contract_id,
                recipe_version_id=recipe_version_id,
                created_after=created_after,
                created_before=created_before,
                query=query,
                cursor=cursor,
                limit=limit,
            )
        except PermissionError:
            return _forbidden_query_response()
        except (CursorError, ValueError) as exc:
            return _invalid_query_response(exc)
        return _page_response(
            items=[_run_summary(item) for item in items],
            limit=limit,
            next_position=next_position,
            kind="runs",
            scope=scope,
        )

    def _list_contracts(
        self,
        *,
        params: dict[str, list[str]],
        identity: UserIdentity | None,
    ) -> ApiResponse:
        if self.contract_service is None:
            return ApiResponse(
                status=503,
                payload={"error": {"code": "contract_service_unavailable"}},
            )
        try:
            _reject_unknown_params(
                params,
                {"owner", "recipe_version_id", "digest", "derived", "q", "limit", "cursor"},
            )
            owner = _query_owner(params, identity)
            recipe_version_id = _optional_query_text(params, "recipe_version_id", max_length=256)
            digest = _optional_query_text(params, "digest", max_length=64)
            derived = _optional_query_bool(params, "derived")
            query = _optional_query_text(params, "q", max_length=256)
            limit = _query_limit(params)
            filters = {
                "owner": owner,
                "recipe_version_id": recipe_version_id,
                "digest": digest,
                "derived": derived,
                "query": query,
            }
            scope = cursor_scope("contracts", filters)
            cursor = _query_cursor(params, kind="contracts", scope=scope)
            items, next_position = self.contract_service.store.list_contracts_page(
                owner=owner,
                recipe_version_id=recipe_version_id,
                digest=digest,
                derived=derived,
                query=query,
                cursor=cursor,
                limit=limit,
            )
        except PermissionError:
            return _forbidden_query_response()
        except (CursorError, ValueError) as exc:
            return _invalid_query_response(exc)
        return _page_response(
            items=[_contract_summary(item) for item in items],
            limit=limit,
            next_position=next_position,
            kind="contracts",
            scope=scope,
        )

    def _contract_agent_suggest(
        self,
        *,
        body: bytes,
        identity: UserIdentity | None,
    ) -> ApiResponse:
        from pilot107.core.agent import (
            AgentProviderError,
            OpenAICompatibleLLMProvider,
            suggest_contract_patch_without_llm,
        )
        payload, error = _json_body(body)
        if error is not None:
            return error
        current_contract = payload.get("current_contract")
        recipe_version_id = payload.get("recipe_version_id")
        user_intent = payload.get("user_intent")
        provider = str(payload.get("provider", "none"))
        if not isinstance(current_contract, dict):
            return ApiResponse(
                status=400,
                payload={
                    "error": {
                        "code": "INVALID_REQUEST",
                        "message": "current_contract must be an object",
                    }
                },
            )
        if not isinstance(recipe_version_id, str) or not recipe_version_id:
            return ApiResponse(
                status=400,
                payload={
                    "error": {"code": "INVALID_REQUEST", "message": "recipe_version_id is required"}
                },
            )
        if not isinstance(user_intent, str) or not user_intent.strip():
            return ApiResponse(
                status=400,
                payload={
                    "error": {"code": "INVALID_REQUEST", "message": "user_intent is required"}
                },
            )
        if provider == "none":
            # Intentional: the user chose deterministic mode. This is not a
            # failure, so it stays HTTP 200 with status "ok" and an empty patch.
            return ApiResponse(
                status=200,
                payload={**suggest_contract_patch_without_llm(), "status": "ok"},
            )
        # Prefer the injected LLM provider from agent_explain_service; fall back to env.
        llm_provider = getattr(self.agent_explain_service, "llm_provider", None)
        if llm_provider is None:
            try:
                llm_provider = OpenAICompatibleLLMProvider.from_env()
            except ValueError:
                return ApiResponse(
                    status=200,
                    payload=_agent_suggest_degraded(
                        "provider_unconfigured",
                        "未配置 LLM 网关，无法生成建议。请选择确定性模式或配置 LLM。",
                    ),
                )
        try:
            result = llm_provider.suggest_contract_patch(
                current_contract=current_contract,
                recipe_version_id=recipe_version_id,
                user_intent=user_intent,
            )
            return ApiResponse(status=200, payload={**result, "status": "ok"})
        except AgentProviderError as exc:
            reason = _agent_suggest_reason_for_error(exc)
            return ApiResponse(
                status=200,
                payload=_agent_suggest_degraded(
                    reason,
                    _agent_suggest_degraded_explanation_zh(reason),
                ),
            )

    def _list_agent_advice(
        self,
        *,
        params: dict[str, list[str]],
        identity: UserIdentity | None,
    ) -> ApiResponse:
        try:
            _reject_unknown_params(
                params,
                {"owner", "state", "pending", "run_id", "limit", "cursor"},
            )
            owner = _query_owner(params, identity)
            states = _enum_values(params, "state", _ADVICE_STATES)
            pending = _optional_query_bool(params, "pending")
            if pending is True:
                if states:
                    raise ValueError("pending cannot be combined with state")
                states = ("ready",)
            run_id = _optional_query_text(params, "run_id", max_length=128)
            limit = _query_limit(params)
            filters = {
                "owner": owner,
                "states": list(states),
                "run_id": run_id,
                "pending": pending,
            }
            scope = cursor_scope("agent_advice", filters)
            cursor = _query_cursor(params, kind="agent_advice", scope=scope)
            items, next_position = self.store.list_agent_advice_page(
                owner=owner,
                states=states,
                run_id=run_id,
                cursor=cursor,
                limit=limit,
            )
        except PermissionError:
            return _forbidden_query_response()
        except (CursorError, ValueError) as exc:
            return _invalid_query_response(exc)
        return _page_response(
            items=[_agent_advice_summary(item) for item in items],
            limit=limit,
            next_position=next_position,
            kind="agent_advice",
            scope=scope,
        )

    def _list_agent_executions(
        self,
        *,
        params: dict[str, list[str]],
        identity: UserIdentity | None,
    ) -> ApiResponse:
        try:
            _reject_unknown_params(
                params,
                {"owner", "state", "advice_id", "limit", "cursor"},
            )
            owner = _query_owner(params, identity)
            states = _enum_values(params, "state", _EXECUTION_STATES)
            advice_id = _optional_query_text(params, "advice_id", max_length=128)
            limit = _query_limit(params)
            filters = {
                "owner": owner,
                "states": list(states),
                "advice_id": advice_id,
            }
            scope = cursor_scope("agent_executions", filters)
            cursor = _query_cursor(params, kind="agent_executions", scope=scope)
            items, next_position = self.store.list_agent_action_executions_page(
                owner=owner,
                states=states,
                advice_id=advice_id,
                cursor=cursor,
                limit=limit,
            )
        except PermissionError:
            return _forbidden_query_response()
        except (CursorError, ValueError) as exc:
            return _invalid_query_response(exc)
        return _page_response(
            items=[execution_payload(item) for item in items],
            limit=limit,
            next_position=next_position,
            kind="agent_executions",
            scope=scope,
        )

    def _list_run_events(
        self,
        *,
        run_id: str,
        params: dict[str, list[str]],
        identity: UserIdentity | None,
    ) -> ApiResponse:
        try:
            run = self.store.get_run(run_id)
        except KeyError:
            return ApiResponse(
                status=404,
                payload={"error": {"code": "run_not_found", "run_id": run_id}},
            )
        access_error = _assert_run_access(identity, run)
        if access_error is not None:
            return access_error
        try:
            _reject_unknown_params(
                params,
                {"type", "limit", "cursor", "after_event_id"},
            )
            event_types = _event_type_values(params)
            limit = _query_limit(params, default=100)
            filters = {"owner": run.owner, "run_id": run_id, "event_types": list(event_types)}
            scope = cursor_scope("run_events", filters)
            cursor = _query_cursor(params, kind="run_events", scope=scope)
            after_text = _optional_query_text(params, "after_event_id", max_length=20)
            if cursor is not None and after_text is not None:
                raise ValueError("cursor cannot be combined with after_event_id")
            if cursor is not None:
                if cursor.secondary != run_id:
                    raise CursorError("event cursor does not match this run")
                after_event_id = int(cursor.primary)
            else:
                after_event_id = int(after_text or "0")
            events, next_event_id = self.store.list_events_page(
                run_id,
                event_types=event_types,
                after_event_id=after_event_id,
                limit=limit,
            )
        except (CursorError, ValueError) as exc:
            return _invalid_query_response(exc)
        next_position = (
            None
            if next_event_id is None
            else CursorPosition(primary=str(next_event_id), secondary=run_id)
        )
        response = _page_response(
            items=[_run_event_payload(event) for event in events],
            limit=limit,
            next_position=next_position,
            kind="run_events",
            scope=scope,
        )
        response.payload["run_id"] = run_id
        response.payload["page"]["last_event_id"] = (
            events[-1].event_id if events else after_event_id
        )
        return response

    def _run_lineage_payload(self, run: RunRecord) -> dict[str, Any]:
        lineage = self.store.list_run_lineage(run.run_id)
        children = self.store.list_child_runs(run.run_id)
        family, external_dependencies = self.store.list_run_family(run.run_id)
        records = {item.run_id: item for item in [*family, *external_dependencies]}
        edges: list[dict[str, Any]] = []
        for item in family:
            if item.parent_run_id is not None and item.parent_run_id in records:
                edges.append(
                    {
                        "source_run_id": item.parent_run_id,
                        "target_run_id": item.run_id,
                        "type": "lineage",
                        "reason": item.lineage_reason,
                    }
                )
            dependencies = item.workflow.get("dependencies", [])
            for dependency_id in dependencies:
                if isinstance(dependency_id, str) and dependency_id in records:
                    edges.append(
                        {
                            "source_run_id": dependency_id,
                            "target_run_id": item.run_id,
                            "type": "workflow_dependency",
                            "reason": "afterok",
                        }
                    )
        return {
            "run_id": run.run_id,
            "root_run_id": lineage[0].run_id,
            "lineage": [_run_summary(item) for item in lineage],
            "children": [_run_summary(item) for item in children],
            "nodes": [_run_summary(item) for item in records.values()],
            "edges": edges,
        }

    def _list_template_drafts(
        self,
        *,
        params: dict[str, list[str]],
        identity: UserIdentity | None,
    ) -> ApiResponse:
        if self.template_market_store is None:
            return _template_service_unavailable()
        try:
            _reject_unknown_params(params, {"owner", "limit", "cursor"})
            owner = _query_owner(params, identity)
            limit = _query_limit(params)
            scope = cursor_scope("template_drafts", {"owner": owner})
            cursor = _query_cursor(
                params,
                kind="template_drafts",
                scope=scope,
            )
            records, next_position = self.template_market_store.list_drafts_page(
                owner=owner,
                cursor=cursor,
                limit=limit,
            )
            return _page_response(
                items=[template_draft_payload(record) for record in records],
                limit=limit,
                next_position=next_position,
                kind="template_drafts",
                scope=scope,
            )
        except PermissionError:
            return _forbidden_query_response()
        except (TemplateMarketError, ValueError) as exc:
            return _template_error_response(exc)

    def _list_template_reviews(
        self,
        *,
        params: dict[str, list[str]],
        identity: UserIdentity | None,
    ) -> ApiResponse:
        if self.template_market_store is None:
            return _template_service_unavailable()
        if identity is None:
            return ApiResponse(status=401, payload={"error": {"code": "AUTH.MISSING"}})
        try:
            _reject_unknown_params(params, {"limit", "cursor"})
            limit = _query_limit(params)
            principal = self.template_role_directory.reviewer_principal(identity.username)
            filters = {
                "actor": principal.actor,
                "roles": sorted(role.value for role in principal.roles),
                "scopes": sorted(principal.course_scopes),
            }
            scope = cursor_scope("template_reviews", filters)
            cursor = _query_cursor(params, kind="template_reviews", scope=scope)
            records, next_position = self.template_market_store.list_review_queue_page(
                principal=principal,
                cursor=cursor,
                limit=limit,
            )
            return _page_response(
                items=[template_review_queue_payload(record) for record in records],
                limit=limit,
                next_position=next_position,
                kind="template_reviews",
                scope=scope,
            )
        except (CursorError, TemplateMarketError, ValueError) as exc:
            return _template_error_response(exc)

    def _list_template_market(
        self,
        *,
        params: dict[str, list[str]],
        identity: UserIdentity | None,
    ) -> ApiResponse:
        if self.template_market_store is None:
            return _template_service_unavailable()
        try:
            _reject_unknown_params(
                params,
                {
                    "q",
                    "visibility",
                    "partition",
                    "gpu",
                    "verification_environment",
                    "verified",
                    "limit",
                    "cursor",
                },
            )
            actor = "" if identity is None else identity.username
            course_scopes = self.template_role_directory.visible_course_scopes(actor)
            query = _optional_query_text(params, "q", max_length=200)
            visibility = _optional_query_enum(params, "visibility", TemplateVisibility)
            partition = _optional_query_text(params, "partition", max_length=64)
            gpu = _optional_query_bool(params, "gpu")
            verified = _optional_query_bool(params, "verified")
            environment = _optional_query_text(
                params,
                "verification_environment",
                max_length=16,
            )
            if environment not in {None, "docker", "real107_cpu", "real107_gpu"}:
                raise ValueError(
                    "verification_environment must be one of: docker, real107_cpu, real107_gpu"
                )
            limit = _query_limit(params)
            filters = {
                "actor": actor,
                "course_scopes": sorted(course_scopes),
                "q": query,
                "visibility": None if visibility is None else visibility.value,
                "partition": partition,
                "gpu": gpu,
                "verification_environment": environment,
                "verified": verified is True,
            }
            scope = cursor_scope("template_market", filters)
            cursor = _query_cursor(params, kind="template_market", scope=scope)
            records, next_position = self.template_market_store.list_market_page(
                actor=actor,
                course_scopes=course_scopes,
                query=query,
                visibility=visibility,
                partition=partition,
                gpu=gpu,
                verification_environment=environment,
                verified_only=verified is True,
                cursor=cursor,
                limit=limit,
            )
            return _page_response(
                items=[template_market_item_payload(record) for record in records],
                limit=limit,
                next_position=next_position,
                kind="template_market",
                scope=scope,
            )
        except (CursorError, TemplateMarketError, ValueError) as exc:
            return _template_error_response(exc)

    def _list_template_verifications(
        self,
        *,
        template_id: str,
        release_version: str,
        params: dict[str, list[str]],
        identity: UserIdentity | None,
    ) -> ApiResponse:
        if self.template_market_store is None:
            return _template_service_unavailable()
        try:
            _reject_unknown_params(params, {"limit"})
            limit = _query_limit(params, default=20)
            release = self.template_market_store.get_release_by_version(
                template_id, release_version
            )
            actor = "" if identity is None else identity.username
            authorize_template_release(
                release,
                actor=actor,
                course_scopes=self.template_role_directory.visible_course_scopes(actor),
            )
            records = self.template_market_store.list_verifications(
                release.release_id,
                limit=limit,
            )
            return ApiResponse(
                status=200,
                payload={
                    "items": [template_verification_payload(record) for record in records],
                    "limit": limit,
                },
            )
        except KeyError:
            return _template_not_found("release", f"{template_id}@{release_version}")
        except (TemplateMarketError, ValueError) as exc:
            return _template_error_response(exc)

    def _diff_template_releases(
        self,
        *,
        template_id: str,
        params: dict[str, list[str]],
        identity: UserIdentity | None,
    ) -> ApiResponse:
        if self.template_market_store is None:
            return _template_service_unavailable()
        try:
            _reject_unknown_params(params, {"from", "to"})
            from_version = _optional_query_text(params, "from", max_length=64)
            to_version = _optional_query_text(params, "to", max_length=64)
            if from_version is None or to_version is None:
                raise ValueError("from and to release versions are required")
            before = self.template_market_store.get_release_by_version(template_id, from_version)
            after = self.template_market_store.get_release_by_version(template_id, to_version)
            actor = "" if identity is None else identity.username
            course_scopes = self.template_role_directory.visible_course_scopes(actor)
            authorize_template_release(
                before,
                actor=actor,
                course_scopes=course_scopes,
            )
            authorize_template_release(
                after,
                actor=actor,
                course_scopes=course_scopes,
            )
            return ApiResponse(
                status=200,
                payload=_template_release_diff_payload(before, after),
            )
        except KeyError:
            return _template_not_found("release", template_id)
        except (TemplateMarketError, ValueError) as exc:
            return _template_error_response(exc)

    def _get_template_draft(
        self,
        *,
        draft_id: str,
        params: dict[str, list[str]],
        identity: UserIdentity | None,
    ) -> ApiResponse:
        if self.template_market_store is None:
            return _template_service_unavailable()
        try:
            _reject_unknown_params(params, {"owner"})
            owner = _query_owner(params, identity)
            record = self.template_market_store.get_draft(draft_id, owner=owner)
            return ApiResponse(status=200, payload=template_draft_payload(record))
        except PermissionError:
            return _forbidden_query_response()
        except KeyError:
            return _template_not_found("draft", draft_id)
        except (TemplateMarketError, ValueError) as exc:
            return _template_error_response(exc)

    def _get_template_release(
        self,
        *,
        template_id: str,
        release_version: str,
        identity: UserIdentity | None,
    ) -> ApiResponse:
        if self.template_market_store is None:
            return _template_service_unavailable()
        try:
            release = self.template_market_store.get_release_by_version(
                template_id, release_version
            )
            actor = "" if identity is None else identity.username
            authorize_template_release(
                release,
                actor=actor,
                course_scopes=self.template_role_directory.visible_course_scopes(actor),
            )
            return ApiResponse(status=200, payload=template_release_payload(release))
        except KeyError:
            return _template_not_found("release", f"{template_id}@{release_version}")
        except TemplateMarketError as exc:
            return _template_error_response(exc)

    def _create_template_draft(
        self,
        *,
        body: bytes,
        identity: UserIdentity | None,
    ) -> ApiResponse:
        if self.template_market_store is None:
            return _template_service_unavailable()
        payload, error = _json_body(body)
        if error is not None:
            return error
        try:
            _reject_unknown_body(
                payload,
                {
                    "owner",
                    "title",
                    "description",
                    "visibility",
                    "scope_key",
                    "payload",
                    "compatibility",
                    "publication",
                },
            )
        except ValueError as exc:
            return _template_error_response(exc)
        owner, error = _owner_from_payload_or_identity(payload, identity)
        if error is not None:
            return error
        try:
            record = self.template_market_store.create_draft(
                owner=owner,
                title=_required_body_string(payload, "title"),
                description=_optional_body_string(payload, "description") or "",
                visibility=TemplateVisibility(_required_body_string(payload, "visibility")),
                scope_key=_optional_body_string(payload, "scope_key"),
                payload=_required_body_mapping(payload, "payload"),
                compatibility=_required_body_mapping(payload, "compatibility"),
                publication=_required_body_mapping(payload, "publication"),
            )
            return ApiResponse(status=201, payload=template_draft_payload(record))
        except (TemplateMarketError, TypeError, ValueError) as exc:
            return _template_error_response(exc)

    def _update_template_draft(
        self,
        *,
        draft_id: str,
        body: bytes,
        identity: UserIdentity | None,
    ) -> ApiResponse:
        if self.template_market_store is None:
            return _template_service_unavailable()
        payload, error = _json_body(body)
        if error is not None:
            return error
        try:
            _reject_unknown_body(
                payload,
                {
                    "owner",
                    "expected_version",
                    "title",
                    "description",
                    "visibility",
                    "scope_key",
                    "payload",
                    "compatibility",
                    "publication",
                },
            )
        except ValueError as exc:
            return _template_error_response(exc)
        owner, error = _owner_from_payload_or_identity(payload, identity)
        if error is not None:
            return error
        try:
            current = self.template_market_store.get_draft(draft_id, owner=owner)
            record = self.template_market_store.update_draft(
                draft_id,
                owner=owner,
                expected_version=_required_body_int(payload, "expected_version"),
                title=(
                    current.title
                    if "title" not in payload
                    else _required_body_string(payload, "title")
                ),
                description=(
                    current.description
                    if "description" not in payload
                    else _optional_body_string(payload, "description") or ""
                ),
                visibility=(
                    current.visibility
                    if "visibility" not in payload
                    else TemplateVisibility(_required_body_string(payload, "visibility"))
                ),
                scope_key=(
                    current.scope_key
                    if "scope_key" not in payload
                    else _optional_body_string(payload, "scope_key")
                ),
                payload=(
                    current.payload
                    if "payload" not in payload
                    else _required_body_mapping(payload, "payload")
                ),
                compatibility=(
                    current.compatibility
                    if "compatibility" not in payload
                    else _required_body_mapping(payload, "compatibility")
                ),
                publication=(
                    current.publication
                    if "publication" not in payload
                    else _required_body_mapping(payload, "publication")
                ),
            )
            return ApiResponse(status=200, payload=template_draft_payload(record))
        except KeyError:
            return _template_not_found("draft", draft_id)
        except (TemplateMarketError, TypeError, ValueError) as exc:
            return _template_error_response(exc)

    def _mutate_template_draft(
        self,
        *,
        draft_id: str,
        action: str,
        body: bytes,
        identity: UserIdentity | None,
    ) -> ApiResponse:
        if self.template_market_store is None:
            return _template_service_unavailable()
        payload, error = _json_body(body)
        if error is not None:
            return error
        allowed_by_action = {
            "validate": {"owner"},
            "reviews": {"owner", "expected_version"},
            "publish": {
                "owner",
                "review_id",
                "release_version",
                "request_key",
            },
        }
        try:
            _reject_unknown_body(payload, allowed_by_action[action])
        except ValueError as exc:
            return _template_error_response(exc)
        owner, error = _owner_from_payload_or_identity(payload, identity)
        if error is not None:
            return error
        try:
            if action == "validate":
                result = self.template_market_store.validate_draft(draft_id, owner=owner)
                return ApiResponse(status=200, payload=result.as_payload())
            if action == "reviews":
                review = self.template_market_store.submit_review(
                    draft_id,
                    owner=owner,
                    expected_version=_required_body_int(payload, "expected_version"),
                )
                return ApiResponse(status=201, payload=template_review_payload(review))
            review_id = _required_body_string(payload, "review_id")
            try:
                review = self.template_market_store.get_review(review_id)
            except KeyError:
                return _template_not_found("review", review_id)
            if review.draft_id != draft_id:
                raise TemplateMarketError(
                    "review does not belong to this draft",
                    code="TEMPLATE.REVIEW_STALE",
                )
            self.template_market_store.get_draft(draft_id, owner=owner)
            release = self.template_market_store.publish(
                review_id,
                owner=owner,
                release_version=_required_body_string(payload, "release_version"),
                request_key=_required_body_string(payload, "request_key"),
            )
            return ApiResponse(status=201, payload=template_release_payload(release))
        except KeyError:
            return _template_not_found("draft", draft_id)
        except (TemplateMarketError, TypeError, ValueError) as exc:
            return _template_error_response(exc)

    def _decide_template_review(
        self,
        *,
        review_id: str,
        body: bytes,
        identity: UserIdentity | None,
    ) -> ApiResponse:
        if self.template_market_store is None:
            return _template_service_unavailable()
        if identity is None:
            return ApiResponse(status=401, payload={"error": {"code": "AUTH.MISSING"}})
        payload, error = _json_body(body)
        if error is not None:
            return error
        try:
            _reject_unknown_body(payload, {"expected_version", "approve", "note"})
        except ValueError as exc:
            return _template_error_response(exc)
        try:
            approve = payload.get("approve")
            if not isinstance(approve, bool):
                raise ValueError("approve must be boolean")
            review = self.template_market_store.decide_review(
                review_id,
                principal=self.template_role_directory.reviewer_principal(identity.username),
                expected_version=_required_body_int(payload, "expected_version"),
                approve=approve,
                note=_optional_body_string(payload, "note"),
            )
            return ApiResponse(status=200, payload=template_review_payload(review))
        except KeyError:
            return _template_not_found("review", review_id)
        except (TemplateMarketError, TypeError, ValueError) as exc:
            return _template_error_response(exc)

    def _mutate_template_release(
        self,
        *,
        template_id: str,
        release_version: str,
        action: str,
        body: bytes,
        identity: UserIdentity | None,
    ) -> ApiResponse:
        if self.template_market_store is None:
            return _template_service_unavailable()
        if identity is None:
            return ApiResponse(status=401, payload={"error": {"code": "AUTH.MISSING"}})
        payload, error = _json_body(body)
        if error is not None:
            return error
        allowed_by_action = {
            "adopt": {"request_key"},
            "withdraw": {"reason"},
            "verify": {"run_id", "request_key"},
        }
        try:
            _reject_unknown_body(payload, allowed_by_action[action])
        except ValueError as exc:
            return _template_error_response(exc)
        actor = identity.username
        try:
            release = self.template_market_store.get_release_by_version(
                template_id, release_version
            )
        except KeyError:
            return _template_not_found("release", f"{template_id}@{release_version}")
        try:
            if action == "adopt":
                adoption = self.template_market_store.adopt_release(
                    release.release_id,
                    adopter=actor,
                    request_key=_required_body_string(payload, "request_key"),
                    course_scopes=self.template_role_directory.visible_course_scopes(actor),
                )
                return ApiResponse(status=201, payload=template_adoption_payload(adoption))
            if action == "verify":
                if self.template_verification_service is None:
                    return ApiResponse(
                        status=503,
                        payload={"error": {"code": "template_verification_unavailable"}},
                    )
                verification = self.template_verification_service.verify_from_run(
                    release_id=release.release_id,
                    run_id=_required_body_string(payload, "run_id"),
                    actor=actor,
                    request_key=_required_body_string(payload, "request_key"),
                )
                return ApiResponse(
                    status=201,
                    payload=template_verification_payload(verification),
                )
            self.template_market_store.withdraw_release(
                release.release_id,
                actor=actor,
                reason=_required_body_string(payload, "reason"),
            )
            return ApiResponse(
                status=200,
                payload={"release_id": release.release_id, "withdrawn": True},
            )
        except KeyError as exc:
            if action != "verify":
                return _template_error_response(exc)
            run_id = str(exc.args[0]) if exc.args else _optional_body_string(payload, "run_id")
            return ApiResponse(
                status=404,
                payload={"error": {"code": "run_not_found", "run_id": run_id}},
            )
        except (TemplateMarketError, TypeError, ValueError) as exc:
            return _template_error_response(exc)

    def _resolve_identity(
        self,
        headers: Mapping[str, str] | None,
    ) -> tuple[UserIdentity | None, ApiResponse | None]:
        resolution = resolve_trusted_header_identity(
            headers,
            header_name=self.trusted_user_header,
            required=self.auth_required,
        )
        if resolution.error == IdentityResolutionError.MISSING:
            return None, ApiResponse(status=401, payload={"error": {"code": "AUTH.MISSING"}})
        if resolution.error == IdentityResolutionError.FORBIDDEN:
            return None, ApiResponse(status=403, payload={"error": {"code": "AUTH.FORBIDDEN"}})
        return resolution.identity, None

    def _proxy_auth_error(
        self,
        method: str,
        path: str,
        body: bytes,
        headers: Mapping[str, str] | None,
    ) -> ApiResponse | None:
        if self.proxy_authenticator is None or _is_public_health_path(path):
            return None
        if self.proxy_authenticator.verify(
            method=method,
            target=path,
            body=body,
            headers=headers or {},
            trusted_user_header=self.trusted_user_header,
        ):
            return None
        return ApiResponse(
            status=403,
            payload={"error": {"code": "AUTH.PROXY_SIGNATURE_INVALID"}},
        )

    def _submit_request_from_contract(
        self,
        payload: dict[str, Any],
        *,
        identity: UserIdentity | None,
    ) -> tuple[RunSubmitRequest, ApiResponse | None]:
        if self.contract_service is None:
            return _empty_submit_request(), ApiResponse(
                status=503,
                payload={"error": {"code": "contract_service_unavailable"}},
            )
        contract_id = str(payload.get("contract_id", "")).strip()
        if not contract_id:
            return _empty_submit_request(), ApiResponse(
                status=400,
                payload={
                    "error": {
                        "code": "invalid_contract_request",
                        "message": "contract_id is required",
                    }
                },
            )
        try:
            contract = self.contract_service.get(contract_id)
            access_error = _assert_owner_access(identity, contract.owner)
            if access_error is not None:
                return _empty_submit_request(), access_error
            return (
                self.contract_service.to_submit_request(
                    contract,
                    parent_run_id=_optional_string(payload, "parent_run_id"),
                    lineage_reason=_optional_string(payload, "lineage_reason"),
                    remediation_plan_id=_optional_string(payload, "remediation_plan_id"),
                ),
                None,
            )
        except KeyError:
            return _empty_submit_request(), ApiResponse(
                status=404,
                payload={"error": {"code": "contract_not_found", "contract_id": contract_id}},
            )
        except (ContractError, TypeError, ValueError) as exc:
            return _empty_submit_request(), _contract_error_response(exc)


def build_api(
    *,
    db_path: Path,
    evidence_root: Path,
    auth_required: bool = False,
    trusted_user_header: str = "X-Pilot107-User",
) -> Pilot107HttpApi:
    store = RunStore(db_path)
    contract_store = ContractStore(db_path)
    catalog = RecipeCatalog(store=contract_store)
    platform_snapshot_store = PlatformSnapshotStore(db_path)
    user_entitlement_store = UserEntitlementStore(db_path)
    contract_service = ContractService(
        catalog=catalog,
        store=contract_store,
        platform_snapshot_store=platform_snapshot_store,
        user_entitlement_store=user_entitlement_store,
    )
    return Pilot107HttpApi(
        store=store,
        control_repository=SQLiteControlRepository(db_path),
        auth_required=auth_required,
        trusted_user_header=trusted_user_header,
        recipe_catalog=catalog,
        contract_service=contract_service,
        template_market_store=TemplateMarketStore(
            db_path,
            publication_gate=TemplatePublicationGate(contract_service),
            contract_service=contract_service,
        ),
        evidence_query=EvidenceQueryService(
            store=store,
            evidence_store=EvidenceStore(evidence_root),
        ),
        capsule_service=RawCapsuleService(
            store=store,
            evidence_store=EvidenceStore(evidence_root),
            capsule_root=evidence_root.parent / "capsules",
            creator="pilot107-api",
        ),
        platform_snapshot_store=platform_snapshot_store,
        user_entitlement_store=user_entitlement_store,
    )


def _is_public_health_path(path: str) -> bool:
    route = urlparse(path).path.rstrip("/") or "/"
    return route in {
        "/healthz",
        "/health/live",
        "/health/ready",
        "/api/v1/health/live",
        "/api/v1/health/ready",
    }


def make_handler(api: Pilot107HttpApi) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "pilot107-api/0.1"

        def do_GET(self) -> None:  # noqa: N802
            started = time.monotonic()
            if urlparse(self.path).path == "/metrics":
                self._send_metrics()
                return
            if not _is_public_health_path(self.path) and not self._allow_request():
                api.metrics.observe_request(
                    method="GET",
                    route=normalize_http_route(self.path),
                    status=429,
                    duration_seconds=time.monotonic() - started,
                )
                return
            stream_run_id = _sse_run_id(self.path)
            if stream_run_id is not None:
                stream_status = self._send_event_stream(stream_run_id)
                api.metrics.observe_request(
                    method="GET",
                    route="/api/v1/runs/{run_id}/events/stream",
                    status=stream_status,
                    duration_seconds=time.monotonic() - started,
                )
                return
            response = api.handle_get(self.path, headers=dict(self.headers.items()))
            self._send_json(response)
            api.metrics.observe_request(
                method="GET",
                route=normalize_http_route(self.path),
                status=response.status,
                duration_seconds=time.monotonic() - started,
            )

        def do_POST(self) -> None:  # noqa: N802
            started = time.monotonic()
            if not self._allow_request():
                self._observe_rejected("POST", 429, started)
                return
            body, error = self._read_request_body()
            if error is not None:
                self._send_json(error)
                self._observe_rejected("POST", error.status, started)
                return
            assert body is not None
            response = api.handle_post(self.path, body=body, headers=dict(self.headers.items()))
            self._send_json(response)
            api.metrics.observe_request(
                method="POST",
                route=normalize_http_route(self.path),
                status=response.status,
                duration_seconds=time.monotonic() - started,
            )

        def do_PATCH(self) -> None:  # noqa: N802
            started = time.monotonic()
            if not self._allow_request():
                self._observe_rejected("PATCH", 429, started)
                return
            body, error = self._read_request_body()
            if error is not None:
                self._send_json(error)
                self._observe_rejected("PATCH", error.status, started)
                return
            assert body is not None
            response = api.handle_patch(self.path, body=body, headers=dict(self.headers.items()))
            self._send_json(response)
            api.metrics.observe_request(
                method="PATCH",
                route=normalize_http_route(self.path),
                status=response.status,
                duration_seconds=time.monotonic() - started,
            )

        def log_message(self, format: str, *args: object) -> None:
            return

        def _send_json(self, response: ApiResponse) -> None:
            body = (
                b""
                if response.status == 304
                else json.dumps(response.payload, ensure_ascii=False, sort_keys=True).encode(
                    "utf-8"
                )
            )
            if len(body) > api.max_response_body_bytes:
                response = ApiResponse(
                    status=500,
                    payload={"error": {"code": "HTTP.RESPONSE_TOO_LARGE"}},
                )
                body = b'{"error":{"code":"HTTP.RESPONSE_TOO_LARGE"}}'
            self.send_response(response.status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for key, value in (response.headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            if body:
                self.wfile.write(body)

        def _allow_request(self) -> bool:
            allowed, retry_after = api.rate_limiter.check(self.client_address[0])
            if allowed:
                return True
            self._send_json(
                ApiResponse(
                    status=429,
                    payload={"error": {"code": "HTTP.RATE_LIMITED"}},
                    headers={"Retry-After": str(retry_after)},
                )
            )
            return False

        def _read_request_body(self) -> tuple[bytes | None, ApiResponse | None]:
            if self.headers.get("Transfer-Encoding"):
                return None, ApiResponse(
                    status=400,
                    payload={"error": {"code": "HTTP.TRANSFER_ENCODING_UNSUPPORTED"}},
                )
            value = self.headers.get("Content-Length", "0") or "0"
            try:
                length = int(value)
            except ValueError:
                return None, ApiResponse(
                    status=400,
                    payload={"error": {"code": "HTTP.CONTENT_LENGTH_INVALID"}},
                )
            if length < 0:
                return None, ApiResponse(
                    status=400,
                    payload={"error": {"code": "HTTP.CONTENT_LENGTH_INVALID"}},
                )
            if length > api.max_request_body_bytes:
                return None, ApiResponse(
                    status=413,
                    payload={"error": {"code": "HTTP.REQUEST_TOO_LARGE"}},
                )
            return self.rfile.read(length) if length else b"", None

        def _observe_rejected(self, method: str, status: int, started: float) -> None:
            api.metrics.observe_request(
                method=method,
                route=normalize_http_route(self.path),
                status=status,
                duration_seconds=time.monotonic() - started,
            )

        def _send_metrics(self) -> None:
            body = api.metrics.render().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _send_event_stream(self, run_id: str) -> int:
            original_headers = dict(self.headers.items())
            proxy_error = api._proxy_auth_error("GET", self.path, b"", original_headers)
            if proxy_error is not None:
                self._send_json(
                    _finalize_response(
                        proxy_error,
                        request_id=_request_id(original_headers),
                        request_headers=original_headers,
                        enable_etag=False,
                    )
                )
                return proxy_error.status
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query, keep_blank_values=True)
            once_values = params.pop("once", [])
            if len(once_values) > 1 or (once_values and once_values[0] not in {"true", "false"}):
                self._send_json(
                    _finalize_response(
                        _invalid_query_response(ValueError("once must be true or false")),
                        request_id=_request_id(dict(self.headers.items())),
                        request_headers=dict(self.headers.items()),
                        enable_etag=False,
                    )
                )
                return 400
            once = once_values == ["true"]
            if (
                "cursor" not in params
                and "after_event_id" not in params
                and self.headers.get("Last-Event-ID")
            ):
                params["after_event_id"] = [self.headers["Last-Event-ID"]]
            request_headers = {
                key: value for key, value in self.headers.items() if key.lower() != "if-none-match"
            }
            response = api._handle_verified_get(
                _events_query_path(run_id, params),
                headers=request_headers,
            )
            if response.status != 200:
                self._send_json(response)
                return response.status
            try:
                stream_run = api.store.get_run(run_id)
                api.control_repository.record_trace(
                    trace_id=f"trace_{uuid4().hex}",
                    request_id=str((response.headers or {})["X-Request-ID"]),
                    method="GET",
                    route="/api/v1/runs/{run_id}/events/stream",
                    status=200,
                    actor=_header_value(request_headers, api.trusted_user_header),
                    run_id=run_id,
                    job_id=None if stream_run.job_id is None else str(stream_run.job_id),
                )
            except Exception:
                api.metrics.observe_trace_write(outcome="error")
            else:
                api.metrics.observe_trace_write(outcome="success")
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("Connection", "close" if once else "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            for key, value in (response.headers or {}).items():
                if key.lower() != "etag":
                    self.send_header(key, value)
            self.end_headers()
            stream_started = time.monotonic()
            stream_outcome = "deadline"
            stream_events = 0
            api.metrics.sse_opened()
            try:
                last_event_id = int(response.payload["page"]["last_event_id"])
                stream_events += self._write_sse_events(response.payload.get("items", []))
                if once:
                    stream_outcome = "complete"
                    self.close_connection = True
                    return 200
                deadline = time.monotonic() + 25.0
                heartbeat_at = time.monotonic() + 10.0
                polling_params = {
                    key: value
                    for key, value in params.items()
                    if key not in {"cursor", "after_event_id"}
                }
                while time.monotonic() < deadline:
                    polling_params["after_event_id"] = [str(last_event_id)]
                    polled = api._handle_verified_get(
                        _events_query_path(run_id, polling_params),
                        headers=request_headers,
                    )
                    if polled.status != 200:
                        stream_outcome = "poll_error"
                        self._write_sse(
                            event="stream_error",
                            data={"code": "EVENT_STREAM.POLL_FAILED"},
                        )
                        return 200
                    items = polled.payload.get("items", [])
                    stream_events += self._write_sse_events(items)
                    last_event_id = int(polled.payload["page"]["last_event_id"])
                    if time.monotonic() >= heartbeat_at:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                        heartbeat_at = time.monotonic() + 10.0
                    time.sleep(0.5)
            except (BrokenPipeError, ConnectionResetError):
                stream_outcome = "disconnect"
                return 200
            except Exception:
                stream_outcome = "error"
                raise
            finally:
                api.metrics.observe_sse_closed(
                    outcome=stream_outcome,
                    duration_seconds=time.monotonic() - stream_started,
                    events=stream_events,
                )
            return 200

        def _write_sse_events(self, items: object) -> int:
            if not isinstance(items, list):
                return 0
            written = 0
            for item_payload in items:
                if not isinstance(item_payload, dict):
                    continue
                self._write_sse(
                    event="run_event",
                    event_id=int(item_payload["event_id"]),
                    data=_sse_event_summary(item_payload),
                )
                written += 1
            return written

        def _write_sse(
            self,
            *,
            event: str,
            data: dict[str, Any],
            event_id: int | None = None,
        ) -> None:
            if event_id is not None:
                self.wfile.write(f"id: {event_id}\n".encode("ascii"))
            self.wfile.write(f"event: {event}\n".encode("ascii"))
            encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
            self.wfile.write(f"data: {encoded}\n\n".encode())
            self.wfile.flush()

    return Handler


def run_http_server(*, api: Pilot107HttpApi, host: str, port: int) -> None:
    server = ThreadingHTTPServer((host, port), make_handler(api))
    try:
        server.serve_forever()
    finally:
        server.server_close()


def _request_id(headers: Mapping[str, str] | None) -> str:
    candidate = _header_value(headers, "X-Request-ID")
    if candidate is not None and _REQUEST_ID.fullmatch(candidate):
        return candidate
    return f"req_{uuid4().hex}"


def _finalize_response(
    response: ApiResponse,
    *,
    request_id: str,
    request_headers: Mapping[str, str] | None,
    enable_etag: bool,
) -> ApiResponse:
    response_headers = dict(response.headers or {})
    response_headers["X-Request-ID"] = request_id
    payload = response.payload
    if response.status >= 400 and "error" in payload:
        error = payload.get("error")
        if isinstance(error, dict):
            payload = {**payload, "error": {**error, "request_id": request_id}}
    if enable_etag and response.status == 200:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        etag = '"' + hashlib.sha256(encoded).hexdigest() + '"'
        response_headers["ETag"] = etag
        if _header_value(request_headers, "If-None-Match") == etag:
            return ApiResponse(status=304, payload={}, headers=response_headers)
    return ApiResponse(status=response.status, payload=payload, headers=response_headers)


def _header_value(headers: Mapping[str, str] | None, name: str) -> str | None:
    if headers is None:
        return None
    expected = name.lower()
    for key, value in headers.items():
        if key.lower() == expected:
            return value.strip()
    return None


def _trace_identifiers(path: str, payload: Mapping[str, Any]) -> dict[str, str]:
    identifiers: dict[str, str] = {}
    parts = _route_parts(path)
    if len(parts) >= 2 and parts[0] == "runs":
        identifiers["run_id"] = parts[1]
    if len(parts) >= 2 and parts[0] == "remediation-sessions":
        identifiers["session_id"] = parts[1]

    def visit(value: object, depth: int) -> None:
        if depth > 5 or len(identifiers) == 3:
            return
        if isinstance(value, Mapping):
            for key, item in value.items():
                name = str(key)
                if name in {"run_id", "job_id", "session_id"} and item is not None:
                    candidate = str(item)
                    if _REQUEST_ID.fullmatch(candidate):
                        identifiers.setdefault(name, candidate)
                elif name in {"item", "run", "session", "error"}:
                    visit(item, depth + 1)
        elif isinstance(value, list):
            for item in value[:10]:
                visit(item, depth + 1)

    visit(payload, 0)
    return identifiers


def _reject_unknown_params(
    params: dict[str, list[str]],
    allowed: set[str],
) -> None:
    unknown = sorted(set(params) - allowed)
    if unknown:
        raise ValueError(f"unsupported query parameter: {', '.join(unknown)}")


def _query_owner(
    params: dict[str, list[str]],
    identity: UserIdentity | None,
) -> str:
    requested = _optional_query_text(params, "owner", max_length=64)
    if identity is not None:
        if requested is not None and requested != identity.username:
            raise PermissionError("owner query does not match authenticated identity")
        return identity.username
    if requested is None:
        raise ValueError("owner is required when authentication is disabled")
    if not _OWNER.fullmatch(requested):
        raise ValueError("owner is invalid")
    return requested


def _optional_bound_owner(
    requested: str | None,
    identity: UserIdentity | None,
) -> str | None:
    if identity is not None:
        if requested is not None and requested != identity.username:
            raise PermissionError("owner query does not match authenticated identity")
        return identity.username
    if requested is None:
        return None
    if not _OWNER.fullmatch(requested):
        raise ValueError("owner is invalid")
    return requested


def _query_limit(params: dict[str, list[str]], *, default: int = 50) -> int:
    text = _optional_query_text(params, "limit", max_length=3)
    if text is None:
        return default
    try:
        limit = int(text)
    except ValueError as exc:
        raise ValueError("limit must be an integer") from exc
    if limit <= 0 or limit > 100:
        raise ValueError("limit must be between 1 and 100")
    return limit


def _query_cursor(
    params: dict[str, list[str]],
    *,
    kind: str,
    scope: str,
) -> CursorPosition | None:
    value = _optional_query_text(params, "cursor", max_length=2048)
    if value is None:
        return None
    return decode_cursor(value=value, kind=kind, scope=scope)


def _optional_query_text(
    params: dict[str, list[str]],
    name: str,
    *,
    max_length: int,
) -> str | None:
    values = params.get(name)
    if values is None:
        return None
    if len(values) != 1:
        raise ValueError(f"{name} must be provided once")
    value = values[0].strip()
    if not value:
        raise ValueError(f"{name} cannot be empty")
    if len(value) > max_length or "\x00" in value:
        raise ValueError(f"{name} is invalid")
    return value


def _optional_query_bool(params: dict[str, list[str]], name: str) -> bool | None:
    value = _optional_query_text(params, name, max_length=5)
    if value is None:
        return None
    if value == "true":
        return True
    if value == "false":
        return False
    raise ValueError(f"{name} must be true or false")


def _optional_query_enum[T: StrEnum](
    params: dict[str, list[str]],
    name: str,
    enum_type: type[T],
) -> T | None:
    value = _optional_query_text(params, name, max_length=64)
    if value is None:
        return None
    try:
        return enum_type(value)
    except ValueError as exc:
        allowed = ", ".join(item.value for item in enum_type)
        raise ValueError(f"{name} must be one of: {allowed}") from exc


def _optional_query_time(params: dict[str, list[str]], name: str) -> str | None:
    value = _optional_query_text(params, name, max_length=64)
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO 8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{name} must include a timezone")
    return parsed.astimezone(UTC).isoformat()


def _enum_values(
    params: dict[str, list[str]],
    name: str,
    allowed: set[str] | frozenset[str],
) -> tuple[str, ...]:
    raw_values = params.get(name, [])
    values = [item.strip() for raw in raw_values for item in raw.split(",")]
    if any(not item for item in values):
        raise ValueError(f"{name} contains an empty value")
    unknown = sorted(set(values) - set(allowed))
    if unknown:
        raise ValueError(f"unsupported {name}: {', '.join(unknown)}")
    return tuple(sorted(set(values)))


def _event_type_values(params: dict[str, list[str]]) -> tuple[str, ...]:
    values = _enum_values_unchecked(params, "type")
    if any(not _EVENT_TYPE.fullmatch(value) for value in values):
        raise ValueError("event type is invalid")
    return values


def _enum_values_unchecked(
    params: dict[str, list[str]],
    name: str,
) -> tuple[str, ...]:
    raw_values = params.get(name, [])
    values = [item.strip() for raw in raw_values for item in raw.split(",")]
    if any(not item for item in values):
        raise ValueError(f"{name} contains an empty value")
    return tuple(sorted(set(values)))


def _page_response(
    *,
    items: list[dict[str, Any]],
    limit: int,
    next_position: CursorPosition | None,
    kind: str,
    scope: str,
) -> ApiResponse:
    next_cursor = (
        None
        if next_position is None
        else encode_cursor(kind=kind, scope=scope, position=next_position)
    )
    return ApiResponse(
        status=200,
        payload={
            "items": items,
            "page": {
                "limit": limit,
                "has_more": next_cursor is not None,
                "next_cursor": next_cursor,
            },
        },
    )


def _invalid_query_response(exc: Exception) -> ApiResponse:
    return ApiResponse(
        status=400,
        payload={"error": {"code": "invalid_query", "message": str(exc)}},
    )


def _forbidden_query_response() -> ApiResponse:
    return ApiResponse(status=403, payload={"error": {"code": "AUTH.FORBIDDEN"}})


def _contract_summary(contract: ContractRecord) -> dict[str, Any]:
    return {
        "contract_id": contract.contract_id,
        "owner": contract.owner,
        "recipe_version_id": contract.recipe_version_id,
        "schema_version": contract.schema_version,
        "digest": contract.digest,
        "parent_contract_id": contract.parent_contract_id,
        "derivation_reason": contract.derivation_reason,
        "source_advice_id": contract.source_advice_id,
        "source_action_id": contract.source_action_id,
        "created_at": contract.created_at,
        "updated_at": contract.updated_at,
    }


def _agent_advice_summary(advice: AgentAdviceRecord) -> dict[str, Any]:
    actions = advice.payload.get("actions", [])
    return {
        "advice_id": advice.advice_id,
        "run_id": advice.run_id,
        "owner": advice.owner,
        "state": advice.state,
        "version": advice.version,
        "provider": advice.provider,
        "model": advice.model,
        "summary": advice.payload.get("summary"),
        "action_count": len(actions) if isinstance(actions, list) else 0,
        "created_at": advice.created_at,
        "updated_at": advice.updated_at,
    }


def _run_event_payload(event: RunEvent) -> dict[str, Any]:
    return {
        "event_id": event.event_id,
        "run_id": event.run_id,
        "event_type": event.event_type,
        "payload": event.payload,
        "created_at": event.created_at,
    }


def _run_summary(run: RunRecord) -> dict[str, Any]:
    return {
        "run_id": run.run_id,
        "contract_id": run.contract_id,
        "parent_run_id": run.parent_run_id,
        "lineage_reason": run.lineage_reason,
        "remediation_plan_id": run.remediation_plan_id,
        "attempt": run.attempt,
        "workflow": run.workflow,
        "retry_not_before": run.retry_not_before,
        "owner": run.owner,
        "state": run.state.value,
        "terminal_state": run.terminal_state,
        "exit_code": run.exit_code,
        "result_status": run.result_status.value,
        "collection_state": run.collection_state.value,
        "diagnosis_state": run.diagnosis_state.value,
        "capsule_state": run.capsule_state.value,
        "job_id": run.job_id,
        "workdir": run.workdir,
        "submit_strategy": run.submit_strategy,
        "created_at": run.created_at,
        "updated_at": run.updated_at,
    }


def _capsule_payload(capsule: Any) -> dict[str, Any]:
    return {
        "run_id": capsule.run_id,
        "capsule_id": capsule.capsule_id,
        "manifest_sha256": capsule.manifest_sha256,
        "files_copied": capsule.files_copied,
        "warnings": capsule.warnings,
    }


def _capsule_read_payload(capsule: Any) -> dict[str, Any]:
    return {
        "run_id": capsule.run_id,
        "capsule_id": capsule.capsule_id,
        "manifest_sha256": capsule.manifest_sha256,
        "files_copied": capsule.files_copied,
        "manifest": capsule.manifest,
        "valid": capsule.valid,
        "checked_files": capsule.checked_files,
        "warnings": capsule.warnings,
        "errors": capsule.errors,
    }


def _diagnosis_payload(diagnosis: Any) -> dict[str, Any]:
    return {
        "diagnosis_id": diagnosis.diagnosis_id,
        "run_id": diagnosis.run_id,
        "rule_id": diagnosis.rule_id,
        "severity": diagnosis.severity,
        "summary": diagnosis.summary,
        "evidence_refs": diagnosis.evidence_refs,
        "suggested_patch": diagnosis.suggested_patch,
        "retryable": diagnosis.retryable,
        "confidence": diagnosis.confidence,
        "category": diagnosis.category,
        "stage": diagnosis.stage,
        "fix_guide": diagnosis.fix_guide,
        "created_at": diagnosis.created_at,
    }


def _known_error_summary(rule: KnownErrorRule) -> dict[str, Any]:
    return {
        "error_id": rule.error_id,
        "category": rule.category,
        "severity": rule.severity,
        "retryable": rule.retryable,
        "stage": rule.stage,
        "title": rule.title,
    }


def _known_error_detail(rule: KnownErrorRule) -> dict[str, Any]:
    return {
        **_known_error_summary(rule),
        "symptoms": list(rule.symptoms),
        "evidence_paths": list(rule.evidence_paths),
        "root_cause": rule.root_cause,
        "fix_template": rule.fix_template,
        "fix_guide": rule.fix_guide,
        "confidence": rule.confidence,
        "kb_article": rule.kb_article,
        "terminal_state_match": rule.terminal_state_match,
        "state_match": rule.state_match,
    }


def _llm_provider_from_env(
    *, observer: ControlPlaneMetrics | None = None
) -> OpenAICompatibleLLMProvider | None:
    try:
        return OpenAICompatibleLLMProvider.from_env(observer=observer)
    except ValueError:
        return None


def _agent_suggest_degraded(reason: str, explanation_zh: str) -> dict[str, Any]:
    """Build a 200-level degraded payload so the frontend can distinguish
    LLM failures from the intentional ``provider=none`` empty-patch case.
    """
    return {
        "status": "degraded",
        "reason": reason,
        "suggested_patch": {},
        "explanation_zh": explanation_zh,
        "needs_user_confirmation": False,
    }


def _agent_suggest_degraded_explanation_zh(reason: str) -> str:
    messages = {
        "provider_unconfigured": "未配置 LLM 网关，无法生成建议。",
        "provider_invalid_key": "LLM 网关认证失败，请检查 API Key 配置。",
        "provider_timeout": "LLM 网关请求超时，请稍后重试。",
        "provider_transport_error": "LLM 网关连接失败，请检查网络或网关状态。",
        "provider_schema_error": "LLM 返回内容不符合预期结构，未能生成建议。",
        "provider_parse_error": "LLM 返回内容解析失败，未能生成建议。",
    }
    return messages.get(reason, "LLM 调用失败，未能生成建议。")


def _agent_suggest_reason_for_error(exc: AgentProviderError) -> str:
    """Map an ``AgentProviderError.code`` to a stable degraded reason string."""
    code = exc.code
    if code.startswith("invalid_schema"):
        return "provider_schema_error"
    if code in {"invalid_json", "invalid_response"}:
        return "provider_parse_error"
    if code in {"invalid_citation", "incomplete_citations"}:
        return "provider_schema_error"
    if code.startswith("http_"):
        try:
            status = int(code.removeprefix("http_"))
        except ValueError:
            return "provider_transport_error"
        if status in {401, 403}:
            return "provider_invalid_key"
        if status == 408:
            return "provider_timeout"
        return "provider_transport_error"
    if code == "transport_error":
        # ``TimeoutError`` and ``URLError`` both surface as ``transport_error``;
        # distinguish by the wrapped cause so timeouts are reported distinctly.
        cause = exc.__cause__
        if isinstance(cause, TimeoutError):
            return "provider_timeout"
        return "provider_transport_error"
    return "provider_transport_error"


def _route_parts(path: str) -> list[str]:
    route = path.rstrip("/") or "/"
    parts = [unquote(part) for part in route.split("/") if part]
    if len(parts) >= 2 and parts[:2] == ["api", "v1"]:
        return parts[2:]
    return parts


def _sse_run_id(path: str) -> str | None:
    parsed = urlparse(path)
    parts = _route_parts(parsed.path)
    if len(parts) == 4 and parts[0] == "runs" and parts[2:] == ["events", "stream"]:
        return parts[1]
    return None


def _events_query_path(run_id: str, params: dict[str, list[str]]) -> str:
    pairs = [(key, value) for key, values in params.items() for value in values]
    query = urlencode(pairs)
    path = f"/api/v1/runs/{quote(run_id, safe='')}/events"
    return path if not query else f"{path}?{query}"


def _sse_event_summary(payload: dict[str, Any]) -> dict[str, Any]:
    event_payload = payload.get("payload")
    source = event_payload if isinstance(event_payload, dict) else {}
    summary_keys = {
        "state",
        "collection_state",
        "diagnosis_state",
        "capsule_state",
        "job_id",
        "code",
        "retryable",
        "attempt",
        "max_attempts",
        "retry_run_id",
        "derived_run_id",
    }
    return {
        "event_id": payload.get("event_id"),
        "run_id": payload.get("run_id"),
        "event_type": payload.get("event_type"),
        "created_at": payload.get("created_at"),
        "summary": {key: source[key] for key in sorted(summary_keys & set(source))},
    }


def _json_body(body: bytes) -> tuple[dict[str, Any], ApiResponse | None]:
    try:
        payload = json.loads(body.decode("utf-8") if body else "{}")
    except json.JSONDecodeError as exc:
        return {}, ApiResponse(
            status=400,
            payload={"error": {"code": "invalid_json", "message": str(exc)}},
        )
    if not isinstance(payload, dict):
        return {}, ApiResponse(
            status=400,
            payload={"error": {"code": "invalid_json_body", "message": "body must be an object"}},
        )
    return payload, None


def _submit_request_from_payload(
    payload: dict[str, Any],
    *,
    identity: UserIdentity | None,
) -> tuple[RunSubmitRequest, ApiResponse | None]:
    try:
        resource_plan_payload = payload["resource_plan"]
        if not isinstance(resource_plan_payload, dict):
            raise TypeError("resource_plan must be an object")
        owner = identity.username if identity is not None else str(payload["owner"])
        if (
            identity is not None
            and "owner" in payload
            and str(payload["owner"]) != identity.username
        ):
            return _empty_submit_request(), ApiResponse(
                status=403,
                payload={"error": {"code": "AUTH.FORBIDDEN"}},
            )
        request = RunSubmitRequest(
            owner=owner,
            workdir=Path(str(payload["workdir"])),
            script=str(payload["script"]),
            resource_plan=_resource_plan_from_payload(resource_plan_payload),
            parent_run_id=_optional_string(payload, "parent_run_id"),
            lineage_reason=_optional_string(payload, "lineage_reason"),
            remediation_plan_id=_optional_string(payload, "remediation_plan_id"),
            workflow=WorkflowPolicy.from_payload(payload.get("workflow")),
        )
    except (KeyError, TypeError, ValueError) as exc:
        return _empty_submit_request(), ApiResponse(
            status=400,
            payload={"error": {"code": "invalid_submit_request", "message": str(exc)}},
        )
    return request, None


def _empty_submit_request() -> RunSubmitRequest:
    return RunSubmitRequest(
        owner="",
        workdir=Path("."),
        script="",
        resource_plan=ResourcePlan(
            partition="",
            qos=None,
            nodes=1,
            ntasks=1,
            cpus_per_task=1,
            time_limit="00:01:00",
        ),
    )


def _optional_string(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string or null")
    return value.strip()


def _resource_plan_from_payload(payload: dict[str, Any]) -> ResourcePlan:
    array_payload = payload.get("array")
    return ResourcePlan(
        partition=str(payload["partition"]),
        qos=None if payload.get("qos") is None else str(payload["qos"]),
        nodes=int(payload["nodes"]),
        ntasks=int(payload["ntasks"]),
        cpus_per_task=int(payload["cpus_per_task"]),
        memory_value=(
            None if payload.get("memory_value") is None else int(payload["memory_value"])
        ),
        memory_unit=None if payload.get("memory_unit") is None else str(payload["memory_unit"]),
        gpus_per_node=(
            None if payload.get("gpus_per_node") is None else int(payload["gpus_per_node"])
        ),
        gpus_total=None if payload.get("gpus_total") is None else int(payload["gpus_total"]),
        gpu_type=None if payload.get("gpu_type") is None else str(payload["gpu_type"]),
        time_limit=None if payload.get("time_limit") is None else str(payload["time_limit"]),
        array=None
        if array_payload is None
        else ArraySpec(
            expression=str(array_payload["expression"]),
            max_concurrency=None
            if array_payload.get("max_concurrency") is None
            else int(array_payload["max_concurrency"]),
        ),
    )


def _script_artifacts(run: RunRecord) -> dict[str, str]:
    return {
        "user_script_sha256": _sha256_text(run.script),
        "submitted_script_sha256": _sha256_text(run.script),
        "wrapper_sha256": _sha256_text(generated_execution_wrapper(run)),
    }


def _sha256_text(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _finding_payload(finding: Any) -> dict[str, Any]:
    return {
        "severity": finding.severity.value,
        "code": finding.code,
        "message": finding.message,
        "source_authority": finding.source_authority,
    }


def _assert_run_access(identity: UserIdentity | None, run: RunRecord) -> ApiResponse | None:
    return _assert_owner_access(identity, run.owner)


def _assert_owner_access(identity: UserIdentity | None, owner: str) -> ApiResponse | None:
    if identity is None:
        return None
    if owner != identity.username:
        return ApiResponse(
            status=403,
            payload={
                "error": {
                    "code": "AUTH.FORBIDDEN",
                    "message": "run is not owned by current identity",
                }
            },
        )
    return None


def _owner_from_payload_or_identity(
    payload: dict[str, Any],
    identity: UserIdentity | None,
) -> tuple[str, ApiResponse | None]:
    if identity is not None:
        if "owner" in payload and str(payload["owner"]) != identity.username:
            return "", ApiResponse(status=403, payload={"error": {"code": "AUTH.FORBIDDEN"}})
        return identity.username, None
    try:
        owner = str(payload["owner"]).strip()
    except KeyError:
        return "", ApiResponse(
            status=400,
            payload={"error": {"code": "invalid_contract_request", "message": "owner is required"}},
        )
    if not owner:
        return "", ApiResponse(
            status=400,
            payload={"error": {"code": "invalid_contract_request", "message": "owner is required"}},
        )
    return owner, None


def _required_body_string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return value.strip()


def _reject_unknown_body(payload: dict[str, Any], allowed: set[str]) -> None:
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise ValueError(f"unsupported request field: {', '.join(unknown)}")


def _optional_body_string(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    return value.strip() or None


def _required_body_mapping(payload: dict[str, Any], key: str) -> dict[str, Any]:
    value = payload.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"{key} must be an object")
    return value


def _required_body_int(payload: dict[str, Any], key: str) -> int:
    value = payload.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{key} must be a positive integer")
    return value


def _template_service_unavailable() -> ApiResponse:
    return ApiResponse(
        status=503,
        payload={"error": {"code": "template_service_unavailable"}},
    )


def _template_not_found(kind: str, object_id: str) -> ApiResponse:
    return ApiResponse(
        status=404,
        payload={"error": {"code": f"template_{kind}_not_found", "id": object_id}},
    )


def _template_error_response(exc: Exception) -> ApiResponse:
    if not isinstance(exc, TemplateMarketError):
        return ApiResponse(
            status=400,
            payload={"error": {"code": "TEMPLATE.INVALID_REQUEST", "message": str(exc)}},
        )
    if exc.code in {
        "TEMPLATE.FORBIDDEN",
        "TEMPLATE.REVIEW_FORBIDDEN",
        "TEMPLATE.SELF_REVIEW_FORBIDDEN",
    }:
        status = 403
    elif exc.code in {
        "TEMPLATE.PUBLICATION_BLOCKED",
        "TEMPLATE.CONTRACT_BLOCKED",
        "TEMPLATE.VERIFICATION_ENVIRONMENT_MISMATCH",
        "TEMPLATE.VERIFICATION_CAPSULE_INCOMPLETE",
        "TEMPLATE.VERIFICATION_EVIDENCE_INCOMPLETE",
        "TEMPLATE.VERIFICATION_LINEAGE_INVALID",
        "TEMPLATE.VERIFICATION_RUN_NOT_READY",
    }:
        status = 422
    elif exc.code in {
        "TEMPLATE.GATE_UNAVAILABLE",
        "TEMPLATE.CONTRACT_SERVICE_UNAVAILABLE",
    }:
        status = 503
    elif exc.code in {
        "TEMPLATE.DRAFT_CONFLICT",
        "TEMPLATE.REVIEW_CONFLICT",
        "TEMPLATE.REVIEW_NOT_APPROVED",
        "TEMPLATE.REVIEW_STALE",
        "TEMPLATE.RELEASE_CONFLICT",
        "TEMPLATE.RELEASE_WITHDRAWN",
        "TEMPLATE.PUBLICATION_GATE_STALE",
        "TEMPLATE.IDEMPOTENCY_CONFLICT",
        "TEMPLATE.VERIFICATION_CONFLICT",
    }:
        status = 409
    else:
        status = 400
    payload: dict[str, Any] = {
        "error": {"code": exc.code, "message": str(exc)},
    }
    if exc.findings:
        payload["findings"] = list(exc.findings)
    return ApiResponse(status=status, payload=payload)


def _template_release_diff_payload(
    before: TemplateReleaseRecord,
    after: TemplateReleaseRecord,
) -> dict[str, Any]:
    before_content = _template_release_diff_content(before)
    after_content = _template_release_diff_content(after)
    changes: list[dict[str, Any]] = []
    _append_json_changes(changes, "", before_content, after_content)
    return {
        "template_id": before.template_id,
        "from": {
            "release_id": before.release_id,
            "release_version": before.release_version,
            "content_sha256": before.content_sha256,
        },
        "to": {
            "release_id": after.release_id,
            "release_version": after.release_version,
            "content_sha256": after.content_sha256,
        },
        "changes": changes,
    }


def _template_release_diff_content(record: TemplateReleaseRecord) -> dict[str, Any]:
    return {
        "title": record.title,
        "description": record.description,
        "visibility": record.visibility.value,
        "scope_key": record.scope_key,
        "payload": record.payload,
        "compatibility": record.compatibility,
        "publication": record.publication,
    }


def _append_json_changes(
    changes: list[dict[str, Any]],
    path: str,
    before: Any,
    after: Any,
) -> None:
    if isinstance(before, dict) and isinstance(after, dict):
        for key in sorted(set(before) | set(after)):
            child_path = f"{path}/{_json_pointer_segment(key)}"
            if key not in before:
                changes.append({"path": child_path, "before": None, "after": after[key]})
            elif key not in after:
                changes.append({"path": child_path, "before": before[key], "after": None})
            else:
                _append_json_changes(changes, child_path, before[key], after[key])
        return
    if before != after:
        changes.append({"path": path or "/", "before": before, "after": after})


def _json_pointer_segment(value: object) -> str:
    return str(value).replace("~", "~0").replace("/", "~1")


def _contract_error_response(exc: Exception) -> ApiResponse:
    if isinstance(exc, ContractError):
        return ApiResponse(
            status=422,
            payload={
                "error": {"code": exc.code, "message": str(exc)},
                "findings": [_finding_payload(finding) for finding in exc.findings],
            },
        )
    if isinstance(exc, KeyError):
        return ApiResponse(
            status=404,
            payload={"error": {"code": "recipe_not_found", "message": str(exc)}},
        )
    return ApiResponse(
        status=400,
        payload={"error": {"code": "invalid_contract_request", "message": str(exc)}},
    )


def _agent_advice_error_response(exc: AgentAdviceError) -> ApiResponse:
    conflict_codes = {
        "AGENT.ADVICE_CONFLICT",
        "AGENT.ADVICE_STALE",
        "AGENT.NOT_APPROVABLE",
    }
    return ApiResponse(
        status=409 if exc.code in conflict_codes else 422,
        payload={"error": {"code": exc.code, "message": str(exc)}},
    )
