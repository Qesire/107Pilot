"""Authenticated, persistence-only resource observation routes."""

from __future__ import annotations

import re
from collections.abc import Mapping

from pilot107.api.http_types import ApiResponse
from pilot107.core.identity import UserIdentity
from pilot107.observability.service import ObservabilityService

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class ResourceObservationRoutes:
    def __init__(self, service: ObservabilityService) -> None:
        self.service = service

    def handle_get(
        self,
        parts: list[str],
        *,
        params: Mapping[str, list[str]],
        identity: UserIdentity | None,
    ) -> ApiResponse | None:
        recognized = (
            (len(parts) >= 2 and parts[0] == "observability")
            or (
                len(parts) >= 3
                and parts[0] == "runs"
                and parts[2] in {"resources", "resource-evaluations"}
            )
        )
        if not recognized:
            return None
        if identity is None:
            return _error(401, "AUTH.MISSING", "authenticated identity is required")
        try:
            if len(parts) == 5 and parts[:2] == ["observability", "connections"]:
                if parts[4] != "latest" or params:
                    raise ValueError("query parameters are not supported")
                connection_id = parts[2]
                _safe_id(connection_id)
                if parts[3:5] == ["capabilities", "latest"]:
                    return ApiResponse(
                        status=200,
                        payload=self.service.latest_capability(connection_id),
                    )
                if parts[3:5] == ["platform", "latest"]:
                    return ApiResponse(
                        status=200,
                        payload=self.service.latest_platform(connection_id),
                    )
                if parts[3:5] == ["account", "latest"]:
                    return ApiResponse(
                        status=200,
                        payload=self.service.latest_account(
                            connection_id, owner=identity.username
                        ),
                    )
            if len(parts) == 3 and parts[0] == "runs" and parts[2] == "resources":
                if params:
                    raise ValueError("query parameters are not supported")
                return ApiResponse(
                    status=200,
                    payload=self.service.run_resources(
                        _safe_id(parts[1]), owner=identity.username
                    ),
                )
            if parts[:1] == ["runs"] and len(parts) == 4 and parts[2:] == [
                "resources",
                "series",
            ]:
                if set(params) - {"step", "limit"}:
                    raise ValueError("query parameters are invalid")
                step = _one(params, "step", default="raw")
                if step not in {"raw", "1m"}:
                    raise ValueError("step must be raw or 1m")
                limit = int(_one(params, "limit", default="500"))
                if not 1 <= limit <= 1000:
                    raise ValueError("limit must be between 1 and 1000")
                return ApiResponse(
                    status=200,
                    payload=self.service.run_series(
                        _safe_id(parts[1]),
                        owner=identity.username,
                        step=step,
                        limit=limit,
                    ),
                )
            if (
                len(parts) == 3
                and parts[0] == "runs"
                and parts[2] == "resource-evaluations"
            ):
                if params:
                    raise ValueError("query parameters are not supported")
                return ApiResponse(
                    status=200,
                    payload=self.service.run_evaluations(
                        _safe_id(parts[1]), owner=identity.username
                    ),
                )
        except KeyError:
            return _error(
                404, "OBSERVABILITY.NOT_FOUND", "resource observation was not found"
            )
        except (TypeError, ValueError):
            return _error(
                400, "OBSERVABILITY.INVALID_REQUEST", "resource request is invalid"
            )
        return None


def _one(
    params: Mapping[str, list[str]], key: str, *, default: str | None = None
) -> str:
    values = params.get(key)
    if values is None:
        if default is None:
            raise ValueError(f"{key} is required")
        return default
    if len(values) != 1 or not values[0]:
        raise ValueError(f"{key} is invalid")
    return values[0]


def _safe_id(value: str) -> str:
    if _ID.fullmatch(value) is None:
        raise ValueError("resource identifier is invalid")
    return value


def _error(status: int, code: str, message: str) -> ApiResponse:
    return ApiResponse(status=status, payload={"error": {"code": code, "message": message}})
