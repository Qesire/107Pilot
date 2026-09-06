from __future__ import annotations

from typing import Any

import pytest

from pilot107.api.workarea_binding_removal_routes import WorkAreaBindingRemovalRoutes
from pilot107.core.identity import UserIdentity
from pilot107.core.workarea_binding_removal import (
    PostgresWorkAreaBindingRemovalService,
    WorkAreaBindingRemovalConflict,
)


class _FakeRemover:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[dict[str, str]] = []

    def remove(self, **kwargs: str) -> None:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error


def test_delete_route_removes_explicit_binding() -> None:
    remover = _FakeRemover()
    routes = WorkAreaBindingRemovalRoutes(remover)

    response = routes.handle_delete(
        ["workareas", "wa-1", "bindings", "asset", "/project/code"],
        identity=UserIdentity(username="alice"),
    )

    assert response is not None
    assert response.status == 204
    assert remover.calls == [
        {
            "workarea_id": "wa-1",
            "owner": "alice",
            "binding_kind": "asset",
            "target_ref": "/project/code",
        }
    ]


def test_delete_route_rejects_immutable_provenance() -> None:
    routes = WorkAreaBindingRemovalRoutes(
        _FakeRemover(WorkAreaBindingRemovalConflict("inherited binding"))
    )

    response = routes.handle_delete(
        ["workareas", "wa-1", "bindings", "run", "run-1"],
        identity=UserIdentity(username="alice"),
    )

    assert response is not None
    assert response.status == 409
    assert response.payload["error"]["code"] == "WORKAREA_BINDING.IMMUTABLE"


def test_delete_route_hides_missing_binding_as_not_found() -> None:
    routes = WorkAreaBindingRemovalRoutes(_FakeRemover(KeyError("run-404")))

    response = routes.handle_delete(
        ["workareas", "wa-1", "bindings", "run", "run-404"],
        identity=UserIdentity(username="alice"),
    )

    assert response is not None
    assert response.status == 404
    assert response.payload["error"]["code"] == "WORKAREA_BINDING.NOT_FOUND"


class _Result:
    def __init__(self, row: dict[str, Any] | None = None, *, rowcount: int = 0) -> None:
        self._row = row
        self.rowcount = rowcount

    def fetchone(self) -> dict[str, Any] | None:
        return self._row


class _Connection:
    def __init__(
        self,
        *,
        source: str | None = "user",
        candidate_dependency: bool = False,
        launch_dependency: bool = False,
        edge_exists: bool = True,
    ) -> None:
        self.source = source
        self.candidate_dependency = candidate_dependency
        self.launch_dependency = launch_dependency
        self.edge_exists = edge_exists
        self.executed: list[str] = []

    def __enter__(self) -> _Connection:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def transaction(self) -> _Connection:
        return self

    def execute(self, sql: str, _params: tuple[object, ...]) -> _Result:
        normalized = " ".join(sql.split())
        self.executed.append(normalized)
        if "SELECT 1 FROM workareas" in normalized:
            return _Result({"exists": 1})
        if "SELECT source FROM workarea_binding_sources" in normalized:
            return _Result(None if self.source is None else {"source": self.source})
        if "SELECT candidate_id FROM launch_candidates" in normalized:
            return _Result({"candidate_id": "cand-1"} if self.candidate_dependency else None)
        if "SELECT lr.launch_id FROM launch_runs" in normalized:
            return _Result({"launch_id": "launch-1"} if self.launch_dependency else None)
        if normalized.startswith("DELETE FROM workarea_"):
            return _Result(rowcount=1 if self.edge_exists else 0)
        if normalized.startswith("UPDATE workareas"):
            return _Result(rowcount=1)
        return _Result()


class _BindingSources:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection

    def connect(self) -> _Connection:
        return self.connection


def _service(connection: _Connection) -> PostgresWorkAreaBindingRemovalService:
    return PostgresWorkAreaBindingRemovalService(_BindingSources(connection))  # type: ignore[arg-type]


def test_user_asset_binding_is_removed_atomically() -> None:
    connection = _Connection(source="user")

    _service(connection).remove(
        workarea_id="wa-1",
        owner="alice",
        binding_kind="asset",
        target_ref="/project/code",
    )

    assert any(statement.startswith("DELETE FROM workarea_assets") for statement in connection.executed)
    assert any(
        statement.startswith("DELETE FROM workarea_binding_sources")
        for statement in connection.executed
    )
    assert any(statement.startswith("UPDATE workareas") for statement in connection.executed)


def test_inherited_binding_cannot_be_removed() -> None:
    connection = _Connection(source="inherited")

    with pytest.raises(WorkAreaBindingRemovalConflict, match="inherited"):
        _service(connection).remove(
            workarea_id="wa-1",
            owner="alice",
            binding_kind="run",
            target_ref="run-1",
        )

    assert not any(statement.startswith("DELETE FROM workarea_runs") for statement in connection.executed)


def test_launch_run_cannot_be_removed_after_manual_source_promotion() -> None:
    connection = _Connection(source="user", launch_dependency=True)

    with pytest.raises(WorkAreaBindingRemovalConflict, match="Launch provenance"):
        _service(connection).remove(
            workarea_id="wa-1",
            owner="alice",
            binding_kind="run",
            target_ref="run-launch",
        )

    assert not any(statement.startswith("DELETE FROM workarea_runs") for statement in connection.executed)


def test_candidate_contract_cannot_be_removed_after_manual_source_promotion() -> None:
    connection = _Connection(source="user", candidate_dependency=True)

    with pytest.raises(WorkAreaBindingRemovalConflict, match="LaunchCandidate provenance"):
        _service(connection).remove(
            workarea_id="wa-1",
            owner="alice",
            binding_kind="contract",
            target_ref="contract-1",
        )

    assert not any(
        statement.startswith("DELETE FROM workarea_contracts") for statement in connection.executed
    )
