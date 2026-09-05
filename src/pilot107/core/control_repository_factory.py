"""PostgreSQL-only runtime selection for the control-plane repository."""

from pathlib import Path

from pilot107.core.control_repository import ControlRepository
from pilot107.core.postgres_control_repository import PostgresControlRepository


def build_control_repository(
    *,
    sqlite_path: Path,
    postgres_dsn: str | None,
) -> ControlRepository:
    """Build the canonical control repository.

    ``sqlite_path`` remains in the call signature during source migration so
    older composition code does not need an unrelated signature change. It is
    deliberately ignored: SQLite is no longer a runtime authority.
    """

    del sqlite_path
    if not postgres_dsn:
        raise ValueError(
            "PostgreSQL control repository is required; SQLite runtime authority has been retired"
        )
    return PostgresControlRepository(postgres_dsn)
