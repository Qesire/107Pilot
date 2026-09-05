from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

from pilot107.core.recovery import (
    PgToolsBackupAdapter,
    RecoveryError,
    create_control_plane_backup,
    restore_control_plane_backup,
    verify_control_plane_backup,
)
from pilot107.core.run_store import RunStore


class FakePostgresAdapter:
    def __init__(self) -> None:
        self.dump_dsns: list[str] = []
        self.restore_dsns: list[str] = []

    def dump(self, *, dsn: str, destination: Path) -> None:
        self.dump_dsns.append(dsn)
        destination.write_bytes(b"fake-postgres-custom-dump")

    def restore(self, *, dsn: str, source: Path) -> None:
        self.restore_dsns.append(dsn)
        if source.read_bytes() != b"fake-postgres-custom-dump":
            raise AssertionError("unexpected PostgreSQL dump")


class ControlPlaneRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.db_path = self.root / "runtime" / "pilot107.db"
        store = RunStore(self.db_path)
        store.create_run(
            run_id="run_backup",
            owner="alice",
            workdir="/public/home/alice",
            script="#!/bin/bash\necho backup\n",
        )
        self.evidence = self.root / "runtime" / "evidence"
        self.capsules = self.root / "runtime" / "capsules"
        (self.evidence / "runs" / "run_backup" / "logs").mkdir(parents=True)
        (self.evidence / "runs" / "run_backup" / "logs" / "stdout.txt").write_text(
            "backup evidence\n",
            encoding="utf-8",
        )
        (self.capsules / "runs" / "run_backup" / "raw").mkdir(parents=True)
        (self.capsules / "runs" / "run_backup" / "raw" / "capsule.json").write_text(
            '{"ok":true}\n',
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_round_trip_restores_sqlite_evidence_and_capsules_to_empty_root(self) -> None:
        backup = self.root / "backups" / "backup-one"
        result = create_control_plane_backup(
            destination=backup,
            sqlite_db=self.db_path,
            evidence_root=self.evidence,
            capsule_root=self.capsules,
            quiesced=True,
        )

        manifest = verify_control_plane_backup(backup)
        restore_root = self.root / "restored"
        restore_root.mkdir()
        restored = restore_control_plane_backup(
            backup_root=backup,
            destination=restore_root,
            quiesced=True,
        )

        self.assertEqual(restored.backup_id, result.backup_id)
        self.assertFalse(restored.postgres_restored)
        self.assertEqual(manifest["backup_id"], result.backup_id)
        with sqlite3.connect(restore_root / "pilot107.db") as conn:
            row = conn.execute(
                "SELECT owner FROM runs WHERE run_id = 'run_backup'"
            ).fetchone()
        self.assertEqual(row, ("alice",))
        self.assertEqual(
            (restore_root / "evidence/runs/run_backup/logs/stdout.txt").read_text(),
            "backup evidence\n",
        )
        self.assertTrue(
            (restore_root / "capsules/runs/run_backup/raw/capsule.json").is_file()
        )

    def test_sqlite_backup_includes_committed_wal_state(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                "INSERT INTO run_events (run_id,event_type,payload_json,created_at) "
                "VALUES ('run_backup','test.wal','{}','2026-07-18T00:00:00+00:00')"
            )
        backup = self.root / "wal-backup"

        create_control_plane_backup(
            destination=backup,
            sqlite_db=self.db_path,
            quiesced=True,
        )

        with sqlite3.connect(backup / "payload/sqlite/pilot107.db") as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM run_events WHERE event_type = 'test.wal'"
            ).fetchone()
        self.assertEqual(count, (1,))

    def test_tampered_payload_fails_digest_verification(self) -> None:
        backup = self.root / "tampered-backup"
        create_control_plane_backup(
            destination=backup,
            sqlite_db=self.db_path,
            evidence_root=self.evidence,
            quiesced=True,
        )
        target = backup / "payload/evidence/runs/run_backup/logs/stdout.txt"
        target.write_text("tampered\n", encoding="utf-8")

        with self.assertRaisesRegex(RecoveryError, "mismatch"):
            verify_control_plane_backup(backup)

    def test_unsafe_manifest_path_fails_before_restore(self) -> None:
        backup = self.root / "unsafe-backup"
        create_control_plane_backup(
            destination=backup,
            sqlite_db=self.db_path,
            quiesced=True,
        )
        manifest_path = backup / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["files"][0]["path"] = "payload/../../outside"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        with self.assertRaisesRegex(RecoveryError, "unsafe"):
            verify_control_plane_backup(backup)

    def test_normalized_empty_manifest_path_fails_closed(self) -> None:
        backup = self.root / "empty-path-backup"
        create_control_plane_backup(
            destination=backup,
            sqlite_db=self.db_path,
            quiesced=True,
        )
        manifest_path = backup / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["files"][0]["path"] = "."
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        with self.assertRaisesRegex(RecoveryError, "unsafe"):
            verify_control_plane_backup(backup)

    def test_payload_root_symlink_is_rejected(self) -> None:
        backup = self.root / "payload-symlink-backup"
        create_control_plane_backup(
            destination=backup,
            sqlite_db=self.db_path,
            quiesced=True,
        )
        external_payload = self.root / "external-payload"
        shutil.copytree(backup / "payload", external_payload)
        shutil.rmtree(backup / "payload")
        (backup / "payload").symlink_to(external_payload, target_is_directory=True)

        with self.assertRaisesRegex(RecoveryError, "payload root"):
            verify_control_plane_backup(backup)

    def test_nonempty_restore_destination_is_rejected_without_changes(self) -> None:
        backup = self.root / "backup-nonempty"
        create_control_plane_backup(
            destination=backup,
            sqlite_db=self.db_path,
            quiesced=True,
        )
        destination = self.root / "nonempty"
        destination.mkdir()
        marker = destination / "keep.txt"
        marker.write_text("keep\n", encoding="utf-8")

        with self.assertRaisesRegex(RecoveryError, "not empty"):
            restore_control_plane_backup(
                backup_root=backup,
                destination=destination,
                quiesced=True,
            )

        self.assertEqual(marker.read_text(encoding="utf-8"), "keep\n")

    def test_symlink_in_evidence_tree_is_rejected(self) -> None:
        (self.evidence / "unsafe-link").symlink_to(self.db_path)

        with self.assertRaisesRegex(RecoveryError, "symlink"):
            create_control_plane_backup(
                destination=self.root / "symlink-backup",
                sqlite_db=self.db_path,
                evidence_root=self.evidence,
                quiesced=True,
            )

    def test_backup_and_restore_require_explicit_quiesce(self) -> None:
        backup = self.root / "quiesce-backup"
        with self.assertRaisesRegex(RecoveryError, "quiesced"):
            create_control_plane_backup(
                destination=backup,
                sqlite_db=self.db_path,
                quiesced=False,
            )
        create_control_plane_backup(
            destination=backup,
            sqlite_db=self.db_path,
            quiesced=True,
        )

        with self.assertRaisesRegex(RecoveryError, "quiesced"):
            restore_control_plane_backup(
                backup_root=backup,
                destination=self.root / "quiesce-restore",
                quiesced=False,
            )

    def test_backup_destination_inside_evidence_tree_is_rejected(self) -> None:
        with self.assertRaisesRegex(RecoveryError, "inside the Evidence"):
            create_control_plane_backup(
                destination=self.evidence / "recursive-backup",
                sqlite_db=self.db_path,
                evidence_root=self.evidence,
                quiesced=True,
            )

    def test_explicit_missing_component_root_is_rejected(self) -> None:
        with self.assertRaisesRegex(RecoveryError, "Evidence root"):
            create_control_plane_backup(
                destination=self.root / "missing-evidence-backup",
                sqlite_db=self.db_path,
                evidence_root=self.root / "missing-evidence",
                quiesced=True,
            )

    def test_symlink_destinations_are_rejected(self) -> None:
        dangling_backup = self.root / "dangling-backup"
        dangling_backup.symlink_to(self.root / "backup-target")
        with self.assertRaisesRegex(RecoveryError, "must not be a symlink"):
            create_control_plane_backup(
                destination=dangling_backup,
                sqlite_db=self.db_path,
                quiesced=True,
            )

        backup = self.root / "real-backup"
        create_control_plane_backup(
            destination=backup,
            sqlite_db=self.db_path,
            quiesced=True,
        )
        restore_target = self.root / "restore-target"
        restore_target.mkdir()
        restore_link = self.root / "restore-link"
        restore_link.symlink_to(restore_target, target_is_directory=True)
        with self.assertRaisesRegex(RecoveryError, "must not be a symlink"):
            restore_control_plane_backup(
                backup_root=backup,
                destination=restore_link,
                quiesced=True,
            )
        self.assertTrue(restore_target.is_dir())

    def test_postgres_adapter_is_explicit_and_dsn_is_not_stored(self) -> None:
        adapter = FakePostgresAdapter()
        dsn = "postgresql://secret-user:secret-password@db/control"
        backup = self.root / "postgres-backup"
        create_control_plane_backup(
            destination=backup,
            sqlite_db=self.db_path,
            postgres_dsn=dsn,
            postgres_adapter=adapter,
            quiesced=True,
        )
        manifest_text = (backup / "manifest.json").read_text(encoding="utf-8")
        self.assertNotIn("secret-user", manifest_text)
        self.assertNotIn("secret-password", manifest_text)

        with self.assertRaisesRegex(RecoveryError, "no restore DSN"):
            restore_control_plane_backup(
                backup_root=backup,
                destination=self.root / "missing-postgres-dsn",
                quiesced=True,
            )
        with self.assertRaisesRegex(RecoveryError, "postgres_allow_reset"):
            restore_control_plane_backup(
                backup_root=backup,
                destination=self.root / "missing-postgres-reset-confirmation",
                postgres_dsn=dsn,
                postgres_adapter=adapter,
                quiesced=True,
            )
        restored = restore_control_plane_backup(
            backup_root=backup,
            destination=self.root / "postgres-restored",
            postgres_dsn=dsn,
            postgres_adapter=adapter,
            postgres_allow_reset=True,
            quiesced=True,
        )

        self.assertTrue(restored.postgres_restored)
        self.assertEqual(adapter.dump_dsns, [dsn])
        self.assertEqual(adapter.restore_dsns, [dsn])

    def test_pg_tools_keep_dsn_out_of_argv_and_redact_failures(self) -> None:
        dsn = "postgresql://secret-user:secret-password@db/control"
        adapter = PgToolsBackupAdapter()
        completed = CompletedProcess(
            args=[],
            returncode=1,
            stdout="",
            stderr=f"connection failed for {dsn}",
        )

        with (
            patch(
                "pilot107.core.recovery.subprocess.run", return_value=completed
            ) as run,
            self.assertRaises(RecoveryError) as raised,
        ):
            adapter.dump(dsn=dsn, destination=self.root / "control.dump")

        argv = run.call_args.args[0]
        environment = run.call_args.kwargs["env"]
        self.assertNotIn(dsn, argv)
        self.assertEqual(environment["PGDATABASE"], dsn)
        self.assertNotIn(dsn, str(raised.exception))
        self.assertNotIn("secret-password", str(raised.exception))

    def test_cli_create_verify_restore_round_trip(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        script = repository / "scripts/control-plane-recovery.py"
        backup = self.root / "cli-backup"
        restored = self.root / "cli-restored"
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(repository / "src")

        created = subprocess.run(
            [
                sys.executable,
                str(script),
                "create",
                "--destination",
                str(backup),
                "--sqlite-db",
                str(self.db_path),
                "--evidence-root",
                str(self.evidence),
                "--capsule-root",
                str(self.capsules),
                "--quiesced",
            ],
            check=False,
            capture_output=True,
            env=environment,
            text=True,
        )
        self.assertEqual(created.returncode, 0, created.stderr)
        created_payload = json.loads(created.stdout)

        verified = subprocess.run(
            [
                sys.executable,
                str(script),
                "verify",
                "--backup-root",
                str(backup),
            ],
            check=False,
            capture_output=True,
            env=environment,
            text=True,
        )
        self.assertEqual(verified.returncode, 0, verified.stderr)
        verified_payload = json.loads(verified.stdout)

        restore = subprocess.run(
            [
                sys.executable,
                str(script),
                "restore",
                "--backup-root",
                str(backup),
                "--destination",
                str(restored),
                "--quiesced",
            ],
            check=False,
            capture_output=True,
            env=environment,
            text=True,
        )
        self.assertEqual(restore.returncode, 0, restore.stderr)
        restore_payload = json.loads(restore.stdout)

        self.assertEqual(created_payload["backup_id"], verified_payload["backup_id"])
        self.assertEqual(created_payload["backup_id"], restore_payload["backup_id"])
        self.assertTrue(verified_payload["verified"])
        self.assertTrue((restored / "pilot107.db").is_file())


if __name__ == "__main__":
    unittest.main()
