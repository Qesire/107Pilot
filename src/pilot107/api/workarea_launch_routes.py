"""HTTP route adapter for the competition WorkArea/Launch vertical slice."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from pilot107.api.http_types import ApiResponse
from pilot107.core.contracts import ContractError
from pilot107.core.identity import UserIdentity
from pilot107.core.launch import (
    LaunchConflict,
    PostgresLaunchStore,
    candidate_payload,
    launch_payload,
    preflight_payload,
)
from pilot107.core.workarea import PostgresWorkAreaStore, WorkAreaConflict, WorkAreaGraph
from pilot107.core.workarea_binding_source import PostgresWorkAreaBindingSourceStore
from pilot107.services.launch_service import LaunchCommitResult, LaunchService


class WorkAreaLaunchRoutes:
    def __init__(
        self,
        *,
        workareas: PostgresWorkAreaStore,
        launches: PostgresLaunchStore,
        launch_service: LaunchService,
        binding_sources: PostgresWorkAreaBindingSourceStore,
    ) -> None:
        self.workareas = workareas
        self.launches = launches
        self.launch_service = launch_service
        self.binding_sources = binding_sources

    def handle_get(
        self,
        parts: list[str],
        *,
        params: Mapping[str, list[str]],
        identity: UserIdentity | None,
    ) -> ApiResponse | None:
        if not parts or parts[0] not in {"workareas", "launch-candidates", "launches"}:
            return None
        owner, error = _owner(identity)
        if error is not None:
            return error
        assert owner is not None
        try:
            if parts == ["workareas"]:
                limit = _limit(params)
                workarea_records = self.workareas.list(owner=owner, limit=limit)
                return ApiResponse(
                    status=200,
                    payload={
                        "items": [_workarea_summary(item) for item in workarea_records]
                    },
                )
            if len(parts) == 2 and parts[0] == "workareas":
                graph = self.workareas.graph(parts[1], owner=owner)
                return ApiResponse(status=200, payload=self._graph_payload(graph))
            if len(parts) == 3 and parts[0] == "workareas" and parts[2] == "launches":
                self.workareas.get(parts[1], owner=owner)
                launch_records = self.launches.list_for_workarea(
                    workarea_id=parts[1], owner=owner, limit=_limit(params)
                )
                return ApiResponse(
                    status=200,
                    payload={
                        "items": [launch_payload(item) for item in launch_records]
                    },
                )
            if len(parts) == 2 and parts[0] == "launch-candidates":
                candidate = self.launches.get_candidate(parts[1], owner=owner)
                latest = self.launches.latest_preflight(parts[1], owner=owner)
                return ApiResponse(status=200, payload=candidate_payload(candidate, latest))
            if len(parts) == 2 and parts[0] == "launches":
                return ApiResponse(
                    status=200,
                    payload=launch_payload(self.launches.get(parts[1], owner=owner)),
                )
        except KeyError as exc:
            return _not_found(parts[0], str(exc.args[0]))
        except (ValueError, WorkAreaConflict, LaunchConflict) as exc:
            return _error(
                409 if isinstance(exc, (WorkAreaConflict, LaunchConflict)) else 400,
                "WORKAREA_LAUNCH.INVALID",
                str(exc),
            )
        return None

    def handle_post(
        self,
        parts: list[str],
        *,
        body: bytes,
        identity: UserIdentity | None,
    ) -> ApiResponse | None:
        if not parts or parts[0] not in {"workareas", "launch-candidates"}:
            return None
        owner, error = _owner(identity)
        if error is not None:
            return error
        assert owner is not None
        payload, error = _json_body(body)
        if error is not None:
            return error
        try:
            if parts == ["workareas"]:
                _only(payload, {"request_key", "title", "description"})
                record = self.workareas.create(
                    owner=owner,
                    request_key=_required(payload, "request_key"),
                    title=_required(payload, "title"),
                    description=_optional(payload, "description") or "",
                )
                return ApiResponse(
                    status=201,
                    payload=self._graph_payload(
                        self.workareas.graph(record.workarea_id, owner=owner)
                    ),
                )
            if len(parts) == 3 and parts[0] == "workareas" and parts[2] == "bindings":
                _only(payload, {"kind", "target_ref", "role"})
                kind = _required(payload, "kind")
                target_ref = _required(payload, "target_ref")
                role = _optional(payload, "role")
                if kind == "asset":
                    self.workareas.link_asset(
                        parts[1],
                        owner=owner,
                        asset_ref=target_ref,
                        asset_kind=role or "file",
                    )
                elif kind == "contract":
                    self.workareas.link_contract(
                        parts[1], owner=owner, contract_id=target_ref
                    )
                elif kind == "run":
                    self.workareas.link_run(parts[1], owner=owner, run_id=target_ref)
                else:
                    raise ValueError("kind must be asset, contract, or run")
                self.binding_sources.mark(
                    workarea_id=parts[1],
                    binding_kind=kind,
                    target_ref=target_ref,
                    source="user",
                )
                return ApiResponse(
                    status=200,
                    payload=self._graph_payload(
                        self.workareas.graph(parts[1], owner=owner)
                    ),
                )
            if (
                len(parts) == 3
                and parts[0] == "workareas"
                and parts[2] == "launch-candidates"
            ):
                _only(payload, {"request_key", "contract_id", "title", "note"})
                candidate = self.launch_service.create_candidate(
                    workarea_id=parts[1],
                    owner=owner,
                    contract_id=_required(payload, "contract_id"),
                    request_key=_required(payload, "request_key"),
                    title=_optional(payload, "title") or "",
                    note=_optional(payload, "note") or "",
                )
                return ApiResponse(status=201, payload=candidate_payload(candidate))
            if (
                len(parts) == 3
                and parts[0] == "launch-candidates"
                and parts[2] == "preflight"
            ):
                _only(payload, set())
                assessment = self.launch_service.assess(parts[1], owner=owner)
                return ApiResponse(status=200, payload=preflight_payload(assessment))
            if (
                len(parts) == 3
                and parts[0] == "launch-candidates"
                and parts[2] == "commit"
            ):
                _only(payload, {"preflight_digest", "request_key"})
                result = self.launch_service.commit(
                    parts[1],
                    owner=owner,
                    expected_preflight_digest=_required(payload, "preflight_digest"),
                    request_key=_required(payload, "request_key"),
                )
                return ApiResponse(status=201, payload=_commit_payload(result))
        except PermissionError as exc:
            return _error(403, "AUTH.FORBIDDEN", str(exc))
        except KeyError as exc:
            return _not_found(parts[0], str(exc.args[0]))
        except ContractError as exc:
            return _error(
                422,
                exc.code,
                str(exc),
                findings=[_finding(item) for item in exc.findings],
            )
        except LaunchConflict as exc:
            return _error(409, "LAUNCH.CONFLICT", str(exc))
        except WorkAreaConflict as exc:
            return _error(409, "WORKAREA.CONFLICT", str(exc))
        except (TypeError, ValueError) as exc:
            return _error(400, "WORKAREA_LAUNCH.INVALID", str(exc))
        return None

    def handle_patch(
        self,
        parts: list[str],
        *,
        body: bytes,
        identity: UserIdentity | None,
    ) -> ApiResponse | None:
        if len(parts) != 2 or parts[0] != "workareas":
            return None
        owner, error = _owner(identity)
        if error is not None:
            return error
        assert owner is not None
        payload, error = _json_body(body)
        if error is not None:
            return error
        try:
            _only(payload, {"title", "description"})
            current = self.workareas.get(parts[1], owner=owner)
            title = _optional(payload, "title") or current.title
            description = (
                current.description
                if "description" not in payload
                else (_optional(payload, "description", allow_empty=True) or "")
            )
            with self.workareas.connect() as connection, connection.transaction():
                result = connection.execute(
                    """
                    UPDATE workareas SET title = %s, description = %s, updated_at = NOW()
                    WHERE workarea_id = %s AND owner = %s
                    """,
                    (title, description, parts[1], owner),
                )
                if result.rowcount != 1:
                    raise WorkAreaConflict("WorkArea changed during update")
            return ApiResponse(
                status=200,
                payload=self._graph_payload(
                    self.workareas.graph(parts[1], owner=owner)
                ),
            )
        except KeyError:
            return _not_found("workarea", parts[1])
        except (TypeError, ValueError) as exc:
            return _error(400, "WORKAREA.INVALID", str(exc))
        except WorkAreaConflict as exc:
            return _error(409, "WORKAREA.CONFLICT", str(exc))

    def _graph_payload(self, graph: WorkAreaGraph) -> dict[str, Any]:
        sources = self.binding_sources.sources_for_workarea(graph.workarea.workarea_id)
        return _workarea_graph(graph, sources=sources)


def _commit_payload(result: LaunchCommitResult) -> dict[str, Any]:
    run = result.run
    return {
        "launch": launch_payload(result.launch),
        "run": {
            "run_id": run.run_id,
            "contract_id": run.contract_id,
            "state": run.state.value,
            "job_id": run.job_id,
            "workdir": run.workdir,
            "created_at": run.created_at,
            "updated_at": run.updated_at,
        },
        "submit_error": result.submit_error,
    }


def _workarea_summary(record: Any) -> dict[str, Any]:
    return {
        "workarea_id": record.workarea_id,
        "owner": record.owner,
        "title": record.title,
        "description": record.description,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
    }


def _workarea_graph(
    graph: WorkAreaGraph,
    *,
    sources: Mapping[tuple[str, str], str],
) -> dict[str, Any]:
    return {
        **_workarea_summary(graph.workarea),
        "bindings": {
            "contracts": [
                {
                    "kind": "contract",
                    "target_ref": item,
                    "source": sources.get(("contract", item), "user"),
                }
                for item in graph.contract_ids
            ],
            "runs": [
                {
                    "kind": "run",
                    "target_ref": item,
                    "source": sources.get(("run", item), "user"),
                }
                for item in graph.run_ids
            ],
            "assets": [
                {
                    "kind": "asset",
                    "target_ref": item.asset_ref,
                    "role": item.asset_kind,
                    "source": sources.get(("asset", item.asset_ref), "user"),
                    "linked_at": item.linked_at,
                }
                for item in graph.assets
            ],
        },
    }


def _owner(identity: UserIdentity | None) -> tuple[str | None, ApiResponse | None]:
    if identity is None:
        return None, _error(401, "AUTH.MISSING", "identity required")
    return identity.username, None


def _limit(params: Mapping[str, list[str]]) -> int:
    unknown = set(params) - {"limit", "owner"}
    if unknown:
        raise ValueError(f"unsupported query parameter: {', '.join(sorted(unknown))}")
    raw = params.get("limit", ["100"])
    if len(raw) != 1:
        raise ValueError("limit must be provided once")
    value = int(raw[0])
    if value <= 0 or value > 500:
        raise ValueError("limit must be between 1 and 500")
    return value


def _json_body(body: bytes) -> tuple[dict[str, Any], ApiResponse | None]:
    try:
        value = json.loads(body.decode("utf-8") if body else "{}")
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return {}, _error(400, "INVALID_JSON", str(exc))
    if not isinstance(value, dict):
        return {}, _error(400, "INVALID_JSON", "body must be an object")
    return value, None


def _only(payload: dict[str, Any], allowed: set[str]) -> None:
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise ValueError(f"unsupported request field: {', '.join(unknown)}")


def _required(payload: dict[str, Any], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} is required")
    return value.strip()


def _optional(
    payload: dict[str, Any], field: str, *, allow_empty: bool = False
) -> str | None:
    value = payload.get(field)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    value = value.strip()
    if not value and not allow_empty:
        return None
    return value


def _finding(item: Any) -> dict[str, Any]:
    return {
        "severity": item.severity.value,
        "code": item.code,
        "message": item.message,
        "source_authority": item.source_authority,
    }


def _not_found(kind: str, object_id: str) -> ApiResponse:
    return _error(404, "WORKAREA_LAUNCH.NOT_FOUND", f"{kind} not found: {object_id}")


def _error(
    status: int,
    code: str,
    message: str,
    *,
    findings: list[dict[str, Any]] | None = None,
) -> ApiResponse:
    payload: dict[str, Any] = {"error": {"code": code, "message": message}}
    if findings is not None:
        payload["findings"] = findings
    return ApiResponse(status=status, payload=payload)


__all__ = ["WorkAreaLaunchRoutes"]
