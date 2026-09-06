from __future__ import annotations

import re
import subprocess
from pathlib import Path

BASE = "62b62c3a53d78bf4a8ad5bf3b77b134849d1ea29"


def restore(path: str) -> None:
    result = subprocess.run(
        ["git", "show", f"{BASE}:{path}"],
        check=True,
        stdout=subprocess.PIPE,
    )
    Path(path).write_bytes(result.stdout)


def replace_once(path: str, old: str, new: str) -> None:
    file_path = Path(path)
    text = file_path.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{path}: expected exactly one match for {old!r}, got {count}")
    file_path.write_text(text.replace(old, new, 1), encoding="utf-8")


def regex_once(path: str, pattern: str, replacement: str) -> None:
    file_path = Path(path)
    text = file_path.read_text(encoding="utf-8")
    updated, count = re.subn(pattern, replacement, text, count=1, flags=re.S)
    if count != 1:
        raise SystemExit(f"{path}: regex replacement count={count} pattern={pattern!r}")
    file_path.write_text(updated, encoding="utf-8")


def restore_service_test_seams(path: str) -> None:
    restore(path)
    replace_once(
        path,
        "database_mode: DatabaseMode = DatabaseMode.SQLITE",
        "database_mode: DatabaseMode = DatabaseMode.POSTGRES",
    )
    replace_once(
        path,
        '''def _database_mode(values: Mapping[str, str], *, postgres_dsn: str | None) -> DatabaseMode:
    configured = values.get("PILOT107_DATABASE_MODE")
    inferred = "postgres" if postgres_dsn else "sqlite"
    try:
        return DatabaseMode(configured or inferred)
    except ValueError as exc:
        raise ValueError("PILOT107_DATABASE_MODE must be sqlite or postgres") from exc
''',
        '''def _database_mode(values: Mapping[str, str], *, postgres_dsn: str | None) -> DatabaseMode:
    del postgres_dsn
    configured = (values.get("PILOT107_DATABASE_MODE") or "postgres").strip().lower()
    if configured == "sqlite":
        raise ValueError("SQLite runtime authority has been retired; configure PostgreSQL")
    if configured != "postgres":
        raise ValueError("PILOT107_DATABASE_MODE must be postgres")
    return DatabaseMode.POSTGRES
''',
    )


restore_service_test_seams("src/pilot107/api/service.py")
restore_service_test_seams("src/pilot107/worker/service.py")

regex_once(
    "src/pilot107/agent/operation_ledger.py",
    r"def build_agent_operation_ledger\(.*?\n\n\n_SQLITE_MIGRATIONS =",
    '''def build_agent_operation_ledger(
    store: AgentSessionStore,
    *,
    clock: Callable[[], datetime] | None = None,
) -> AgentOperationLedger | None:
    """Build the PostgreSQL operation ledger or fail closed for legacy SQLite stores."""

    dsn = getattr(store, "dsn", None)
    if isinstance(dsn, str) and dsn:
        return PostgresAgentOperationLedger(dsn, clock=clock)
    if isinstance(getattr(store, "db_path", None), Path):
        raise RuntimeError(
            "Agent operation ledger requires PostgreSQL; "
            "SQLite runtime authority has been retired"
        )
    return None


_SQLITE_MIGRATIONS =''',
)

regex_once(
    "src/pilot107/agent/operation_attempts.py",
    r"def build_agent_operation_attempt_store\(.*?\n\n\n_SQLITE_MIGRATIONS =",
    '''def build_agent_operation_attempt_store(
    session_store: object,
    *,
    clock: Callable[[], datetime] | None = None,
) -> AgentOperationAttemptStore | None:
    """Build the PostgreSQL attempt store or fail closed for legacy SQLite stores."""

    dsn = getattr(session_store, "dsn", None)
    if isinstance(dsn, str) and dsn:
        return PostgresAgentOperationAttemptStore(dsn, clock=clock)
    if isinstance(getattr(session_store, "db_path", None), Path):
        raise RuntimeError(
            "Agent operation attempt store requires PostgreSQL; "
            "SQLite runtime authority has been retired"
        )
    return None


_SQLITE_MIGRATIONS =''',
)

