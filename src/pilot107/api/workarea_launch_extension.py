"""Narrow route extension for WorkArea/Launch delivery APIs.

The central stdlib HTTP adapter is intentionally stable and very large. This
module installs an isolated extension at its two internal dispatch seams so the
competition vertical can evolve without duplicating or rewriting the existing
Run/Files/Market router. Unrelated paths fall through to the original handlers.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from pilot107.api.workarea_launch_routes import WorkAreaLaunchRoutes
from pilot107.core.launch import PostgresLaunchStore
from pilot107.core.workarea import PostgresWorkAreaStore
from pilot107.core.workarea_binding_source import PostgresWorkAreaBindingSourceStore
from pilot107.services.launch_service import LaunchService

_INSTALLED = False
_ROUTE_ROOTS = frozenset({"workareas", "launch-candidates", "launches"})


def install_workarea_launch_extension() -> None:
    """Install once on :class:`Pilot107HttpApi` internal dispatch methods."""

    global _INSTALLED
    if _INSTALLED:
        return
    from pilot107.api.http_app import Pilot107HttpApi

    original_get = Pilot107HttpApi._handle_get
    original_post = Pilot107HttpApi._handle_post

    def extended_get(
        self: Any,
        path: str,
        headers: Any = None,
    ) -> Any:
        parts = _parts(path)
        if parts and parts[0] in _ROUTE_ROOTS:
            identity, auth_error = self._resolve_identity(headers)
            if auth_error is not None:
                return auth_error
            routes = _routes(self)
            if routes is not None:
                response = routes.handle_get(
                    parts,
                    params=parse_qs(urlparse(path).query, keep_blank_values=True),
                    identity=identity,
                )
                if response is not None:
                    return response
        return original_get(self, path, headers=headers)

    def extended_post(
        self: Any,
        path: str,
        body: bytes = b"",
        headers: Any = None,
    ) -> Any:
        parts = _parts(path)
        if parts and parts[0] in _ROUTE_ROOTS:
            identity, auth_error = self._resolve_identity(headers)
            if auth_error is not None:
                return auth_error
            routes = _routes(self)
            if routes is not None:
                response = routes.handle_post(parts, body=body, identity=identity)
                if response is not None:
                    return response
        return original_post(self, path, body=body, headers=headers)

    # The extension mutates the class object at composition time. Deliberately
    # type only that object as Any so the rest of Pilot107HttpApi remains under
    # strict mypy checking; no lint/type rule is suppressed globally or locally.
    api_type: Any = Pilot107HttpApi
    api_type._handle_get = extended_get
    api_type._handle_post = extended_post
    _INSTALLED = True


def _routes(api: Any) -> WorkAreaLaunchRoutes | None:
    existing = getattr(api, "_workarea_launch_extension_routes", None)
    if isinstance(existing, WorkAreaLaunchRoutes):
        return existing
    if existing is False:
        return None
    dsn = getattr(api.store, "dsn", None)
    if (
        not isinstance(dsn, str)
        or not dsn
        or api.contract_service is None
        or api.run_service is None
    ):
        api._workarea_launch_extension_routes = False
        return None
    # WorkArea schema is initialized first because both provenance and Launch
    # migrations reference the WorkArea tables created by 006c.002-004.
    workareas = PostgresWorkAreaStore(dsn)
    binding_sources = PostgresWorkAreaBindingSourceStore(dsn)
    launches = PostgresLaunchStore(dsn)
    service = LaunchService(
        workareas=workareas,
        launches=launches,
        contracts=api.contract_service,
        run_service=api.run_service,
        run_store=api.store,
        binding_sources=binding_sources,
    )
    routes = WorkAreaLaunchRoutes(
        workareas=workareas,
        launches=launches,
        launch_service=service,
        binding_sources=binding_sources,
    )
    api._workarea_launch_extension_routes = routes
    return routes


def _parts(path: str) -> list[str]:
    route = urlparse(path).path.rstrip("/") or "/"
    parts = [unquote(part) for part in route.split("/") if part]
    if len(parts) >= 2 and parts[:2] == ["api", "v1"]:
        return parts[2:]
    return parts


__all__ = ["install_workarea_launch_extension"]
