"""Owner-scoped routes for strong market application and publication branches."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from pilot107.agent.market_sessions import (
    MarketApplicationError,
    MarketApplicationService,
    MarketApplicationSourceKind,
    ReferenceAdaptationSession,
    TemplateApplicationSession,
    TemplatePublicationError,
    TemplatePublicationService,
    TemplatePublicationSession,
)
from pilot107.api.http_types import ApiResponse
from pilot107.core.identity import UserIdentity
from pilot107.core.template_market import TemplateVisibility


class MarketSessionRoutes:
    def __init__(
        self,
        *,
        applications: MarketApplicationService | None,
        publications: TemplatePublicationService | None,
    ) -> None:
        self.applications = applications
        self.publications = publications

    def handle_get(
        self,
        parts: list[str],
        *,
        params: Mapping[str, list[str]],
        identity: UserIdentity | None,
    ) -> ApiResponse | None:
        is_application = len(parts) == 2 and parts[0] == "market" and parts[1] == "applications"
        is_application_detail = len(parts) == 3 and parts[:2] == ["market", "applications"]
        is_publication_detail = len(parts) == 2 and parts[0] == "template-publication-sessions"
        if not (is_application or is_application_detail or is_publication_detail):
            return None
        if identity is None:
            return _error(401, "AUTH.MISSING", "authenticated identity is required")
        if params:
            return _error(400, "MARKET.INVALID_QUERY", "query parameters are not allowed")
        owner = identity.username
        try:
            if is_application:
                if self.applications is None:
                    return _unavailable("MARKET.APPLICATION_UNAVAILABLE")
                return ApiResponse(
                    status=200,
                    payload={
                        "items": [
                            _application_payload(item)
                            for item in self.applications.store.list_market_applications(
                                owner=owner
                            )
                        ]
                    },
                )
            if is_application_detail:
                if self.applications is None:
                    return _unavailable("MARKET.APPLICATION_UNAVAILABLE")
                application = self.applications.store.get_market_application(
                    parts[2], owner=owner
                )
                detail = self.applications.store.market_application_detail(
                    application.session_id, owner=owner
                )
                return ApiResponse(
                    status=200,
                    payload={**_application_payload(application), "detail": detail},
                )
            if self.publications is None:
                return _unavailable("TEMPLATE.PUBLICATION_UNAVAILABLE")
            session = self.publications.store.get_template_publication(
                parts[1], owner=owner
            )
            return ApiResponse(status=200, payload=_publication_payload(session))
        except KeyError:
            return _error(404, "MARKET.SESSION_NOT_FOUND", "market session was not found")

    def handle_post(
        self,
        parts: list[str],
        *,
        body: bytes,
        identity: UserIdentity | None,
    ) -> ApiResponse | None:
        recognized = (
            parts == ["market", "applications"]
            or (
                len(parts) == 4
                and parts[:2] == ["market", "applications"]
                and parts[3] == "confirmation"
            )
            or (
                len(parts) == 3
                and parts[0] == "runs"
                and parts[2] == "template-publication-sessions"
            )
            or (
                len(parts) == 3
                and parts[0] == "template-publication-sessions"
                and parts[2] in {"responses", "confirmation"}
            )
        )
        if not recognized:
            return None
        if identity is None:
            return _error(401, "AUTH.MISSING", "authenticated identity is required")
        payload, error = _body(body)
        if error is not None:
            return error
        owner = identity.username
        try:
            if parts == ["market", "applications"]:
                if self.applications is None:
                    return _unavailable("MARKET.APPLICATION_UNAVAILABLE")
                _closed(
                    payload,
                    {"source_kind", "source_item_id", "user_intent", "request_key"},
                )
                source_kind = MarketApplicationSourceKind(
                    _required_string(payload, "source_kind")
                )
                user_intent = _required_string(payload, "user_intent")
                request_key = _required_string(payload, "request_key")
                if source_kind is MarketApplicationSourceKind.CURATED_TEMPLATE:
                    branch: ReferenceAdaptationSession | TemplateApplicationSession = (
                        self.applications.start_template_application(
                            release_id=_required_string(payload, "source_item_id"),
                            owner=owner,
                            user_intent=user_intent,
                            request_key=request_key,
                        )
                    )
                else:
                    branch = self.applications.start_reference_adaptation(
                        publication_id=_required_string(payload, "source_item_id"),
                        owner=owner,
                        user_intent=user_intent,
                        request_key=request_key,
                    )
                return ApiResponse(status=201, payload=_branch_payload(branch))

            if len(parts) == 4 and parts[:2] == ["market", "applications"]:
                if self.applications is None:
                    return _unavailable("MARKET.APPLICATION_UNAVAILABLE")
                _closed(
                    payload,
                    {"expected_version", "confirmation_digest", "request_key"},
                )
                application = self.applications.store.get_market_application(
                    parts[2], owner=owner
                )
                expected_version = _required_integer(payload, "expected_version")
                confirmation_digest = _required_string(payload, "confirmation_digest")
                request_key = _required_string(payload, "request_key")
                if (
                    application.source_kind
                    is MarketApplicationSourceKind.CURATED_TEMPLATE
                ):
                    branch = self.applications.finalize_template_application(
                        session_id=application.session_id,
                        owner=owner,
                        expected_version=expected_version,
                        confirmation_digest=confirmation_digest,
                        request_key=request_key,
                    )
                else:
                    branch = self.applications.finalize_reference_adaptation(
                        session_id=application.session_id,
                        owner=owner,
                        expected_version=expected_version,
                        confirmation_digest=confirmation_digest,
                        request_key=request_key,
                    )
                return ApiResponse(status=200, payload=_branch_payload(branch))

            if len(parts) == 3 and parts[0] == "runs":
                if self.publications is None:
                    return _unavailable("TEMPLATE.PUBLICATION_UNAVAILABLE")
                _closed(
                    payload,
                    {
                        "request_key",
                        "title",
                        "description",
                        "visibility",
                        "compatibility",
                        "publication",
                    },
                    {
                        "scope_key",
                        "source_evidence_ref",
                        "source_evidence_digest",
                        "environment",
                        "base_release_id",
                    },
                )
                compatibility = _required_mapping(payload, "compatibility")
                publication = _required_mapping(payload, "publication")
                publication_session = self.publications.start_template_publication(
                    owner=owner,
                    source_run_id=parts[1],
                    request_key=_required_string(payload, "request_key"),
                    title=_required_string(payload, "title"),
                    description=_required_string(payload, "description"),
                    visibility=TemplateVisibility(
                        _required_string(payload, "visibility")
                    ),
                    scope_key=_optional_string(payload.get("scope_key")),
                    compatibility=dict(compatibility),
                    publication_metadata=dict(publication),
                    source_evidence_ref=_optional_string(
                        payload.get("source_evidence_ref")
                    ),
                    source_evidence_digest=_optional_string(
                        payload.get("source_evidence_digest")
                    ),
                    environment=_optional_string(payload.get("environment")) or "docker",
                    base_release_id=_optional_string(payload.get("base_release_id")),
                )
                return ApiResponse(
                    status=201,
                    payload=_publication_payload(publication_session),
                )

            if self.publications is None:
                return _unavailable("TEMPLATE.PUBLICATION_UNAVAILABLE")
            session_id = parts[1]
            if parts[2] == "responses":
                _closed(
                    payload,
                    {
                        "expected_version",
                        "evidence_ref",
                        "evidence_digest",
                        "environment",
                        "release_version",
                    },
                )
                publication_session = self.publications.record_template_reproduction(
                    session_id=session_id,
                    owner=owner,
                    expected_version=_required_integer(payload, "expected_version"),
                    evidence_ref=_required_string(payload, "evidence_ref"),
                    evidence_digest=_required_string(payload, "evidence_digest"),
                    environment=_required_string(payload, "environment"),
                    release_version=_required_string(payload, "release_version"),
                )
            else:
                _closed(payload, {"expected_version", "confirmation_digest"})
                publication_session = self.publications.submit_template_publication_review(
                    session_id=session_id,
                    owner=owner,
                    expected_version=_required_integer(payload, "expected_version"),
                    confirmation_digest=_required_string(
                        payload, "confirmation_digest"
                    ),
                )
            return ApiResponse(
                status=200,
                payload=_publication_payload(publication_session),
            )
        except KeyError:
            return _error(404, "MARKET.SESSION_NOT_FOUND", "market source or session not found")
        except PermissionError as exc:
            return _error(403, "MARKET.SOURCE_NOT_VISIBLE", str(exc))
        except MarketApplicationError as exc:
            return _error(_domain_status(exc.code), exc.code, str(exc))
        except TemplatePublicationError as exc:
            return _error(_domain_status(exc.code), exc.code, str(exc))
        except (TypeError, ValueError) as exc:
            return _error(400, "MARKET.INVALID_REQUEST", str(exc))


def _application_payload(application: Any) -> dict[str, Any]:
    return {
        "session_id": application.session_id,
        "owner": application.owner,
        "request_key": application.request_key,
        "source_kind": application.source_kind.value,
        "source_item_id": application.source_item_id,
        "source_digest": application.source_digest,
        "assurance": application.assurance.value,
        "user_intent": application.user_intent,
        "state": application.state,
        "version": application.version,
        "project_id": application.project_id,
        "workspace_id": application.workspace_id,
        "change_set_id": application.change_set_id,
        "target_contract_id": application.target_contract_id,
        "adoption_id": application.adoption_id,
        "created_at": application.created_at,
        "updated_at": application.updated_at,
    }


def _branch_payload(
    session: ReferenceAdaptationSession | TemplateApplicationSession,
) -> dict[str, Any]:
    payload = _application_payload(session.application)
    payload.update(
        {
            "target_contract_payload": session.target_contract_payload,
            "plan_digest": session.plan_digest,
            "confirmation_digest": session.confirmation_digest,
            "change_set_digest": session.change_set_digest,
        }
    )
    if isinstance(session, ReferenceAdaptationSession):
        payload.update(
            {
                "source_run_id": session.source_run_id,
                "source_contract_id": session.source_contract_id,
                "share_manifest_digest": session.share_manifest_digest,
            }
        )
    else:
        payload.update(
            {
                "release_id": session.release_id,
                "template_id": session.template_id,
                "release_version": session.release_version,
                "bundle_digest": session.bundle_digest,
            }
        )
    return payload


def _publication_payload(session: TemplatePublicationSession) -> dict[str, Any]:
    return {
        "session_id": session.session_id,
        "owner": session.owner,
        "request_key": session.request_key,
        "source_run_id": session.source_run_id,
        "source_contract_id": session.source_contract_id,
        "source_digest": session.source_digest,
        "bundle_digest": session.bundle_digest,
        "draft_id": session.draft_id,
        "state": session.state,
        "version": session.version,
        "reproduction_evidence_ref": session.reproduction_evidence_ref,
        "reproduction_evidence_digest": session.reproduction_evidence_digest,
        "reproduction_environment": session.reproduction_environment,
        "confirmation_digest": session.confirmation_digest,
        "review_id": session.review_id,
        "release_id": session.release_id,
        "release_version": session.release_version,
        "verification_id": session.verification_id,
        "created_at": session.created_at,
        "updated_at": session.updated_at,
    }


def _body(body: bytes) -> tuple[dict[str, Any], ApiResponse | None]:
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}, _error(400, "MARKET.INVALID_REQUEST", "request body must be JSON")
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        return {}, _error(400, "MARKET.INVALID_REQUEST", "request body must be an object")
    return value, None


def _closed(
    payload: Mapping[str, Any],
    required: set[str],
    optional: set[str] | None = None,
) -> None:
    optional = optional or set()
    if set(payload) != required | (set(payload) & optional) or not required <= set(payload):
        raise ValueError("request body fields are invalid")


def _required_string(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value or len(value) > 64_000 or "\0" in value:
        raise ValueError(f"{key} must be a non-empty string")
    return value


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or "\0" in value:
        raise ValueError("optional string is invalid")
    return value


def _required_integer(payload: Mapping[str, Any], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{key} must be a positive integer")
    return value


def _required_mapping(payload: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = payload.get(key)
    if not isinstance(value, Mapping) or not all(isinstance(name, str) for name in value):
        raise ValueError(f"{key} must be an object")
    return value


def _domain_status(code: str) -> int:
    if code.endswith(("CONFLICT", "STALE", "WITHDRAWN", "MISMATCH")):
        return 409
    if "NOT_VISIBLE" in code:
        return 403
    return 400


def _unavailable(code: str) -> ApiResponse:
    return _error(503, code, "market session service is unavailable")


def _error(status: int, code: str, message: str) -> ApiResponse:
    return ApiResponse(status=status, payload={"error": {"code": code, "message": message}})
