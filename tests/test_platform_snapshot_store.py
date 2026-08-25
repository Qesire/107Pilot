import sqlite3
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from pilot107.core.platform_snapshot import (
    CommandObservation,
    NodeSnapshot,
    ObservationSourceType,
    PartitionSnapshot,
    PlatformSnapshot,
    PlatformSnapshotScope,
)
from pilot107.core.platform_snapshot_store import (
    PlatformSnapshotStore,
    PlatformSnapshotStoreError,
    SnapshotCollectionStatus,
    SnapshotFreshness,
)
from pilot107.core.run_store import RunStore

_VM_SLURM_SOURCE_NAME = "vm-slurm"


class PlatformSnapshotStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "pilot107.db"
        self.store = PlatformSnapshotStore(self.db_path)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_persists_with_freshness_and_strips_command_output_from_safe_payload(self) -> None:
        record = self.store.create(
            owner="alice",
            snapshot=_snapshot("snapshot_fresh", "2026-07-15T00:00:00+00:00"),
            source_type=ObservationSourceType.SIMULATOR,
            source_name="docker-sim",
            expires_at="2026-07-15T02:00:00+00:00",
        )

        self.assertEqual(
            record.freshness(at=datetime(2026, 7, 15, 1, tzinfo=UTC)),
            SnapshotFreshness.FRESH,
        )
        self.assertEqual(record.collection_status, SnapshotCollectionStatus.COMPLETE)
        command = record.safe_payload()["snapshot"]["command_results"][0]
        self.assertNotIn("argv", command)
        self.assertNotIn("stdout", command)
        self.assertNotIn("stderr", command)

    def test_normalizes_timestamp_in_record_and_payload(self) -> None:
        record = self.store.create(
            owner="alice",
            snapshot=_snapshot("snapshot_offset", "2026-07-15T08:00:00+08:00"),
            source_type=ObservationSourceType.CLI,
            source_name="login-node",
            expires_at=None,
        )

        self.assertEqual(record.captured_at, "2026-07-15T00:00:00+00:00")
        self.assertEqual(record.payload["captured_at"], record.captured_at)

    def test_rejects_invalid_snapshot_id(self) -> None:
        with self.assertRaises(PlatformSnapshotStoreError) as raised:
            self.store.create(
                owner="alice",
                snapshot=_snapshot("../snapshot", "2026-07-15T00:00:00+00:00"),
                source_type=ObservationSourceType.CLI,
                source_name="login-node",
                expires_at=None,
            )

        self.assertEqual(raised.exception.code, "PLATFORM_SNAPSHOT.ID_INVALID")

    def test_owner_boundary_latest_and_keyset_page(self) -> None:
        for index in range(3):
            self.store.create(
                owner="alice",
                snapshot=_snapshot(
                    f"snapshot_alice_{index}",
                    f"2026-07-15T00:0{index}:00+00:00",
                ),
                source_type=ObservationSourceType.CLI,
                source_name="login-node",
                expires_at=None,
            )
        self.store.create(
            owner="bob",
            snapshot=_snapshot("snapshot_bob", "2026-07-15T00:10:00+00:00"),
            source_type=ObservationSourceType.CLI,
            source_name="login-node",
            expires_at=None,
        )

        first, cursor = self.store.list_page(owner="alice", limit=2)
        second, final_cursor = self.store.list_page(owner="alice", limit=2, cursor=cursor)

        self.assertEqual(
            [item.snapshot_id for item in first],
            ["snapshot_alice_2", "snapshot_alice_1"],
        )
        self.assertEqual([item.snapshot_id for item in second], ["snapshot_alice_0"])
        self.assertIsNone(final_cursor)
        self.assertEqual(self.store.latest(owner="alice").snapshot_id, "snapshot_alice_2")
        with self.assertRaises(KeyError):
            self.store.get("snapshot_bob", owner="alice")

    def test_filters_fresh_stale_and_unknown(self) -> None:
        at = datetime(2026, 7, 15, 1, tzinfo=UTC)
        for snapshot_id, expiry in (
            ("snapshot_fresh", "2026-07-15T02:00:00+00:00"),
            ("snapshot_stale", "2026-07-15T00:30:00+00:00"),
            ("snapshot_unknown", None),
        ):
            self.store.create(
                owner="alice",
                snapshot=_snapshot(snapshot_id, "2026-07-15T00:00:00+00:00"),
                source_type=ObservationSourceType.CLI,
                source_name="login-node",
                expires_at=expiry,
            )

        for freshness, expected in (
            (SnapshotFreshness.FRESH, "snapshot_fresh"),
            (SnapshotFreshness.STALE, "snapshot_stale"),
            (SnapshotFreshness.UNKNOWN, "snapshot_unknown"),
        ):
            items, _ = self.store.list_page(owner="alice", freshness=freshness, at=at)
            self.assertEqual([item.snapshot_id for item in items], [expected])

    def test_idempotency_rejects_same_id_with_different_content(self) -> None:
        kwargs = {
            "owner": "alice",
            "source_type": ObservationSourceType.CLI,
            "source_name": "login-node",
            "expires_at": None,
        }
        self.store.create(
            snapshot=_snapshot("snapshot_same", "2026-07-15T00:00:00+00:00"),
            **kwargs,
        )

        with self.assertRaises(PlatformSnapshotStoreError) as raised:
            self.store.create(
                snapshot=_snapshot("snapshot_same", "2026-07-15T00:01:00+00:00"),
                **kwargs,
            )
        self.assertEqual(raised.exception.code, "PLATFORM_SNAPSHOT.IDEMPOTENCY_CONFLICT")

    def test_latest_usable_keeps_partial_snapshot_with_healthy_slurm_capacity(self) -> None:
        healthy = self.store.create(
            owner="alice",
            snapshot=_slurm_snapshot(
                "snapshot_vm_healthy",
                "2026-07-15T00:00:00+00:00",
                conda_returncode=127,
            ),
            source_type=ObservationSourceType.CLI,
            source_name=_VM_SLURM_SOURCE_NAME,
            expires_at="2026-07-15T02:00:00+00:00",
        )
        self.store.create(
            owner="alice",
            snapshot=_snapshot("snapshot_vm_degraded", "2026-07-15T00:10:00+00:00"),
            source_type=ObservationSourceType.CLI,
            source_name=_VM_SLURM_SOURCE_NAME,
            expires_at="2026-07-15T02:10:00+00:00",
        )
        self.store.create(
            owner="alice",
            snapshot=_slurm_snapshot(
                "snapshot_rest_newest",
                "2026-07-15T00:20:00+00:00",
            ),
            source_type=ObservationSourceType.REST,
            source_name="slurm-rest-diagnostic",
            expires_at="2026-07-15T02:20:00+00:00",
        )

        selection = self.store.latest_usable(
            owner="alice",
            at=datetime(2026, 7, 15, 1, tzinfo=UTC),
        )

        self.assertIsNotNone(selection)
        assert selection is not None
        self.assertEqual(selection.record.snapshot_id, healthy.snapshot_id)
        self.assertEqual(selection.authority_id, _VM_SLURM_SOURCE_NAME)
        self.assertEqual(selection.warnings, ("partial_ancillary_facts",))

    def test_latest_usable_marks_last_healthy_snapshot_stale(self) -> None:
        self.store.create(
            owner="alice",
            snapshot=_slurm_snapshot(
                "snapshot_vm_stale",
                "2026-07-15T00:00:00+00:00",
            ),
            source_type=ObservationSourceType.CLI,
            source_name=_VM_SLURM_SOURCE_NAME,
            expires_at="2026-07-15T00:30:00+00:00",
        )

        selection = self.store.latest_usable(
            owner="alice",
            at=datetime(2026, 7, 15, 1, tzinfo=UTC),
        )

        self.assertIsNotNone(selection)
        assert selection is not None
        self.assertEqual(selection.record.snapshot_id, "snapshot_vm_stale")
        self.assertEqual(selection.warnings, ("stale_authoritative_snapshot",))

    def test_latest_usable_returns_none_without_healthy_slurm_capacity(self) -> None:
        self.store.create(
            owner="alice",
            snapshot=_snapshot("snapshot_vm_empty", "2026-07-15T00:00:00+00:00"),
            source_type=ObservationSourceType.CLI,
            source_name=_VM_SLURM_SOURCE_NAME,
            expires_at="2026-07-15T02:00:00+00:00",
        )

        self.assertIsNone(self.store.latest_usable(owner="alice"))

    def test_latest_usable_accepts_sinfo_as_the_node_probe_fallback(self) -> None:
        self.store.create(
            owner="alice",
            snapshot=_slurm_snapshot(
                "snapshot_vm_sinfo",
                "2026-07-15T00:00:00+00:00",
                node_probe_name="sinfo_pipe",
            ),
            source_type=ObservationSourceType.CLI,
            source_name=_VM_SLURM_SOURCE_NAME,
            expires_at="2026-07-15T02:00:00+00:00",
        )

        selection = self.store.latest_usable(owner="alice")

        self.assertIsNotNone(selection)
        assert selection is not None
        self.assertEqual(selection.record.snapshot_id, "snapshot_vm_sinfo")

    def test_migration_coexists_with_existing_run_database(self) -> None:
        run_store = RunStore(self.db_path)
        run_store.create_run(
            run_id="run_before_snapshot_migration",
            owner="alice",
            workdir="/public/home/alice",
            script="echo ok",
        )

        PlatformSnapshotStore(self.db_path)

        self.assertEqual(
            run_store.get_run("run_before_snapshot_migration").owner,
            "alice",
        )
        with sqlite3.connect(self.db_path) as conn:
            migration = conn.execute(
                "SELECT migration_id FROM schema_migrations"
            ).fetchone()
        self.assertEqual(migration[0], "003b.001.platform_snapshots")


