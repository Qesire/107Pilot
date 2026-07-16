import json
import tempfile
import unittest
from pathlib import Path

from pilot107.core.platform_snapshot import ObservationSourceType, PlatformSnapshotScope
from pilot107.core.platform_snapshot_store import PlatformSnapshotStore, SnapshotCollectionStatus
from pilot107.services.platform_compute_probe import (
    PROBE_SCHEMA,
    compute_runtime_probe_script,
    parse_compute_runtime_probe_output,
    store_compute_runtime_probe_output,
)


class PlatformComputeProbeTests(unittest.TestCase):
    def test_script_is_static_and_uses_fixed_nvidia_argv(self) -> None:
        first = compute_runtime_probe_script()
        second = compute_runtime_probe_script()

        self.assertEqual(first, second)
        self.assertIn("--query-gpu=name,driver_version,memory.total", first)
        self.assertNotIn("$USER", first)

    def test_parses_unavailable_gpu_runtime_without_failing_job(self) -> None:
        snapshot = parse_compute_runtime_probe_output(
            _output(nvidia_returncode=127, cuda_available=False),
            job_id="42",
        )

        self.assertEqual(snapshot.scope, PlatformSnapshotScope.COMPUTE_JOB)
        self.assertEqual(snapshot.command_results[0].returncode, 127)
        self.assertEqual(snapshot.runtime_limitations[0].availability, "unavailable")
        self.assertTrue(snapshot.limitations)

    def test_rejects_changed_probe_command(self) -> None:
        payload = _payload(nvidia_returncode=0, cuda_available=True)
        payload["nvidia_smi"]["argv"] = ["sh", "-c", "nvidia-smi"]

        with self.assertRaises(ValueError):
            parse_compute_runtime_probe_output(_encoded(payload), job_id="42")

    def test_stores_compute_snapshot_with_ttl(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = PlatformSnapshotStore(Path(temporary) / "pilot107.db")
            record = store_compute_runtime_probe_output(
                store=store,
                owner="alice",
                job_id="42",
                output=_output(nvidia_returncode=0, cuda_available=True),
                source_type=ObservationSourceType.SIMULATOR,
                source_name="docker-gpu-job",
                ttl_seconds=60,
            )

        self.assertEqual(record.scope, PlatformSnapshotScope.COMPUTE_JOB)
        self.assertEqual(record.collection_status, SnapshotCollectionStatus.COMPLETE)
        self.assertEqual(record.expires_at, "2026-07-15T00:01:00+00:00")


def _output(*, nvidia_returncode: int, cuda_available: bool) -> str:
    return "job prelude\n" + _encoded(
        _payload(
            nvidia_returncode=nvidia_returncode,
            cuda_available=cuda_available,
        )
    )


def _encoded(payload: dict) -> str:
    return "PILOT107_COMPUTE_PROBE_JSON=" + json.dumps(payload, separators=(",", ":"))


def _payload(*, nvidia_returncode: int, cuda_available: bool) -> dict:
    return {
        "schema": PROBE_SCHEMA,
        "captured_at": "2026-07-15T00:00:00+00:00",
        "hostname": "anode16",
        "nvidia_smi": {
            "argv": [
                "nvidia-smi",
                "--query-gpu=name,driver_version,memory.total",
                "--format=csv,noheader,nounits",
            ],
            "returncode": nvidia_returncode,
            "stdout": "A100, 550, 81920\n" if nvidia_returncode == 0 else "",
            "stderr": "" if nvidia_returncode == 0 else "command unavailable",
        },
        "torch": {
            "available": True,
            "cuda_available": cuda_available,
            "device_count": 1 if cuda_available else 0,
        },
    }


if __name__ == "__main__":
    unittest.main()