regex_once(
    "src/pilot107/agent/operation_reconciler.py",
    r"def build_agent_operation_reconciler\(.*?\n\n\nclass SQLiteAgentOperationReconciler:",
    '''def build_agent_operation_reconciler(
    store: object,
    ledger: AgentOperationLedger | None,
    *,
    clock: Callable[[], datetime] | None = None,
) -> AgentOperationReconciler | None:
    """Build the PostgreSQL reconciler against the Session authority."""

    if ledger is None:
        return None
    dsn = getattr(store, "dsn", None)
    if isinstance(dsn, str) and dsn:
        return PostgresAgentOperationReconciler(dsn, ledger=ledger, clock=clock)
    if isinstance(getattr(store, "db_path", None), Path):
        raise RuntimeError(
            "Agent operation reconciler requires PostgreSQL; "
            "SQLite runtime authority has been retired"
        )
    return None


class SQLiteAgentOperationReconciler:''',
)

replace_once(
    "tests/agent/test_postgres_store.py",
    "from pilot107.agent.store import SQLiteAgentSessionStore\n"
    "from pilot107.agent.store_factory import build_agent_session_store",
    "from pilot107.agent.store_factory import ConfigurationError, build_agent_session_store",
)
regex_once(
    "tests/agent/test_postgres_store.py",
    r"def test_factory_selects_sqlite_without_postgres_dsn\(tmp_path: Path\) -> None:\n.*?\n\n\ndef test_factory_selects_postgres_when_dsn_is_present",
    '''def test_factory_rejects_missing_postgres_dsn(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="requires PostgreSQL"):
        build_agent_session_store(
            sqlite_path=tmp_path / "agent.db",
            postgres_dsn=None,
        )


def test_factory_selects_postgres_when_dsn_is_present''',
)
replace_once(
    "tests/agent/test_postgres_store.py",
    '''    monkeypatch.setattr(
        "pilot107.agent.store_factory.PostgresAgentSessionStore",
        lambda dsn: sentinel,
    )

    store = build_agent_session_store(''',
    '''    monkeypatch.setattr(
        "pilot107.agent.store_factory.PostgresAgentSessionStore",
        lambda dsn: sentinel,
    )
    monkeypatch.setattr(
        "pilot107.agent.store_factory.ensure_postgres_checkpoint_pointer",
        lambda dsn: None,
    )

    store = build_agent_session_store(''',
)

replace_once(
    "tests/agent/test_project_store_contract.py",
    "from pilot107.agent.store_factory import build_project_store",
    "from pilot107.agent.store_factory import ConfigurationError, build_project_store",
)
regex_once(
    "tests/agent/test_project_store_contract.py",
    r"def test_factory_selects_sqlite_project_store\(tmp_path: Path\) -> None:\n.*?\n\n\ndef test_factory_selects_postgres_project_store",
    '''def test_factory_rejects_missing_postgres_project_dsn(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="requires PostgreSQL"):
        build_project_store(
            sqlite_path=tmp_path / "projects.db",
            postgres_dsn=None,
        )


def test_factory_selects_postgres_project_store''',
)

replace_once(
    "tests/agent/test_task_store_contract.py",
    "from pilot107.agent.store_factory import build_agent_task_store",
    "from pilot107.agent.store_factory import ConfigurationError, build_agent_task_store",
)
regex_once(
    "tests/agent/test_task_store_contract.py",
    r"def test_factory_selects_agent_task_store\(tmp_path: Path\) -> None:\n.*?\n\n\ndef test_factory_selects_postgres_agent_task_store",
    '''def test_factory_rejects_missing_postgres_agent_task_dsn(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="requires PostgreSQL"):
        build_agent_task_store(
            sqlite_path=tmp_path / "tasks.db",
            postgres_dsn=None,
        )


def test_factory_selects_postgres_agent_task_store''',
)

regex_once(
    "tests/test_control_repository.py",
    r"    def test_defaults_to_sqlite_and_selects_postgres_when_dsn_is_present\(self\) -> None:\n.*?        postgres.assert_called_once_with\(\"postgresql://control.example/pilot107\"\)\n",
    '''    def test_requires_postgres_and_selects_postgres_when_dsn_is_present(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temporary,
            self.assertRaisesRegex(
                ValueError,
                "PostgreSQL control repository is required",
            ),
        ):
            build_control_repository(
                sqlite_path=Path(temporary) / "control.db",
                postgres_dsn=None,
            )

        with patch(
            "pilot107.core.control_repository_factory.PostgresControlRepository"
        ) as postgres:
            selected = build_control_repository(
                sqlite_path=Path("unused.db"),
                postgres_dsn="postgresql://control.example/pilot107",
            )
        self.assertIs(selected, postgres.return_value)
        postgres.assert_called_once_with("postgresql://control.example/pilot107")
''',
)
