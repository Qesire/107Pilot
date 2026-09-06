from __future__ import annotations

import re
from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    file_path = Path(path)
    text = file_path.read_text(encoding="utf-8")
    count = text.count(old)
    duplicate_reload = (
        path == "tests/api/test_service_template_seed_wiring.py"
        and old == "    importlib.reload(service_module)\n"
        and count == 2
    )
    if count != 1 and not duplicate_reload:
        raise SystemExit(f"{path}: expected exactly one match for {old!r}, got {count}")
    file_path.write_text(text.replace(old, new, 1), encoding="utf-8")


def regex_once(path: str, pattern: str, replacement: str) -> None:
    file_path = Path(path)
    text = file_path.read_text(encoding="utf-8")
    updated, count = re.subn(pattern, replacement, text, count=1, flags=re.S)
    if count != 1:
        raise SystemExit(f"{path}: regex replacement count={count} pattern={pattern!r}")
    file_path.write_text(updated, encoding="utf-8")


# Operation durability remains available to an explicitly constructed SQLite
# AgentSessionStore in tests/dev, while arbitrary db_path duck-types fail closed.
replace_once(
    "src/pilot107/agent/operation_ledger.py",
    "from pilot107.agent.store import AgentSessionStore",
    "from pilot107.agent.store import AgentSessionStore, SQLiteAgentSessionStore",
)
regex_once(
    "src/pilot107/agent/operation_ledger.py",
    r"def build_agent_operation_ledger\(.*?\n\n\n_SQLITE_MIGRATIONS =",
    '''def build_agent_operation_ledger(
    store: AgentSessionStore,
    *,
    clock: Callable[[], datetime] | None = None,
) -> AgentOperationLedger | None:
    """Build operation durability without reintroducing runtime SQLite selection."""

    dsn = getattr(store, "dsn", None)
    if isinstance(dsn, str) and dsn:
        return PostgresAgentOperationLedger(dsn, clock=clock)
    if isinstance(store, SQLiteAgentSessionStore):
        return SQLiteAgentOperationLedger(store.db_path, clock=clock)
    if isinstance(getattr(store, "db_path", None), Path):
        raise RuntimeError(
            "Agent operation ledger requires PostgreSQL; "
            "SQLite runtime authority has been retired"
        )
    return None


_SQLITE_MIGRATIONS =''',
)

replace_once(
    "src/pilot107/agent/operation_attempts.py",
    "from typing import Any, Protocol",
    "from typing import Any, Protocol\n\nfrom pilot107.agent.store import SQLiteAgentSessionStore",
)
regex_once(
    "src/pilot107/agent/operation_attempts.py",
    r"def build_agent_operation_attempt_store\(.*?\n\n\n_SQLITE_MIGRATIONS =",
    '''def build_agent_operation_attempt_store(
    session_store: object,
    *,
    clock: Callable[[], datetime] | None = None,
) -> AgentOperationAttemptStore | None:
    """Build attempt durability for PostgreSQL or an explicit SQLite test store."""

    dsn = getattr(session_store, "dsn", None)
    if isinstance(dsn, str) and dsn:
        return PostgresAgentOperationAttemptStore(dsn, clock=clock)
    if isinstance(session_store, SQLiteAgentSessionStore):
        return SQLiteAgentOperationAttemptStore(session_store.db_path, clock=clock)
    if isinstance(getattr(session_store, "db_path", None), Path):
        raise RuntimeError(
            "Agent operation attempt store requires PostgreSQL; "
            "SQLite runtime authority has been retired"
        )
    return None


_SQLITE_MIGRATIONS =''',
)

replace_once(
    "src/pilot107/agent/operation_reconciler.py",
    "from pilot107.agent.protocol import ToolInvocation",
    "from pilot107.agent.protocol import ToolInvocation\nfrom pilot107.agent.store import SQLiteAgentSessionStore",
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
    """Build reconciliation against PostgreSQL or an explicit SQLite test store."""

    if ledger is None:
        return None
    dsn = getattr(store, "dsn", None)
    if isinstance(dsn, str) and dsn:
        return PostgresAgentOperationReconciler(dsn, ledger=ledger, clock=clock)
    if isinstance(store, SQLiteAgentSessionStore):
        return SQLiteAgentOperationReconciler(store.db_path, ledger=ledger, clock=clock)
    if isinstance(getattr(store, "db_path", None), Path):
        raise RuntimeError(
            "Agent operation reconciler requires PostgreSQL; "
            "SQLite runtime authority has been retired"
        )
    return None


class SQLiteAgentOperationReconciler:''',
)

# Obsolete factory tests now assert the frozen PostgreSQL-only production contract.
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

# These tests reload the service module only to pick up environment variables,
# but config_from_env already reads the environment per call. Reloading destroys
# the pytest-only composition seam and is therefore both unnecessary and harmful.
replace_once(
    "tests/api/test_service_snapshot_wiring.py",
    "import contextlib\nimport importlib\nimport threading",
    "import contextlib\nimport threading",
)
replace_once(
    "tests/api/test_service_snapshot_wiring.py",
    '''def _reload_service_module():
    from pilot107.api import service as service_module
    importlib.reload(service_module)
    return service_module
''',
    '''def _reload_service_module():
    from pilot107.api import service as service_module
    return service_module
''',
)
replace_once(
    "tests/api/test_service_template_seed_wiring.py",
    "import contextlib\nimport importlib",
    "import contextlib",
)
replace_once(
    "tests/api/test_service_template_seed_wiring.py",
    "    importlib.reload(service_module)\n",
    "",
)
# The template-seed file has the same reload in both tests.
replace_once(
    "tests/api/test_service_template_seed_wiring.py",
    "    importlib.reload(service_module)\n",
    "",
)
