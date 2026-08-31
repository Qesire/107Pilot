import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from pilot107.api.health import ApiHealthService
from pilot107.core.control_repository import SQLiteControlRepository
from pilot107.core.platform_snapshot import (
    ObservationSourceType,
    PlatformSnapshot,
    PlatformSnapshotScope,
)
from pilot107.core.platform_snapshot_store import PlatformSnapshotStore
from pilot107.core.postgres_control_repository import PostgresControlRepository
from pilot107.core.postgres_domain_migration import migrate_sqlite_domain_to_postgres
from pilot107.core.postgres_domain_schema import persisted_table_names
from pilot107.core.postgres_domain_stores import (
    PostgresPlatformSnapshotStore,
    PostgresRemediationStore,
    PostgresRunStore,
    PostgresTemplateMarketStore,
    PostgresUserEntitlementStore,
)
from pilot107.core.remediation import RemediationBudget, RemediationState
from pilot107.core.remediation_store import RemediationStore
from pilot107.core.run_store import EvidenceSealClaimConflict, RunStore
from pilot107.core.template_market import TemplateMarketStore, TemplateVisibility


@unittest.skipUnless(
    os.environ.get("PILOT107_TEST_POSTGRES_DSN")
    and os.environ.get("PILOT107_TEST_POSTGRES_ALLOW_RESET") == "1",
    "set a dedicated PILOT107_TEST_POSTGRES_DSN and explicit reset opt-in",
)
class PostgresDomainMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dsn = os.environ["PILOT107_TEST_POSTGRES_DSN"]
        self._temporary = tempfile.TemporaryDirectory()
        self.runtime_path = Path(self._temporary.name) / "runtime.db"
        target = PostgresRunStore(self.dsn, compatibility_path=self.runtime_path)
        PostgresControlRepository(self.dsn)
        with target.connect() as conn:
            conn.execute(
                "TRUNCATE "
                + ", ".join(reversed(persisted_table_names()))
                + " RESTART IDENTITY CASCADE"
            )

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def test_quiesced_copy_preserves_all_active_business_domains(self) -> None:
        source_path = Path(self._temporary.name) / "source.db"
        runs = RunStore(source_path)
        runs.create_run(
            run_id="run_source",
            owner="alice",
            workdir="/public/home/alice/project",
            script="echo postgres",
            job_name="postgres migration proof",
        )
        runs.append_event(
            run_id="run_source",
            event_type="migration.proof",
            payload={"stage": "source"},
        )
        SQLiteControlRepository(source_path).enqueue(
            message_id="migration-control-message",
            topic="run.submit",
            aggregate_id="run_source",
            payload={"run_id": "run_source"},
        )
        PlatformSnapshotStore(source_path).create(
            owner="alice",
            snapshot=PlatformSnapshot(
                snapshot_id="snapshot_source",
                scope=PlatformSnapshotScope.LOGIN_NODE,
                captured_at="2026-07-24T00:00:00+00:00",
                collector_version="test.v1",
            ),
            source_type=ObservationSourceType.SIMULATOR,
            source_name="postgres-domain-test",
            expires_at=None,
        )
        TemplateMarketStore(source_path).create_draft(
            owner="alice",
            title="PostgreSQL migration template",
            description="copy verification",
            visibility=TemplateVisibility.PRIVATE,
            payload={"entry": {"command": "echo postgres"}},
            draft_id="draft_source",
            template_id="template_source",
        )
        RemediationStore(source_path).create_session(
            session_id="session_source",
            owner="alice",
            request_key="migration-proof",
            state=RemediationState.WAITING_EVIDENCE,
            source_run_id="run_source",
            source_contract_id=None,
            source_diagnosis_digest="d" * 64,
            source_evidence_digest="e" * 64,
            automation_policy="manual_approval",
            budget=RemediationBudget(),
        )

        report = migrate_sqlite_domain_to_postgres(
            sqlite_path=source_path,
            postgres_dsn=self.dsn,
            source_quiesced=True,
        )

        self.assertTrue(report.transferred)
        self.assertEqual(report.source_digest, report.target_digest)
        postgres_runs = PostgresRunStore(self.dsn, compatibility_path=self.runtime_path)
        postgres_snapshots = PostgresPlatformSnapshotStore(
            self.dsn,
            compatibility_path=self.runtime_path,
        )
        postgres_templates = PostgresTemplateMarketStore(
            self.dsn,
            compatibility_path=self.runtime_path,
        )
        postgres_remediation = PostgresRemediationStore(
            self.dsn,
            compatibility_path=self.runtime_path,
        )

        self.assertEqual(postgres_runs.get_run("run_source").job_name, "postgres migration proof")
        self.assertEqual(
            postgres_runs.list_events("run_source")[-1].event_type,
            "migration.proof",
        )
        latest_snapshot = postgres_snapshots.latest(owner="alice")
        self.assertIsNotNone(latest_snapshot)
        assert latest_snapshot is not None
        self.assertEqual(latest_snapshot.snapshot_id, "snapshot_source")
        self.assertEqual(
            postgres_templates.get_draft("draft_source", owner="alice").template_id,
            "template_source",
        )
        self.assertEqual(
            postgres_remediation.get_session("session_source").state,
            RemediationState.WAITING_EVIDENCE,
        )
        self.assertEqual(
            PostgresControlRepository(self.dsn).get_outbox("migration-control-message").payload,
            {"run_id": "run_source"},
        )

        rerun = migrate_sqlite_domain_to_postgres(
            sqlite_path=source_path,
            postgres_dsn=self.dsn,
            source_quiesced=True,
        )
        self.assertTrue(rerun.already_complete)

    def test_api_readiness_uses_portable_schema_checks(self) -> None:
        evidence_root = Path(self._temporary.name) / "evidence"
        evidence_root.mkdir()
        service = ApiHealthService(
            store=PostgresRunStore(self.dsn, compatibility_path=self.runtime_path),
            evidence_root=evidence_root,
            platform_snapshot_store=PostgresPlatformSnapshotStore(
                self.dsn,
                compatibility_path=self.runtime_path,
            ),
            user_entitlement_store=PostgresUserEntitlementStore(
                self.dsn,
                compatibility_path=self.runtime_path,
            ),
            submission_enabled=False,
            llm_enabled=False,
        )

        ready, payload = service.ready()

        self.assertTrue(ready)
        checks = {item["name"]: item for item in payload["checks"]}
        self.assertEqual(checks["database"]["status"], "ok")
        self.assertEqual(checks["platform_snapshot_store"]["status"], "ok")
        self.assertEqual(checks["user_entitlement_store"]["status"], "ok")

    def test_evidence_seal_claim_is_exclusive_and_expiry_takeover_is_fenced(self) -> None:
        store = PostgresRunStore(self.dsn, compatibility_path=self.runtime_path)
        run = store.create_run(
            run_id="run_pg_evidence_seal_claim",
            owner="alice",
            workdir="/public/home/alice/project",
            script="echo sealed",
        )
        with store.connect() as conn:
            conn.execute(
                "UPDATE runs SET state = ?, collection_state = ? WHERE run_id = ?",
                ("succeeded", "succeeded", run.run_id),
            )
        first = store.begin_evidence_seal(
            run.run_id,
            claim_owner="pg-sealer-a",
            lease_seconds=300,
        )

        with self.assertRaises(EvidenceSealClaimConflict):
            store.begin_evidence_seal(
                run.run_id,
                claim_owner="pg-sealer-b",
                lease_seconds=300,
            )
        with store.connect() as conn:
            conn.execute(
                "UPDATE runs SET evidence_seal_lease_expires_at = ? WHERE run_id = ?",
                ("2000-01-01T00:00:00+00:00", run.run_id),
            )
        takeover = store.begin_evidence_seal(
            run.run_id,
            claim_owner="pg-sealer-b",
            lease_seconds=300,
        )

        self.assertGreater(takeover.fencing_token, first.fencing_token)

    def test_migration_cli_imports_and_verifies_a_quiesced_source(self) -> None:
        source_path = Path(self._temporary.name) / "cli-source.db"
        RunStore(source_path).create_run(
            run_id="run_cli",
            owner="alice",
            workdir="/public/home/alice/project",
            script="echo cli",
        )
        root = Path(__file__).resolve().parents[1]
        command = [
            sys.executable,
            str(root / "scripts" / "migrate-sqlite-domain-to-postgres.py"),
            "--sqlite-db",
            str(source_path),
        ]
        environment = dict(os.environ)
        environment["PILOT107_POSTGRES_DSN"] = self.dsn
        environment["PYTHONPATH"] = str(root / "src")

        imported = subprocess.run(
            [*command, "--source-quiesced"],
            cwd=root,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(imported.returncode, 0, imported.stderr)
        self.assertTrue(json.loads(imported.stdout)["transferred"])
        verified = subprocess.run(
            [*command, "--verify-only"],
            cwd=root,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(verified.returncode, 0, verified.stderr)
        self.assertTrue(json.loads(verified.stdout)["already_complete"])


if __name__ == "__main__":
    unittest.main()
