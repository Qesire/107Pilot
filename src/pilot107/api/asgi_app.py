"""FastAPI transport adapter for the existing HTTP application contract."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from pilot107.api.http_app import ApiResponse, Pilot107HttpApi
from pilot107.api.metrics import ApiMetricsMiddleware
from pilot107.api.service import build_api_service, config_from_env

Forwarder = Callable[[Request], Awaitable[Response]]

_OWNER_PARAMETER = {
    "name": "owner",
    "in": "query",
    "required": False,
    "schema": {"type": "string", "maxLength": 64},
    "description": "Defaults to and must match the authenticated owner.",
}
_PLATFORM_LIST_PARAMETERS = [
    _OWNER_PARAMETER,
    {
        "name": "scope",
        "in": "query",
        "required": False,
        "schema": {"type": "string", "enum": ["login_node", "compute_job", "simulator"]},
    },
    {
        "name": "source_type",
        "in": "query",
        "required": False,
        "schema": {"type": "string", "enum": ["cli", "rest", "official_docs", "simulator"]},
    },
    {
        "name": "freshness",
        "in": "query",
        "required": False,
        "schema": {"type": "string", "enum": ["fresh", "stale", "unknown"]},
    },
    {
        "name": "as_of",
        "in": "query",
        "required": False,
        "schema": {"type": "string", "format": "date-time"},
    },
    {
        "name": "limit",
        "in": "query",
        "required": False,
        "schema": {"type": "integer", "minimum": 1, "maximum": 100, "default": 50},
    },
    {
        "name": "cursor",
        "in": "query",
        "required": False,
        "schema": {"type": "string"},
    },
]
_ENTITLEMENT_LIST_PARAMETERS = [
    _OWNER_PARAMETER,
    *_PLATFORM_LIST_PARAMETERS[3:],
]
_TEMPLATE_DRAFT_PATH_PARAMETER = {
    "name": "draft_id",
    "in": "path",
    "required": True,
    "schema": {"type": "string", "minLength": 1, "maxLength": 128},
}
_TEMPLATE_RELEASE_PARAMETERS = [
    {
        "name": "template_id",
        "in": "path",
        "required": True,
        "schema": {"type": "string", "minLength": 1, "maxLength": 128},
    },
    {
        "name": "release_version",
        "in": "path",
        "required": True,
        "schema": {"type": "string", "pattern": "^[0-9]+\\.[0-9]+\\.[0-9]+$"},
    },
]
_TEMPLATE_MARKET_PARAMETERS = [
    {
        "name": "q",
        "in": "query",
        "required": False,
        "schema": {"type": "string", "maxLength": 200},
    },
    {
        "name": "visibility",
        "in": "query",
        "required": False,
        "schema": {
            "type": "string",
            "enum": ["private", "course", "campus", "public"],
        },
    },
    {
        "name": "partition",
        "in": "query",
        "required": False,
        "schema": {"type": "string", "maxLength": 64},
    },
    {
        "name": "gpu",
        "in": "query",
        "required": False,
        "schema": {"type": "boolean"},
    },
    {
        "name": "verification_environment",
        "in": "query",
        "required": False,
        "schema": {
            "type": "string",
            "enum": ["docker", "real107_cpu", "real107_gpu"],
        },
    },
    {
        "name": "verified",
        "in": "query",
        "required": False,
        "schema": {"type": "boolean"},
    },
    {
        "name": "limit",
        "in": "query",
        "required": False,
        "schema": {"type": "integer", "minimum": 1, "maximum": 100},
    },
    {
        "name": "cursor",
        "in": "query",
        "required": False,
        "schema": {"type": "string"},
    },
]
_REMEDIATION_SESSION_PATH_PARAMETER = {
    "name": "session_id",
    "in": "path",
    "required": True,
    "schema": {"type": "string", "minLength": 1, "maxLength": 64},
}
_RUN_PATH_PARAMETER = {
    "name": "run_id",
    "in": "path",
    "required": True,
    "schema": {"type": "string", "minLength": 1, "maxLength": 128},
}


def build_asgi_app(api: Pilot107HttpApi) -> FastAPI:
    app = FastAPI(
        title="107Pilot Control API",
        version="0.3.0",
        description="Owner-scoped control-plane API for 107Pilot.",
    )
    metrics = api.metrics
    app.state.pilot107_metrics = metrics
    app.add_middleware(ApiMetricsMiddleware, metrics=metrics)

    async def metrics_endpoint() -> Response:
        return Response(
            content=metrics.render(),
            media_type="text/plain; version=0.0.4",
            headers={"Cache-Control": "no-store"},
        )

    app.add_api_route(
        "/metrics",
        metrics_endpoint,
        methods=["GET"],
        include_in_schema=False,
    )
    forward_get = _get_forwarder(api)
    forward_post = _post_forwarder(api)
    forward_patch = _patch_forwarder(api)

    app.add_api_route(
        "/healthz",
        forward_get,
        methods=["GET"],
        operation_id="legacy_healthz",
        tags=["health"],
        deprecated=True,
    )
    app.add_api_route(
        "/api/v1/health/live",
        forward_get,
        methods=["GET"],
        operation_id="get_liveness",
        tags=["health"],
    )
    app.add_api_route(
        "/api/v1/health/ready",
        forward_get,
        methods=["GET"],
        operation_id="get_readiness",
        tags=["health"],
        responses={503: {"description": "A required dependency is unavailable."}},
    )
    app.add_api_route(
        "/api/v1/platform/capabilities",
        forward_get,
        methods=["GET"],
        operation_id="get_platform_capabilities",
        tags=["platform"],
        openapi_extra={"parameters": [_OWNER_PARAMETER]},
    )
    app.add_api_route(
        "/api/v1/platform/snapshots",
        forward_get,
        methods=["GET"],
        operation_id="list_platform_snapshots",
        tags=["platform"],
        openapi_extra={"parameters": _PLATFORM_LIST_PARAMETERS},
    )
    app.add_api_route(
        "/api/v1/platform/snapshots/latest",
        forward_get,
        methods=["GET"],
        operation_id="get_latest_platform_snapshot",
        tags=["platform"],
        openapi_extra={"parameters": [_OWNER_PARAMETER, _PLATFORM_LIST_PARAMETERS[1]]},
    )
    app.add_api_route(
        "/api/v1/platform/snapshots/{snapshot_id}",
        forward_get,
        methods=["GET"],
        operation_id="get_platform_snapshot",
        tags=["platform"],
        openapi_extra={
            "parameters": [
                {
                    "name": "snapshot_id",
                    "in": "path",
                    "required": True,
                    "schema": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 128,
                        "pattern": "^[A-Za-z0-9._:-]+$",
                    },
                },
                _OWNER_PARAMETER,
            ]
        },
    )
    app.add_api_route(
        "/api/v1/platform/entitlements",
        forward_get,
        methods=["GET"],
        operation_id="list_user_entitlements",
        tags=["platform"],
        openapi_extra={"parameters": _ENTITLEMENT_LIST_PARAMETERS},
    )
    app.add_api_route(
        "/api/v1/platform/entitlements/latest",
        forward_get,
        methods=["GET"],
        operation_id="get_latest_user_entitlement",
        tags=["platform"],
        openapi_extra={"parameters": [_OWNER_PARAMETER]},
    )
    app.add_api_route(
        "/api/v1/platform/entitlements/{snapshot_id}",
        forward_get,
        methods=["GET"],
        operation_id="get_user_entitlement",
        tags=["platform"],
        openapi_extra={
            "parameters": [
                {
                    "name": "snapshot_id",
                    "in": "path",
                    "required": True,
                    "schema": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 128,
                        "pattern": "^[A-Za-z0-9._:-]+$",
                    },
                },
                _OWNER_PARAMETER,
            ]
        },
    )
    app.add_api_route(
        "/api/v1/template-drafts",
        forward_get,
        methods=["GET"],
        operation_id="list_template_drafts",
        tags=["templates"],
        openapi_extra={
            "parameters": [
                _OWNER_PARAMETER,
                {
                    "name": "limit",
                    "in": "query",
                    "required": False,
                    "schema": {"type": "integer", "minimum": 1, "maximum": 100},
                },
                {
                    "name": "cursor",
                    "in": "query",
                    "required": False,
                    "schema": {"type": "string"},
                },
            ]
        },
    )
    app.add_api_route(
        "/api/v1/template-drafts",
        forward_post,
        methods=["POST"],
        operation_id="create_template_draft",
        tags=["templates"],
    )
    app.add_api_route(
        "/api/v1/template-drafts/{draft_id}",
        forward_get,
        methods=["GET"],
        operation_id="get_template_draft",
        tags=["templates"],
        openapi_extra={"parameters": [_TEMPLATE_DRAFT_PATH_PARAMETER, _OWNER_PARAMETER]},
    )
    app.add_api_route(
        "/api/v1/template-drafts/{draft_id}",
        forward_patch,
        methods=["PATCH"],
        operation_id="update_template_draft",
        tags=["templates"],
        openapi_extra={"parameters": [_TEMPLATE_DRAFT_PATH_PARAMETER]},
    )
    for action, operation_id in (
        ("validate", "validate_template_draft"),
        ("reviews", "submit_template_review"),
        ("publish", "publish_template_release"),
    ):
        app.add_api_route(
            f"/api/v1/template-drafts/{{draft_id}}/{action}",
            forward_post,
            methods=["POST"],
            operation_id=operation_id,
            tags=["templates"],
            openapi_extra={"parameters": [_TEMPLATE_DRAFT_PATH_PARAMETER]},
        )
    app.add_api_route(
        "/api/v1/template-reviews/{review_id}/decision",
        forward_post,
        methods=["POST"],
        operation_id="decide_template_review",
        tags=["templates"],
        openapi_extra={
            "parameters": [
                {
                    "name": "review_id",
                    "in": "path",
                    "required": True,
                    "schema": {"type": "string", "minLength": 1, "maxLength": 128},
                }
            ]
        },
    )
    app.add_api_route(
        "/api/v1/template-reviews",
        forward_get,
        methods=["GET"],
        operation_id="list_template_reviews",
        tags=["templates"],
        openapi_extra={
            "parameters": [
                {
                    "name": "limit",
                    "in": "query",
                    "required": False,
                    "schema": {"type": "integer", "minimum": 1, "maximum": 100},
                },
                {
                    "name": "cursor",
                    "in": "query",
                    "required": False,
                    "schema": {"type": "string"},
                },
            ]
        },
    )
    app.add_api_route(
        "/api/v1/templates",
        forward_get,
        methods=["GET"],
        operation_id="list_template_market",
        tags=["templates"],
        openapi_extra={"parameters": _TEMPLATE_MARKET_PARAMETERS},
    )
    app.add_api_route(
        "/api/v1/templates/{template_id}/diff",
        forward_get,
        methods=["GET"],
        operation_id="diff_template_releases",
        tags=["templates"],
        openapi_extra={
            "parameters": [
                _TEMPLATE_RELEASE_PARAMETERS[0],
                {
                    "name": "from",
                    "in": "query",
                    "required": True,
                    "schema": {"type": "string", "pattern": "^[0-9]+\\.[0-9]+\\.[0-9]+$"},
                },
                {
                    "name": "to",
                    "in": "query",
                    "required": True,
                    "schema": {"type": "string", "pattern": "^[0-9]+\\.[0-9]+\\.[0-9]+$"},
                },
            ]
        },
    )
    app.add_api_route(
        "/api/v1/templates/{template_id}/releases/{release_version}",
        forward_get,
        methods=["GET"],
        operation_id="get_template_release",
        tags=["templates"],
        openapi_extra={"parameters": _TEMPLATE_RELEASE_PARAMETERS},
    )
    app.add_api_route(
        "/api/v1/templates/{template_id}/releases/{release_version}/verifications",
        forward_get,
        methods=["GET"],
        operation_id="list_template_verifications",
        tags=["templates"],
        openapi_extra={
            "parameters": [
                *_TEMPLATE_RELEASE_PARAMETERS,
                {
                    "name": "limit",
                    "in": "query",
                    "required": False,
                    "schema": {"type": "integer", "minimum": 1, "maximum": 100},
                },
                {
                    "name": "cursor",
                    "in": "query",
                    "required": False,
                    "schema": {"type": "string", "maxLength": 2048},
                },
            ]
        },
    )
    for action, operation_id in (
        ("adopt", "adopt_template_release"),
        ("withdraw", "withdraw_template_release"),
        ("verify", "verify_template_release"),
    ):
        app.add_api_route(
            f"/api/v1/templates/{{template_id}}/releases/{{release_version}}/{action}",
            forward_post,
            methods=["POST"],
            operation_id=operation_id,
            tags=["templates"],
            openapi_extra={"parameters": _TEMPLATE_RELEASE_PARAMETERS},
        )

    app.add_api_route(
        "/api/v1/remediation-sessions",
        forward_get,
        methods=["GET"],
        operation_id="list_remediation_sessions",
        tags=["remediation"],
        openapi_extra={
            "parameters": [
                _OWNER_PARAMETER,
                {
                    "name": "state",
                    "in": "query",
                    "required": False,
                    "schema": {"type": "string"},
                },
                {
                    "name": "limit",
                    "in": "query",
                    "required": False,
                    "schema": {"type": "integer", "minimum": 1, "maximum": 100},
                },
                {
                    "name": "cursor",
                    "in": "query",
                    "required": False,
                    "schema": {"type": "string", "maxLength": 2048},
                },
            ]
        },
    )
    app.add_api_route(
        "/api/v1/remediation-sessions/{session_id}",
        forward_get,
        methods=["GET"],
        operation_id="get_remediation_session",
        tags=["remediation"],
        openapi_extra={"parameters": [_REMEDIATION_SESSION_PATH_PARAMETER]},
    )
    app.add_api_route(
        "/api/v1/remediation-sessions/{session_id}/events",
        forward_get,
        methods=["GET"],
        operation_id="list_remediation_session_events",
        tags=["remediation"],
        openapi_extra={
            "parameters": [
                _REMEDIATION_SESSION_PATH_PARAMETER,
                {
                    "name": "after_event_id",
                    "in": "query",
                    "required": False,
                    "schema": {"type": "integer", "minimum": 0},
                },
                {
                    "name": "limit",
                    "in": "query",
                    "required": False,
                    "schema": {"type": "integer", "minimum": 1, "maximum": 100},
                },
            ]
        },
    )
    app.add_api_route(
        "/api/v1/runs/{run_id}/remediation-sessions",
        forward_post,
        methods=["POST"],
        operation_id="create_remediation_session",
        tags=["remediation"],
        openapi_extra={"parameters": [_RUN_PATH_PARAMETER]},
    )
    for action, operation_id in (
        ("advance", "advance_remediation_session"),
        ("approve", "approve_remediation_action"),
        ("reject", "reject_remediation_action"),
        ("execute", "execute_remediation_action"),
        ("cancel", "cancel_remediation_session"),
        ("takeover", "takeover_remediation_session"),
    ):
        app.add_api_route(
            f"/api/v1/remediation-sessions/{{session_id}}/{action}",
            forward_post,
            methods=["POST"],
            operation_id=operation_id,
            tags=["remediation"],
            openapi_extra={"parameters": [_REMEDIATION_SESSION_PATH_PARAMETER]},
        )

    # Routes migrate to explicit OpenAPI operations incrementally while sharing
    # the same domain adapter and contract tests throughout the transition.
    app.add_api_route(
        "/{path:path}",
        forward_get,
        methods=["GET"],
        include_in_schema=False,
    )
    app.add_api_route(
        "/{path:path}",
        forward_post,
        methods=["POST"],
        include_in_schema=False,
    )
    app.add_api_route(
        "/{path:path}",
        forward_patch,
        methods=["PATCH"],
        include_in_schema=False,
    )
    return app


def create_app() -> FastAPI:
    """Uvicorn factory using the same environment configuration as the legacy server."""

    return build_asgi_app(build_api_service(config_from_env()))


def openapi_contract_snapshot(app: FastAPI) -> dict[str, Any]:
    """Return the stable public surface used by the OpenAPI snapshot test."""

    schema = app.openapi()
    paths: dict[str, Any] = {}
    for path, path_item in sorted(schema["paths"].items()):
        operations: dict[str, Any] = {}
        for method, operation in sorted(path_item.items()):
            if method not in {"get", "post", "patch", "put", "delete"}:
                continue
            operations[method] = {
                "operationId": operation.get("operationId"),
                "tags": operation.get("tags", []),
                "parameters": [
                    {
                        "name": parameter["name"],
                        "in": parameter["in"],
                        "required": parameter.get("required", False),
                    }
                    for parameter in operation.get("parameters", [])
                ],
            }
        paths[path] = operations
    return {
        "title": schema["info"]["title"],
        "version": schema["info"]["version"],
        "paths": paths,
    }


def _get_forwarder(api: Pilot107HttpApi) -> Forwarder:
    async def forward(request: Request) -> Response:
        return _to_fastapi_response(
            api.handle_get(_request_target(request), headers=dict(request.headers.items()))
        )

    return forward


def _post_forwarder(api: Pilot107HttpApi) -> Forwarder:
    async def forward(request: Request) -> Response:
        return _to_fastapi_response(
            api.handle_post(
                _request_target(request),
                body=await request.body(),
                headers=dict(request.headers.items()),
            )
        )

    return forward


def _patch_forwarder(api: Pilot107HttpApi) -> Forwarder:
    async def forward(request: Request) -> Response:
        return _to_fastapi_response(
            api.handle_patch(
                _request_target(request),
                body=await request.body(),
                headers=dict(request.headers.items()),
            )
        )

    return forward


def _request_target(request: Request) -> str:
    query = request.url.query
    return request.url.path if not query else f"{request.url.path}?{query}"


def _to_fastapi_response(response: ApiResponse) -> Response:
    headers = response.headers or {}
    if response.status == 304:
        return Response(status_code=304, headers=headers)
    return JSONResponse(
        content=response.payload,
        status_code=response.status,
        headers=headers,
    )
