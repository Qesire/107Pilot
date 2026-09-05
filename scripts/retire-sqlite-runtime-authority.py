from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def _write(relative: str, text: str) -> None:
    (ROOT / relative).write_text(text, encoding="utf-8")


def _replace_once(text: str, old: str, new: str, *, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected exactly one match, found {count}")
    return text.replace(old, new, 1)


def _replace_function(text: str, name: str, replacement: str, *, label: str) -> str:
    tree = ast.parse(text)
    matches = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
    ]
    if len(matches) != 1:
        raise RuntimeError(f"{label}: expected one top-level function {name}, found {len(matches)}")
    node = matches[0]
    lines = text.splitlines(keepends=True)
    start = node.lineno - 1
    end = node.end_lineno
    return "".join([*lines[:start], replacement.rstrip() + "\n\n", *lines[end:]])


def _patch_api_service() -> None:
    path = "src/pilot107/api/service.py"
    text = _read(path)
    text = _replace_once(
        text,
        "    database_mode: DatabaseMode = DatabaseMode.SQLITE\n",
        "    database_mode: DatabaseMode = DatabaseMode.POSTGRES\n",
        label="api config default",
    )
    text = _replace_once(
        text,
        "from pilot107.observability.store import SQLiteObservabilityStore\n",
        "",
        label="api SQLite observability import",
    )
    text = _replace_once(
        text,
        "from pilot107.runtime_watch.store import SQLiteRuntimeWatchStore\n",
        "",
        label="api SQLite runtime-watch import",
    )
    text = _replace_once(
        text,
        """    upload_store: UploadSessionStore
    if selection.is_postgres:
        assert selection.postgres_dsn is not None
        upload_store = PostgresUploadSessionStore(
            selection.postgres_dsn, compatibility_path=selection.sqlite_path
        )
    else:
        upload_store = UploadSessionStore(selection.sqlite_path)
""",
        """    postgres_dsn = selection.postgres_dsn
    if postgres_dsn is None:
        raise RuntimeError("PostgreSQL durable-store selection lost its DSN")
    upload_store = PostgresUploadSessionStore(
        postgres_dsn,
        compatibility_path=selection.sqlite_path,
    )
""",
        label="api upload store fallback",
    )
    text = _replace_once(
        text,
        """    postgres_dsn = selection.postgres_dsn or ""
    agent_capability_secret = _load_agent_capability_secret(config)
    if not selection.is_postgres:
        store = RunStore(selection.sqlite_path)
        contract_store = ContractStore(selection.sqlite_path)
        platform_snapshot_store = PlatformSnapshotStore(selection.sqlite_path)
        user_entitlement_store = UserEntitlementStore(selection.sqlite_path)
        remediation_store = RemediationStore(selection.sqlite_path)
        repair_ticket_store = RepairTicketStore(selection.sqlite_path)
    else:
        assert postgres_dsn is not None
        store = PostgresRunStore(postgres_dsn, compatibility_path=selection.sqlite_path)
        contract_store = PostgresContractStore(
            postgres_dsn,
            compatibility_path=selection.sqlite_path,
        )
        platform_snapshot_store = PostgresPlatformSnapshotStore(
            postgres_dsn,
            compatibility_path=selection.sqlite_path,
        )
        user_entitlement_store = PostgresUserEntitlementStore(
            postgres_dsn,
            compatibility_path=selection.sqlite_path,
        )
        remediation_store = PostgresRemediationStore(
            postgres_dsn,
            compatibility_path=selection.sqlite_path,
        )
        repair_ticket_store = PostgresRepairTicketStore(
            postgres_dsn,
            compatibility_path=selection.sqlite_path,
        )
""",
        """    postgres_dsn = selection.postgres_dsn
    if postgres_dsn is None:
        raise RuntimeError("PostgreSQL durable-store selection lost its DSN")
    agent_capability_secret = _load_agent_capability_secret(config)
    store = PostgresRunStore(postgres_dsn, compatibility_path=selection.sqlite_path)
    contract_store = PostgresContractStore(
        postgres_dsn,
        compatibility_path=selection.sqlite_path,
    )
    platform_snapshot_store = PostgresPlatformSnapshotStore(
        postgres_dsn,
        compatibility_path=selection.sqlite_path,
    )
    user_entitlement_store = PostgresUserEntitlementStore(
        postgres_dsn,
        compatibility_path=selection.sqlite_path,
    )
    remediation_store = PostgresRemediationStore(
        postgres_dsn,
        compatibility_path=selection.sqlite_path,
    )
    repair_ticket_store = PostgresRepairTicketStore(
        postgres_dsn,
        compatibility_path=selection.sqlite_path,
    )
""",
        label="api primary durable stores",
    )
    text = _replace_once(
        text,
        """    if not selection.is_postgres:
        template_market_store = TemplateMarketStore(
            config.db_path,
            publication_gate=publication_gate,
            contract_service=contract_service,
        )
    else:
        template_market_store = PostgresTemplateMarketStore(
            postgres_dsn,
            compatibility_path=selection.sqlite_path,
            publication_gate=publication_gate,
            contract_service=contract_service,
        )
    if not selection.is_postgres:
        run_publication_store = RunPublicationStore(
            config.db_path,
            run_store=store,
            contract_service=contract_service,
        )
    else:
        run_publication_store = PostgresRunPublicationStore(
            postgres_dsn,
            compatibility_path=selection.sqlite_path,
            run_store=store,
            contract_service=contract_service,
        )
""",
        """    template_market_store = PostgresTemplateMarketStore(
        postgres_dsn,
        compatibility_path=selection.sqlite_path,
        publication_gate=publication_gate,
        contract_service=contract_service,
    )
    run_publication_store = PostgresRunPublicationStore(
        postgres_dsn,
        compatibility_path=selection.sqlite_path,
        run_store=store,
        contract_service=contract_service,
    )
""",
        label="api template and publication stores",
    )
    text = _replace_once(
        text,
        """    observation_store = (
        SQLiteObservabilityStore(config.db_path)
        if not selection.is_postgres
        else PostgresObservabilityStore(
            postgres_dsn,
            compatibility_path=selection.sqlite_path,
        )
    )
""",
        """    observation_store = PostgresObservabilityStore(
        postgres_dsn,
        compatibility_path=selection.sqlite_path,
    )
""",
        label="api observability fallback",
    )
    text = _replace_once(
        text,
        """    runtime_watch_store = (
        SQLiteRuntimeWatchStore(
            config.db_path,
            segment_root=config.evidence_root / "runtime-watch-segments",
        )
        if not selection.is_postgres
        else PostgresRuntimeWatchStore(
            postgres_dsn,
            segment_root=config.evidence_root / "runtime-watch-segments",
        )
    )
""",
        """    runtime_watch_store = PostgresRuntimeWatchStore(
        postgres_dsn,
        segment_root=config.evidence_root / "runtime-watch-segments",
    )
""",
        label="api runtime-watch fallback",
    )
    text = _replace_once(
        text,
        """                store=(
                    PostgresSshConnectionStore(
                        postgres_dsn,
                        compatibility_path=selection.sqlite_path,
                    )
                    if selection.is_postgres
                    else SshConnectionStore(selection.sqlite_path)
                ),
""",
        """                store=PostgresSshConnectionStore(
                    postgres_dsn,
                    compatibility_path=selection.sqlite_path,
                ),
""",
        label="api ssh store fallback",
    )
    old_database_mode = """def _database_mode(values: Mapping[str, str], *, postgres_dsn: str | None) -> DatabaseMode:
    configured = values.get("PILOT107_DATABASE_MODE")
    inferred = "postgres" if postgres_dsn else "sqlite"
    try:
        return DatabaseMode(configured or inferred)
    except ValueError as exc:
        raise ValueError("PILOT107_DATABASE_MODE must be sqlite or postgres") from exc
"""
    new_database_mode = """def _database_mode(values: Mapping[str, str], *, postgres_dsn: str | None) -> DatabaseMode:
    del postgres_dsn
    configured = (values.get("PILOT107_DATABASE_MODE") or "postgres").strip().lower()
    if configured == "sqlite":
        raise ValueError("SQLite runtime authority has been retired; configure PostgreSQL")
    if configured != "postgres":
        raise ValueError("PILOT107_DATABASE_MODE must be postgres")
    return DatabaseMode.POSTGRES
"""
    text = _replace_once(text, old_database_mode, new_database_mode, label="api database mode")
    _write(path, text)


