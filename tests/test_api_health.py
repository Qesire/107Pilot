import json
import tempfile
import time
import unittest
from pathlib import Path

from pilot107.api.health import ApiHealthService
from pilot107.core.run_store import RunStore


class ApiHealthServiceTests(unittest.TestCase):
    def test_missing_evidence_directory_makes_readiness_fail(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service = ApiHealthService(
                store=RunStore(root / "pilot107.db"),
                evidence_root=root / "missing-evidence",
                platform_snapshot_store=None,
                submission_enabled=False,
                llm_enabled=False,
            )

            ready, payload = service.ready()

        self.assertFalse(ready)
        self.assertEqual(payload["status"], "not_ready")
        checks = {item["name"]: item for item in payload["checks"]}
        self.assertEqual(checks["database"]["status"], "ok")
        self.assertEqual(checks["evidence_store"]["status"], "unavailable")
        self.assertFalse(checks["run_submission"]["required"])

    def test_worker_heartbeat_disabled_when_path_unset(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "evidence").mkdir()
            service = ApiHealthService(
                store=RunStore(root / "pilot107.db"),
                evidence_root=root / "evidence",
                platform_snapshot_store=None,
                submission_enabled=False,
                llm_enabled=False,
                worker_health_path=None,
            )

            ready, payload = service.ready()

        self.assertTrue(ready)
        checks = {item["name"]: item for item in payload["checks"]}
        self.assertEqual(checks["worker_heartbeat"]["status"], "disabled")
        self.assertFalse(checks["worker_heartbeat"]["required"])

    def test_worker_heartbeat_ok_when_file_fresh_and_ok_true(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "evidence").mkdir()
            health_path = root / "worker-health.json"
            health_path.write_text(
                json.dumps({"ok": True, "last_tick_unix": time.time()}),
                encoding="utf-8",
            )
            service = ApiHealthService(
                store=RunStore(root / "pilot107.db"),
                evidence_root=root / "evidence",
                platform_snapshot_store=None,
                submission_enabled=False,
                llm_enabled=False,
                worker_health_path=str(health_path),
            )

            ready, payload = service.ready()

        self.assertTrue(ready)
        checks = {item["name"]: item for item in payload["checks"]}
        self.assertEqual(checks["worker_heartbeat"]["status"], "ok")
        self.assertTrue(checks["worker_heartbeat"]["required"])

    def test_worker_heartbeat_not_ready_when_ok_false(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "evidence").mkdir()
            health_path = root / "worker-health.json"
            health_path.write_text(
                json.dumps({"ok": False, "last_tick_unix": time.time()}),
                encoding="utf-8",
            )
            service = ApiHealthService(
                store=RunStore(root / "pilot107.db"),
                evidence_root=root / "evidence",
                platform_snapshot_store=None,
                submission_enabled=False,
                llm_enabled=False,
                worker_health_path=str(health_path),
            )

            ready, payload = service.ready()

        self.assertFalse(ready)
        checks = {item["name"]: item for item in payload["checks"]}
        self.assertEqual(checks["worker_heartbeat"]["status"], "unavailable")
        self.assertTrue(checks["worker_heartbeat"]["required"])

    def test_worker_heartbeat_not_ready_when_stale(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "evidence").mkdir()
            health_path = root / "worker-health.json"
            health_path.write_text(
                json.dumps({"ok": True, "last_tick_unix": time.time() - 600}),
                encoding="utf-8",
            )
            service = ApiHealthService(
                store=RunStore(root / "pilot107.db"),
                evidence_root=root / "evidence",
                platform_snapshot_store=None,
                submission_enabled=False,
                llm_enabled=False,
                worker_health_path=str(health_path),
            )

            ready, payload = service.ready()

        self.assertFalse(ready)
        checks = {item["name"]: item for item in payload["checks"]}
        self.assertEqual(checks["worker_heartbeat"]["status"], "unavailable")
        self.assertTrue(checks["worker_heartbeat"]["required"])

    def test_worker_heartbeat_not_ready_when_file_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "evidence").mkdir()
            service = ApiHealthService(
                store=RunStore(root / "pilot107.db"),
                evidence_root=root / "evidence",
                platform_snapshot_store=None,
                submission_enabled=False,
                llm_enabled=False,
                worker_health_path=str(root / "missing-worker-health.json"),
            )

            ready, payload = service.ready()

        self.assertFalse(ready)
        checks = {item["name"]: item for item in payload["checks"]}
        self.assertEqual(checks["worker_heartbeat"]["status"], "unavailable")
        self.assertTrue(checks["worker_heartbeat"]["required"])


if __name__ == "__main__":
    unittest.main()
