"""Runtime selection for the control-plane consistency repository."""

from pathlib import Path

from pilot107.core.control_repository import ControlRepository, SQLiteControlRepository
from pilot107.core.postgres_control_repository import PostgresControlRepository


def build_control_repository(
    *,
    sqlite_path: Path,
    postgres_dsn: str | None,
) -> ControlRepository:
    if postgres_dsn is not None:
        return PostgresControlRepository(postgres_dsn)
    return SQLiteControlRepository(sqlite_path)