def _patch_worker_service() -> None:
    path = "src/pilot107/worker/service.py"
    text = _read(path)
    text = _replace_once(
        text,
        "    database_mode: DatabaseMode = DatabaseMode.SQLITE\n",
        "    database_mode: DatabaseMode = DatabaseMode.POSTGRES\n",
        label="worker config default",
    )
    text = _replace_once(
        text,
        "from pilot107.observability.store import SQLiteObservabilityStore\n",
        "",
        label="worker SQLite observability import",
    )
    text = _replace_once(
        text,
        "from pilot107.runtime_watch.store import SQLiteRuntimeWatchStore\n",
        "",
        label="worker SQLite runtime-watch import",
    )
    text = _replace_once(
        text,
        """    postgres_dsn = selection.postgres_dsn or ""
    agent_capability_secret = _load_agent_capability_secret(config)
    if not selection.is_postgres:
        store = RunStore(selection.sqlite_path)
        contract_store = ContractStore(selection.sqlite_path)
        platform_snapshot_store = PlatformSnapshotStore(selection.sqlite_path)
        remediation_store = RemediationStore(selection.sqlite_path)
    else:
        store = PostgresRunStore(postgres_dsn, compatibility_path=selection.sqlite_path)
        contract_store = PostgresContractStore(
            postgres_dsn,
            compatibility_path=selection.sqlite_path,
        )
        platform_snapshot_store = PostgresPlatformSnapshotStore(
            postgres_dsn,
            compatibility_path=selection.sqlite_path,
        )
        remediation_store = PostgresRemediationStore(
            postgres_dsn,
            compatibility_path=selection.sqlite_path,
        )
""",
        """    postgres_dsn = selection.postgres_dsn
    if postgres_dsn is None:
        raise RuntimeError("PostgreSQL durable-store selection lost its DSN")
    agent_capability_secret = _load_agent_capability_secret(config)
    store = PostgresRunStore(postgres_dsn, compatibility_path=selection.sqlite_path)
    contract_store = PostgresContractStore(
        postgres_dsn,
        compatibility_path=selection.sqlite_path,
    )
    platform_snapshot_store = PostgresPlatformSnapshotStore(
        postgres_dsn,
        compatibility_path=selection.sqlite_path,
    )
    remediation_store = PostgresRemediationStore(
        postgres_dsn,
        compatibility_path=selection.sqlite_path,
    )
""",
        label="worker primary durable stores",
    )
    text = _replace_once(
        text,
        """        runtime_store = (
            SQLiteRuntimeWatchStore(config.db_path, segment_root=segment_root)
            if not selection.is_postgres
            else PostgresRuntimeWatchStore(
                postgres_dsn,
                segment_root=segment_root,
            )
        )
""",
        """        runtime_store = PostgresRuntimeWatchStore(
            postgres_dsn,
            segment_root=segment_root,
        )
""",
        label="worker runtime-watch fallback",
    )
    text = _replace_once(
        text,
        """        observation_store = (
            SQLiteObservabilityStore(config.db_path)
            if not selection.is_postgres
            else PostgresObservabilityStore(
                postgres_dsn,
                compatibility_path=selection.sqlite_path,
            )
        )
""",
        """        observation_store = PostgresObservabilityStore(
            postgres_dsn,
            compatibility_path=selection.sqlite_path,
        )
""",
        label="worker observability fallback",
    )
    old_database_mode = """def _database_mode(values: Mapping[str, str], *, postgres_dsn: str | None) -> DatabaseMode:
    configured = values.get("PILOT107_DATABASE_MODE")
    inferred = "postgres" if postgres_dsn else "sqlite"
    try:
        return DatabaseMode(configured or inferred)
    except ValueError as exc:
        raise ValueError("PILOT107_DATABASE_MODE must be sqlite or postgres") from exc
"""
    new_database_mode = """def _database_mode(values: Mapping[str, str], *, postgres_dsn: str | None) -> DatabaseMode:
    del postgres_dsn
    configured = (values.get("PILOT107_DATABASE_MODE") or "postgres").strip().lower()
    if configured == "sqlite":
        raise ValueError("SQLite runtime authority has been retired; configure PostgreSQL")
    if configured != "postgres":
        raise ValueError("PILOT107_DATABASE_MODE must be postgres")
    return DatabaseMode.POSTGRES
"""
    text = _replace_once(text, old_database_mode, new_database_mode, label="worker database mode")
    _write(path, text)


