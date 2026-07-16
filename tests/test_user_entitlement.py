import sqlite3
import tempfile
import unittest
from pathlib import Path

from pilot107.core.platform_snapshot import CommandObservation, ObservationSourceType
from pilot107.core.platform_snapshot_store import SnapshotFreshness
from pilot107.core.user_entitlement import EntitlementDataQuality
from pilot107.core.user_entitlement_store import (
    UserEntitlementStore,
    UserEntitlementStoreError,
)
from pilot107.services.user_entitlement_service import UserEntitlementService


class UserEntitlementTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.store = UserEntitlementStore(Path(self._temporary.name) / "pilot107.db")

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def test_collects_redacts_and_persists_authoritative_association(self) -> None:
        service = UserEntitlementService(
            collector=FakeCollector(
                stdout=(
                    "alice|students|students||normal,qos_stu_default,qos_stu_medium_2gpu|"
                    "qos_stu_medium_2gpu\n"
                )
            )
        )

        record = service.collect_and_store(
            store=self.store,
            owner="alice",
            username="alice",
            source_type=ObservationSourceType.SIMULATOR,
            source_name="docker-sim",
            ttl_seconds=300,
            captured_at="2026-07-15T08:00:00+08:00",
            snapshot_id="entitlement_test",
        )

        self.assertEqual(record.data_quality, EntitlementDataQuality.AUTHORITATIVE)
        self.assertEqual(record.captured_at, "2026-07-15T00:00:00+00:00")
        self.assertEqual(record.expires_at, "2026-07-15T00:05:00+00:00")
        self.assertEqual(record.payload["default_account"], "students")
        association = record.payload["associations"][0]
        self.assertEqual(association["account"], "students")
        self.assertIn("qos_stu_medium_2gpu", association["qos"])
        encoded = str(record.payload)
        self.assertNotIn("alice", encoded)
        safe_command = record.safe_payload()["snapshot"]["command_results"][0]
        self.assertNotIn("argv", safe_command)
        self.assertNotIn("stdout", safe_command)
        self.assertNotIn("stderr", safe_command)

    def test_permission_denied_is_not_authoritative(self) -> None:
        snapshot = UserEntitlementService(
            collector=FakeCollector(returncode=1, stderr="Access denied for alice")
        ).collect(
            username="alice",
            captured_at="2026-07-15T00:00:00+00:00",
            snapshot_id="entitlement_denied",
        )

        self.assertEqual(snapshot.data_quality, EntitlementDataQuality.PERMISSION_DENIED)
        self.assertEqual(snapshot.associations, ())
        self.assertNotIn("alice", snapshot.command_results[0].stderr)

    def test_store_rejects_cross_owner_entitlement_collection(self) -> None:
        service = UserEntitlementService(
            collector=FakeCollector(stdout="bob|students|students||normal|normal\n")
        )

        with self.assertRaisesRegex(ValueError, "owner and queried username must match"):
            service.collect_and_store(
                store=self.store,
                owner="alice",
                username="bob",
                source_type=ObservationSourceType.CLI,
                source_name="login-node",
            )

    def test_mismatched_user_row_is_partial(self) -> None:
        snapshot = UserEntitlementService(
            collector=FakeCollector(stdout="bob|students|students||normal|normal\n")
        ).collect(
            username="alice",
            captured_at="2026-07-15T00:00:00+00:00",
        )

        self.assertEqual(snapshot.data_quality, EntitlementDataQuality.PARTIAL)
        self.assertEqual(snapshot.associations, ())
        self.assertIn("mismatched user", snapshot.limitations[0])

    def test_owner_latest_freshness_and_idempotency(self) -> None:
        service = UserEntitlementService(
            collector=FakeCollector(stdout="alice|students|students||normal|normal\n")
        )
        first = service.collect_and_store(
            store=self.store,
            owner="alice",
            username="alice",
            source_type=ObservationSourceType.CLI,
            source_name="login-node",
            captured_at="2026-07-15T00:00:00+00:00",
            snapshot_id="entitlement_same",
        )
        repeated = service.collect_and_store(
            store=self.store,
            owner="alice",
            username="alice",
            source_type=ObservationSourceType.CLI,
            source_name="login-node",
            captured_at="2026-07-15T00:00:00+00:00",
            snapshot_id="entitlement_same",
        )

        self.assertEqual(first.content_sha256, repeated.content_sha256)
        self.assertEqual(self.store.latest(owner="alice").snapshot_id, "entitlement_same")
        self.assertEqual(first.freshness(), SnapshotFreshness.STALE)
        with self.assertRaises(KeyError):
            self.store.get("entitlement_same", owner="bob")

        conflict_service = UserEntitlementService(
            collector=FakeCollector(stdout="alice|other|other||normal|normal\n")
        )
        with self.assertRaises(UserEntitlementStoreError) as raised:
            conflict_service.collect_and_store(
                store=self.store,
                owner="alice",
                username="alice",
                source_type=ObservationSourceType.CLI,
                source_name="login-node",
                captured_at="2026-07-15T00:00:00+00:00",
                snapshot_id="entitlement_same",
            )
        self.assertEqual(raised.exception.code, "USER_ENTITLEMENT.IDEMPOTENCY_CONFLICT")

    def test_store_applies_global_platform_migrations_in_order(self) -> None:
        with sqlite3.connect(self.store.db_path) as conn:
            rows = conn.execute(
                "SELECT migration_id, checksum FROM schema_migrations "
                "ORDER BY migration_id"
            ).fetchall()
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }

        self.assertEqual(
            rows,
            [
                (
                    "003b.001.platform_snapshots",
                    "f9514d2d4a312490fa010f8b188be67e084a7f8cffec937ed23186adee415d02",
                ),
                (
                    "003b.002.user_entitlement_snapshots",
                    "538acfdaa3e6be2140760dc1d7567c17f1844dfe43b846e021b00fe2899f8356",
                ),
            ],
        )
        self.assertIn("platform_snapshots", tables)
        self.assertIn("user_entitlement_snapshots", tables)


class FakeCollector:
    def __init__(
        self,
        *,
        stdout: str = "",
        stderr: str = "",
        returncode: int = 0,
    ) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode

    def collect(self, specs):
        spec = specs[0]
        return (
            CommandObservation(
                name=spec.name.value,
                argv=spec.argv,
                returncode=self.returncode,
                stdout=self.stdout,
                stderr=self.stderr,
            ),
        )


if __name__ == "__main__":
    unittest.main()
