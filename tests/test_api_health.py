import tempfile
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


if __name__ == "__main__":
    unittest.main()