def _patch_http_app() -> None:
    path = "src/pilot107/api/http_app.py"
    text = _read(path)
    text = _replace_once(
        text,
        "from pilot107.agent.store import SQLiteAgentSessionStore\n",
        "",
        label="http SQLite AgentSession import",
    )
    text = _replace_once(
        text,
        "from pilot107.core.control_repository import ControlRepository, SQLiteControlRepository\n",
        "from pilot107.core.control_repository import ControlRepository\n",
        label="http SQLite control import",
    )
    text = _replace_once(
        text,
        "        self.control_repository = control_repository or SQLiteControlRepository(store.db_path)\n",
        """        if control_repository is None:
            raise ValueError("control_repository is required; SQLite fallback has been retired")
        self.control_repository = control_repository
""",
        label="http control fallback",
    )
    text = _replace_once(
        text,
        """        self.remediation_service = remediation_service or RemediationService(
            run_store=store,
            remediation_store=remediation_store or RemediationStore(store.db_path),
            advice_service=self.agent_advice_service,
            contract_store=contract_store,
            evidence_store=evidence_store,
            project_agent_service=project_agent_service,
        )
""",
        """        if remediation_service is None and remediation_store is None:
            raise ValueError("remediation_store is required; SQLite fallback has been retired")
        self.remediation_service = remediation_service or RemediationService(
            run_store=store,
            remediation_store=remediation_store,
            advice_service=self.agent_advice_service,
            contract_store=contract_store,
            evidence_store=evidence_store,
            project_agent_service=project_agent_service,
        )
""",
        label="http remediation fallback",
    )
    text = _replace_once(
        text,
        """        self.repair_ticket_service = RepairTicketService(
            run_store=store,
            repair_ticket_store=repair_ticket_store or RepairTicketStore(store.db_path),
            remediation_store=self.remediation_service.remediation_store,
""",
        """        if repair_ticket_store is None:
            raise ValueError("repair_ticket_store is required; SQLite fallback has been retired")
        self.repair_ticket_service = RepairTicketService(
            run_store=store,
            repair_ticket_store=repair_ticket_store,
            remediation_store=self.remediation_service.remediation_store,
""",
        label="http repair-ticket fallback",
    )
    replacement = '''def build_api(
    *,
    db_path: Path,
    evidence_root: Path,
    auth_required: bool = False,
    trusted_user_header: str = "X-Pilot107-User",
) -> Pilot107HttpApi:
    """Retired SQLite composition entrypoint.

    Production composition is PostgreSQL-only and must go through
    :func:`pilot107.api.service.build_api_service`, which injects every durable
    repository explicitly.  The legacy signature remains only as an explicit
    fail-closed compatibility sentinel for callers that have not migrated yet.
    """

    del db_path, evidence_root, auth_required, trusted_user_header
    raise RuntimeError(
        "build_api SQLite composition has been retired; use build_api_service with PostgreSQL"
    )
'''
    text = _replace_function(text, "build_api", replacement, label="http build_api")
    _write(path, text)