def _snapshot(snapshot_id: str, captured_at: str) -> PlatformSnapshot:
    return PlatformSnapshot(
        snapshot_id=snapshot_id,
        scope=PlatformSnapshotScope.LOGIN_NODE,
        captured_at=captured_at,
        collector_version="test.v1",
        command_results=(
            CommandObservation(
                name="hostname",
                argv=("hostname",),
                returncode=0,
                stdout="anode16\n",
                stderr="",
            ),
        ),
    )


def _slurm_snapshot(
    snapshot_id: str,
    captured_at: str,
    *,
    conda_returncode: int = 0,
    node_probe_name: str = "scontrol_show_nodes",
) -> PlatformSnapshot:
    node_probe_argv = (
        ("sinfo", "-h", "-o", "%N|%P|%t|%c|%m|%G|%E")
        if node_probe_name == "sinfo_pipe"
        else ("scontrol", "show", "nodes")
    )
    command_results = (
        CommandObservation(
            name="scontrol_show_part",
            argv=("scontrol", "show", "part"),
            returncode=0,
            stdout="PartitionName=CPU-RC State=UP Nodes=worker-1\n",
            stderr="",
        ),
        CommandObservation(
            name=node_probe_name,
            argv=node_probe_argv,
            returncode=0,
            stdout="NodeName=worker-1 CPUTot=6 RealMemory=10240 State=IDLE\n",
            stderr="",
        ),
        CommandObservation(
            name="conda_env_list_json",
            argv=("conda", "env", "list", "--json"),
            returncode=conda_returncode,
            stdout="{\"envs\": []}" if conda_returncode == 0 else "",
            stderr="" if conda_returncode == 0 else "command unavailable",
        ),
    )
    return PlatformSnapshot(
        snapshot_id=snapshot_id,
        scope=PlatformSnapshotScope.LOGIN_NODE,
        captured_at=captured_at,
        collector_version="test.v1",
        command_results=command_results,
        partitions=(
            PartitionSnapshot(
                name="CPU-RC",
                nodes="worker-1",
                total_cpus=6,
                total_nodes=1,
            ),
        ),
        nodes=(
            NodeSnapshot(
                node_name="worker-1",
                partitions=("CPU-RC",),
                cpus_total=6,
                memory_mb=10_240,
            ),
        ),
        limitations=(
            ("conda env list unavailable",) if conda_returncode != 0 else ()
        ),
    )


if __name__ == "__main__":
    unittest.main()
