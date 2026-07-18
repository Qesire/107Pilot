from __future__ import annotations

import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from pilot107.api.http_app import build_api, make_handler
from pilot107.worker.telemetry import WorkerTelemetryStore


class StdlibHttpMetricsTests(unittest.TestCase):
    def test_metrics_are_available_without_identity_and_share_api_registry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            api = build_api(
                db_path=root / "pilot107.db",
                evidence_root=root / "evidence",
                auth_required=True,
            )
            WorkerTelemetryStore(
                root=root / "worker-metrics",
                worker_id="stdlib-worker",
            ).update(
                increments={"ticks_total": 3},
                tick_duration_seconds=0.1,
            )
            server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(api))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                with urllib.request.urlopen(f"{base}/api/v1/health/live") as response:
                    self.assertEqual(response.status, 200)
                with self.assertRaises(urllib.error.HTTPError):
                    urllib.request.urlopen(f"{base}/api/v1/runs/run_missing")
                with urllib.request.urlopen(f"{base}/metrics") as response:
                    metrics = response.read().decode("utf-8")
                    content_type = response.headers["Content-Type"]
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

        self.assertIn("text/plain", content_type)
        self.assertIn(
            'pilot107_api_requests_total{method="GET",route="/api/v1/health/live",status="200"} 1',
            metrics,
        )
        self.assertIn(
            'pilot107_worker_ticks_total{worker_id="stdlib-worker"} 3',
            metrics,
        )
        self.assertIn(
            'pilot107_control_trace_writes_total{outcome="success"} 1',
            metrics,
        )
        self.assertIn("pilot107_metrics_scrape_error 0", metrics)


if __name__ == "__main__":
    unittest.main()
