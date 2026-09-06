"""Safe removal of explicit WorkArea membership edges.

A WorkArea binding can be removed only when it is an explicit user binding and
is not required by durable Launch provenance.  The mutation is performed in one
PostgreSQL transaction so provenance cannot race the typed membership edge.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pilot107.core.workarea_binding_source import PostgresWorkAreaBindingSourceStore

_REMOVABLE_KINDS = frozenset({"asset", "contract", "run"})


class WorkAreaBindingRemovalConflict(RuntimeError):
    """Raised when deleting a WorkArea edge would destroy durable provenance."""


class PostgresWorkAreaBindingRemovalService:
    """PostgreSQL authority for user-requested WorkArea binding removal."""

    def __init__(self, binding_sources: PostgresWorkAreaBindingSourceStore) -> None:
        self.binding_sources = binding_sources

    def remove(
        self,
        *,
        workarea_id: str,
        owner: str,
        binding_kind: str,
        target_ref: str,
    ) -> None:
        _validate(workarea_id, owner, binding_kind, target_ref)
        now = datetime.now(UTC)
        with self.binding_sources.connect() as connection, connection.transaction():
            workarea = connection.execute(
                "SELECT 1 FROM workareas WHERE workarea_id = %s AND owner = %s",
                (workarea_id, owner),
            ).fetchone()
            if workarea is None:
                raise KeyError(workarea_id)

            source_row = connection.execute(
                """
                SELECT source FROM workarea_binding_sources
                WHERE workarea_id = %s AND binding_kind = %s AND target_ref = %s
                FOR UPDATE
                """,
                (workarea_id, binding_kind, target_ref),
            ).fetchone()
            # A missing sidecar row can only be a historical pre-006c.005 edge;
            # the migration contract treats those as explicit user bindings.
            source = "user" if source_row is None else str(source_row["source"])
            if source != "user":
                raise WorkAreaBindingRemovalConflict(
                    "inherited WorkArea bindings cannot be removed"
                )

            if binding_kind == "contract":
                dependency = connection.execute(
                    """
                    SELECT candidate_id FROM launch_candidates
                    WHERE workarea_id = %s AND owner = %s AND contract_id = %s
                    LIMIT 1
                    """,
                    (workarea_id, owner, target_ref),
                ).fetchone()
                if dependency is not None:
                    raise WorkAreaBindingRemovalConflict(
                        "Contract binding is required by durable LaunchCandidate provenance"
                    )
                result = connection.execute(
                    """
                    DELETE FROM workarea_contracts
                    WHERE workarea_id = %s AND contract_id = %s
                    """,
                    (workarea_id, target_ref),
                )
            elif binding_kind == "run":
                dependency = connection.execute(
                    """
                    SELECT lr.launch_id FROM launch_runs AS lr
                    JOIN launches AS l ON l.launch_id = lr.launch_id
                    WHERE l.workarea_id = %s AND l.owner = %s AND lr.run_id = %s
                    LIMIT 1
                    """,
                    (workarea_id, owner, target_ref),
                ).fetchone()
                if dependency is not None:
                    raise WorkAreaBindingRemovalConflict(
                        "Run binding is required by durable Launch provenance"
                    )
                result = connection.execute(
                    """
                    DELETE FROM workarea_runs
                    WHERE workarea_id = %s AND run_id = %s
                    """,
                    (workarea_id, target_ref),
                )
            else:
                result = connection.execute(
                    """
                    DELETE FROM workarea_assets
                    WHERE workarea_id = %s AND asset_ref = %s
                    """,
                    (workarea_id, target_ref),
                )

            if result.rowcount != 1:
                raise KeyError(target_ref)

            connection.execute(
                """
                DELETE FROM workarea_binding_sources
                WHERE workarea_id = %s AND binding_kind = %s AND target_ref = %s
                """,
                (workarea_id, binding_kind, target_ref),
            )
            connection.execute(
                """
                UPDATE workareas SET updated_at = %s
                WHERE workarea_id = %s AND owner = %s
                """,
                (now, workarea_id, owner),
            )


def _validate(workarea_id: str, owner: str, kind: str, target_ref: str) -> None:
    if kind not in _REMOVABLE_KINDS:
        raise ValueError("binding_kind must be asset, contract, or run")
    for value, label, limit in (
        (workarea_id, "workarea_id", 256),
        (owner, "owner", 64),
        (target_ref, "target_ref", 4_096),
    ):
        if not isinstance(value, str) or not value.strip() or "\0" in value:
            raise ValueError(f"{label} is invalid")
        if len(value) > limit:
            raise ValueError(f"{label} is too long")


__all__ = [
    "PostgresWorkAreaBindingRemovalService",
    "WorkAreaBindingRemovalConflict",
]
