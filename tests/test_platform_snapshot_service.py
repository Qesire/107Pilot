import tempfile
import unittest
from pathlib import Path

from pilot107.core.platform_snapshot import (
    CommandObservation,
    NormalizedNodeState,
    ObservationSourceType,
    PlatformDefault,
    PlatformSnapshotScope,
)
from pilot107.core.platform_snapshot_store import (
    PlatformSnapshotStore,
    SnapshotCollectionStatus,
)
from pilot107.services.platform_snapshot_service import (
    PlatformSnapshotService,
    build_login_snapshot_from_observations,
    redact_command_results,
)


class PlatformSnapshotServiceTests(unittest.TestCase):
    def test_builds_snapshot_from_redacted_cli_observations(self) -> None:
        observations = (
            CommandObservation(
                name="scontrol_show_part",
                argv=("scontrol", "show", "part"),
                returncode=0,
                stdout=(
                    "PartitionName=Students AllowAccounts=ALL "
                    "AllowQos=qos_stu_default MaxTime=04:00:00 State=UP "
                    "Nodes=anode[05-17] TRES=cpu=1664,mem=7500G,gres/gpu=104\n"
                ),
                stderr="",
            ),
            CommandObservation(
                name="scontrol_show_nodes",
                argv=("scontrol", "show", "nodes"),
                returncode=0,
                stdout=(
                    "NodeName=anode16 CPUAlloc=1 CPUTot=32 RealMemory=8192 "
                    "Gres=gpu:A100:2 Partitions=Students State=MIXED\n"
                ),
                stderr="",
            ),
            CommandObservation(
                name="squeue_user_pipe",
                argv=("squeue", "-h", "-u", "alice", "-o", "%i|%T|%R|%P|%j"),
                returncode=0,
                stdout="21039|PENDING|Resources|Students|pilot107-probe\n",
                stderr="",
            ),
        )

        snapshot = build_login_snapshot_from_observations(
            command_results=observations,
            captured_at="2026-07-15T00:00:00+00:00",
            snapshot_id="snapshot-test",
        )

        self.assertEqual(snapshot.snapshot_id, "snapshot-test")
        self.assertEqual(snapshot.partitions[0].name, "Students")
        self.assertEqual(snapshot.partitions[0].tres["gres/gpu"], "104")
        self.assertEqual(snapshot.partitions[0].captured_at, "2026-07-15T00:00:00+00:00")
        self.assertEqual(snapshot.nodes[0].state_normalized, NormalizedNodeState.MIXED)
        self.assertEqual(snapshot.squeue_jobs[0].pending_reason, "Resources")
        self.assertEqual(snapshot.squeue_jobs[0].raw_artifact, "raw/squeue.txt")

    def test_redacts_username_and_home_from_command_outputs(self) -> None:
        observations = (
            CommandObservation(
                name="pwd",
                argv=("cat", "/public/home/alice/project", "alice"),
                returncode=0,
                stdout="/public/home/alice/project\n",
                stderr="alice warning\n",
            ),
        )

        redacted, report = redact_command_results(
            observations,
            username="alice",
            home="/public/home/alice",
        )

        self.assertEqual(redacted[0].stdout, "<home>/project\n")
        self.assertEqual(redacted[0].stderr, "<user> warning\n")
        self.assertEqual(redacted[0].argv, ("cat", "<home>/project", "<user>"))
        self.assertEqual(report, ("pwd: username/home redacted",))

    def test_login_node_gpu_runtime_limitation_does_not_disable_gpu_partitions(self) -> None:
        observations = (
            CommandObservation(
                name="scontrol_show_part",
                argv=("scontrol", "show", "part"),
                returncode=0,
                stdout=(
                    "PartitionName=GPU-A100 AllowAccounts=ALL AllowQos=qos_gpu "
                    "MaxTime=04:00:00 State=UP Nodes=anode16 TRES=cpu=32,gres/gpu=2\n"
                ),
                stderr="",
            ),
        )

        snapshot = build_login_snapshot_from_observations(
            command_results=observations,
            captured_at="2026-07-15T00:00:00+00:00",
        )
        payload = snapshot.to_payload()

        self.assertEqual(snapshot.partitions[0].name, "GPU-A100")
        self.assertEqual(snapshot.partitions[0].state_raw, "UP")
        self.assertEqual(payload["runtime_limitations"][0]["name"], "gpu_runtime")
        self.assertEqual(payload["runtime_limitations"][0]["availability"], "unsupported")
        self.assertIn("not evidence", str(payload["runtime_limitations"][0]["warning"]))
        self.assertNotEqual(snapshot.partitions[0].state_raw, "UNAVAILABLE")

    def test_defaults_can_preserve_docs_competition_and_user_choices(self) -> None:
        defaults = (
            PlatformDefault(
                name="docs_default",
                partition="Students",
                qos="qos_stu_default",
                source_type=ObservationSourceType.OFFICIAL_DOCS,
                source_name="docs-main",
            ),
            PlatformDefault(
                name="competition_carrier_default",
                partition="Students",
                qos="qos_stu_medium_2gpu",
                source_type=ObservationSourceType.SIMULATOR,
                source_name="simulator-real107-behavior",
            ),
            PlatformDefault(
                name="user_selected_default",
                partition="GPU-A100",
                qos="qos_gpu",
                source_type=ObservationSourceType.CLI,
                source_name="user snapshot",
            ),
        )

        snapshot = build_login_snapshot_from_observations(
            command_results=(),
            captured_at="2026-07-15T00:00:00+00:00",
            defaults=defaults,
        )
        payload_defaults = {item["name"]: item for item in snapshot.to_payload()["defaults"]}

        self.assertEqual(payload_defaults["docs_default"]["qos"], "qos_stu_default")
        self.assertEqual(
            payload_defaults["competition_carrier_default"]["qos"],
            "qos_stu_medium_2gpu",
        )
        self.assertEqual(payload_defaults["user_selected_default"]["partition"], "GPU-A100")

    def test_collects_and_persists_partial_snapshot_with_ttl(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = PlatformSnapshotStore(Path(temporary) / "pilot107.db")
            service = PlatformSnapshotService(collector=FakeCollector())

            record = service.collect_and_store_login_snapshot(
                store=store,
                owner="alice",
                username="alice",
                home="/public/home/alice",
                source_type=ObservationSourceType.SIMULATOR,
                source_name="docker-sim",
                ttl_seconds=600,
                captured_at="2026-07-15T08:00:00+08:00",
                snapshot_id="snapshot_ingested",
            )

        self.assertEqual(record.scope, PlatformSnapshotScope.LOGIN_NODE)
        self.assertEqual(record.captured_at, "2026-07-15T00:00:00+00:00")
        self.assertEqual(record.expires_at, "2026-07-15T00:10:00+00:00")
        self.assertEqual(record.collection_status, SnapshotCollectionStatus.PARTIAL)
        self.assertEqual(record.source_type, ObservationSourceType.SIMULATOR)
        conda = next(
            item
            for item in record.payload["command_results"]
            if item["name"] == "conda_env_list_json"
        )
        self.assertEqual(conda["returncode"], 127)


class FakeCollector:
    def collect(self, specs):
        return tuple(
            CommandObservation(
                name=spec.name.value,
                argv=spec.argv,
                returncode=127 if spec.name.value == "conda_env_list_json" else 0,
                stdout="",
                stderr=("command unavailable" if spec.name.value == "conda_env_list_json" else ""),
            )
            for spec in specs
        )


if __name__ == "__main__":
    unittest.main()
