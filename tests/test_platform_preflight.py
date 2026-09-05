import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from pilot107.core.platform_preflight import (
    validate_platform_snapshot_resource_plan,
    validate_user_entitlement_resource_plan,
)
from pilot107.core.platform_snapshot import (
    ObservationSourceType,
    PartitionSnapshot,
    PlatformSnapshot,
    PlatformSnapshotScope,
)
from pilot107.core.platform_snapshot_store import (
    PlatformSnapshotRecord,
    PlatformSnapshotStore,
)
from pilot107.core.resources import PreflightSeverity, ResourcePlan
from pilot107.core.user_entitlement import (
    EntitlementDataQuality,
    UserAssociation,
    UserEntitlementSnapshot,
)
from pilot107.core.user_entitlement_store import (
    UserEntitlementRecord,
    UserEntitlementStore,
)


class PlatformPreflightTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.store = PlatformSnapshotStore(Path(self._temporary.name) / "pilot107.db")
        self.entitlement_store = UserEntitlementStore(Path(self._temporary.name) / "pilot107.db")
        self.plan = ResourcePlan(
            partition="Students",
            qos="qos_stu_default",
            nodes=1,
            ntasks=1,
            cpus_per_task=1,
            time_limit="00:10:00",
        )

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def test_stale_snapshot_is_unknown_and_not_used_as_authorization(self) -> None:
        record = self._snapshot(
            expires_at="2026-07-15T00:05:00+00:00",
            partitions=(
                PartitionSnapshot(
                    name="Students",
                    state_raw="UP",
                    allow_qos=("qos_stu_default",),
                ),
            ),
        )

        findings = validate_platform_snapshot_resource_plan(
            self.plan,
            record,
            at=datetime(2026, 7, 15, 0, 10, tzinfo=UTC),
        )

        self.assertEqual([item.code for item in findings], ["PLATFORM.SNAPSHOT_STALE"])
        self.assertEqual(findings[0].severity, PreflightSeverity.UNKNOWN)

    def test_fresh_snapshot_warns_without_claiming_entitlement_authority(self) -> None:
        record = self._snapshot(
            expires_at="2026-07-15T01:00:00+00:00",
            partitions=(
                PartitionSnapshot(
                    name="Students",
                    state_raw="DOWN",
                    allow_qos=("another_qos",),
                ),
            ),
        )

        findings = validate_platform_snapshot_resource_plan(
            self.plan,
            record,
            at=datetime(2026, 7, 15, 0, 10, tzinfo=UTC),
        )

        self.assertEqual(
            {item.code for item in findings},
            {"PLATFORM.PARTITION_NOT_UP", "PLATFORM.QOS_NOT_OBSERVED"},
        )
        self.assertTrue(all(item.severity == PreflightSeverity.WARN for item in findings))
        self.assertTrue(
            all(
                item.source_authority == "platform_snapshot:snapshot_preflight" for item in findings
            )
        )

    def test_missing_snapshot_is_explicitly_unknown(self) -> None:
        findings = validate_platform_snapshot_resource_plan(self.plan, None)

        self.assertEqual(findings[0].code, "PLATFORM.SNAPSHOT_UNAVAILABLE")
        self.assertEqual(findings[0].severity, PreflightSeverity.UNKNOWN)

    def test_fresh_authoritative_entitlement_blocks_ungranted_qos(self) -> None:
        entitlement = self._entitlement(qos=("normal",))

        findings = validate_user_entitlement_resource_plan(
            self.plan,
            entitlement,
            at=datetime(2026, 7, 15, 0, 1, tzinfo=UTC),
        )

        self.assertEqual([item.code for item in findings], ["ENTITLEMENT.QOS_NOT_ALLOWED"])
        self.assertEqual(findings[0].severity, PreflightSeverity.BLOCK)

    def test_fresh_authoritative_entitlement_confirms_qos(self) -> None:
        entitlement = self._entitlement(qos=("qos_stu_default",))

        findings = validate_user_entitlement_resource_plan(
            self.plan,
            entitlement,
            at=datetime(2026, 7, 15, 0, 1, tzinfo=UTC),
        )

        self.assertEqual([item.code for item in findings], ["ENTITLEMENT.QOS_CONFIRMED"])
        self.assertEqual(findings[0].severity, PreflightSeverity.INFO)

    def test_permission_denied_entitlement_is_unknown_not_block(self) -> None:
        entitlement = self._entitlement(
            qos=(),
            quality=EntitlementDataQuality.PERMISSION_DENIED,
        )

        findings = validate_user_entitlement_resource_plan(
            self.plan,
            entitlement,
            at=datetime(2026, 7, 15, 0, 1, tzinfo=UTC),
        )

        self.assertEqual(
            [item.code for item in findings],
            ["ENTITLEMENT.DATA_QUALITY_INSUFFICIENT"],
        )
        self.assertEqual(findings[0].severity, PreflightSeverity.UNKNOWN)

    def test_entitlement_only_authorizes_default_account_associations(self) -> None:
        entitlement = self.entitlement_store.create(
            owner="alice",
            snapshot=UserEntitlementSnapshot(
                snapshot_id="entitlement_multi_account",
                captured_at="2026-07-15T00:00:00+00:00",
                collector_version="test.v1",
                data_quality=EntitlementDataQuality.AUTHORITATIVE,
                default_account="students",
                associations=(
                    UserAssociation(
                        account="students",
                        partition=None,
                        qos=("normal",),
                        default_qos="normal",
                    ),
                    UserAssociation(
                        account="research",
                        partition=None,
                        qos=("qos_stu_default",),
                        default_qos="qos_stu_default",
                    ),
                ),
            ),
            source_type=ObservationSourceType.SIMULATOR,
            source_name="docker-sim",
            expires_at="2026-07-15T00:05:00+00:00",
        )

        findings = validate_user_entitlement_resource_plan(
            self.plan,
            entitlement,
            at=datetime(2026, 7, 15, 0, 1, tzinfo=UTC),
        )

        self.assertEqual([item.code for item in findings], ["ENTITLEMENT.QOS_NOT_ALLOWED"])

    def test_missing_default_account_cannot_authorize(self) -> None:
        entitlement = self.entitlement_store.create(
            owner="alice",
            snapshot=UserEntitlementSnapshot(
                snapshot_id="entitlement_no_default_account",
                captured_at="2026-07-15T00:00:00+00:00",
                collector_version="test.v1",
                data_quality=EntitlementDataQuality.AUTHORITATIVE,
                associations=(),
            ),
            source_type=ObservationSourceType.SIMULATOR,
            source_name="docker-sim",
            expires_at="2026-07-15T00:05:00+00:00",
        )

        findings = validate_user_entitlement_resource_plan(
            self.plan,
            entitlement,
            at=datetime(2026, 7, 15, 0, 1, tzinfo=UTC),
        )

        self.assertEqual(
            [item.code for item in findings],
            ["ENTITLEMENT.DEFAULT_ACCOUNT_UNAVAILABLE"],
        )
        self.assertEqual(findings[0].severity, PreflightSeverity.UNKNOWN)

    def _snapshot(
        self,
        *,
        expires_at: str,
        partitions: tuple[PartitionSnapshot, ...],
    ) -> PlatformSnapshotRecord:
        return self.store.create(
            owner="alice",
            snapshot=PlatformSnapshot(
                snapshot_id="snapshot_preflight",
                scope=PlatformSnapshotScope.LOGIN_NODE,
                captured_at="2026-07-15T00:00:00+00:00",
                collector_version="test.v1",
                partitions=partitions,
            ),
            source_type=ObservationSourceType.SIMULATOR,
            source_name="docker-sim",
            expires_at=expires_at,
        )

    def _entitlement(
        self,
        *,
        qos: tuple[str, ...],
        quality: EntitlementDataQuality = EntitlementDataQuality.AUTHORITATIVE,
    ) -> UserEntitlementRecord:
        return self.entitlement_store.create(
            owner="alice",
            snapshot=UserEntitlementSnapshot(
                snapshot_id="entitlement_preflight",
                captured_at="2026-07-15T00:00:00+00:00",
                collector_version="test.v1",
                data_quality=quality,
                default_account="students",
                associations=(
                    UserAssociation(
                        account="students",
                        partition=None,
                        qos=qos,
                        default_qos=qos[0] if qos else None,
                    ),
                ),
            ),
            source_type=ObservationSourceType.SIMULATOR,
            source_name="docker-sim",
            expires_at="2026-07-15T00:05:00+00:00",
        )


if __name__ == "__main__":
    unittest.main()