def _patch_architecture_gate() -> None:
    path = "tests/test_postgres_only_runtime_authority.py"
    text = _read(path)
    marker = "def test_production_composition_has_no_sqlite_runtime_authority() -> None:"
    if marker in text:
        return
    addition = '''\n\ndef test_production_composition_has_no_sqlite_runtime_authority() -> None:
    production_sources = (
        "src/pilot107/api/service.py",
        "src/pilot107/worker/service.py",
        "src/pilot107/api/http_app.py",
    )
    forbidden = (
        "DatabaseMode.SQLITE",
        "SQLiteObservabilityStore",
        "SQLiteRuntimeWatchStore",
        "SQLiteControlRepository",
        "SQLiteAgentSessionStore",
        "if not selection.is_postgres",
    )
    for relative in production_sources:
        source = _source(relative)
        assert all(token not in source for token in forbidden), relative


def test_legacy_http_composition_is_an_explicit_fail_closed_sentinel() -> None:
    source = _source("src/pilot107/api/http_app.py")
    assert "build_api SQLite composition has been retired" in source
    assert "control_repository is required; SQLite fallback has been retired" in source
    assert "remediation_store is required; SQLite fallback has been retired" in source
    assert "repair_ticket_store is required; SQLite fallback has been retired" in source
'''
    _write(path, text.rstrip() + addition + "\n")


def main() -> None:
    _patch_api_service()
    _patch_worker_service()
    _patch_http_app()
    _patch_architecture_gate()


if __name__ == "__main__":
    main()
