"""DELETE route for explicit WorkArea bindings."""

from __future__ import annotations

from typing import Protocol

from pilot107.api.http_types import ApiResponse
from pilot107.core.identity import UserIdentity
from pilot107.core.workarea_binding_removal import WorkAreaBindingRemovalConflict


class WorkAreaBindingRemover(Protocol):
    def remove(
        self,
        *,
        workarea_id: str,
        owner: str,
        binding_kind: str,
        target_ref: str,
    ) -> None: ...


class WorkAreaBindingRemovalRoutes:
    """Narrow route adapter for user-controlled WorkArea membership removal."""

    def __init__(self, remover: WorkAreaBindingRemover) -> None:
        self.remover = remover

    def handle_delete(
        self,
        parts: list[str],
        *,
        identity: UserIdentity | None,
    ) -> ApiResponse | None:
        if not (
            len(parts) == 5
            and parts[0] == "workareas"
            and parts[2] == "bindings"
        ):
            return None
        if identity is None:
            return _error(401, "AUTH.MISSING", "identity required")
        workarea_id = parts[1]
        binding_kind = parts[3]
        target_ref = parts[4]
        try:
            self.remover.remove(
                workarea_id=workarea_id,
                owner=identity.username,
                binding_kind=binding_kind,
                target_ref=target_ref,
            )
        except KeyError:
            return _error(
                404,
                "WORKAREA_BINDING.NOT_FOUND",
                "WorkArea binding was not found",
            )
        except WorkAreaBindingRemovalConflict as exc:
            return _error(409, "WORKAREA_BINDING.IMMUTABLE", str(exc))
        except (TypeError, ValueError) as exc:
            return _error(400, "WORKAREA_BINDING.INVALID", str(exc))
        return ApiResponse(status=204, payload={})


def _error(status: int, code: str, message: str) -> ApiResponse:
    return ApiResponse(
        status=status,
        payload={"error": {"code": code, "message": message}},
    )


__all__ = ["WorkAreaBindingRemovalRoutes", "WorkAreaBindingRemover"]
