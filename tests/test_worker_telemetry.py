from __future__ import annotations

import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from pilot107.core.redaction import redact_sensitive_structure, redact_sensitive_text
from pilot107.worker.telemetry import (
    WorkerTelemetryError,
    WorkerTelemetryStore,
    load_worker_metrics,
)


class WorkerTelemetryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_counters_persist_across_store_recreation(self) -> None:
        first = WorkerTelemetryStore(root=self.root, worker_id="worker-a")
        initial = first.update(
            increments={"ticks_total": 1, "submission_checked_total": 2},
            tick_duration_seconds=0.25,
            timestamp=10.0,
        )
        recreated = WorkerTelemetryStore(root=self.root, worker_id="worker-a")
        updated = recreated.update(
            increments={"ticks_total": 1, "submission_succeeded_total": 1},
            tick_duration_seconds=0.5,
            timestamp=20.0,
        )

        self.assertEqual(initial["first_tick_unix"], 10.0)
        self.assertEqual(updated["first_tick_unix"], 10.0)
        self.assertEqual(updated["last_tick_unix"], 20.0)
        self.assertEqual(updated["counters"]["ticks_total"], 2)
        self.assertEqual(updated["counters"]["submission_checked_total"], 2)
        self.assertEqual(updated["counters"]["submission_succeeded_total"], 1)
        self.assertTrue(updated["active"])
        self.assertEqual(first.path.stat().st_mode & 0o777, 0o600)

        stopped = recreated.mark_stopped(timestamp=30.0)
        assert stopped is not None
        self.assertFalse(stopped["active"])
        self.assertEqual(stopped["stopped_at_unix"], 30.0)

        resumed = recreated.update(
            increments={"ticks_total": 1},
            tick_duration_seconds=0.1,
            timestamp=40.0,
        )
        self.assertTrue(resumed["active"])
        self.assertIsNone(resumed["stopped_at_unix"])

    def test_distinct_workers_publish_independent_snapshots(self) -> None:
        for worker_id in ("worker-a", "worker-b"):
            WorkerTelemetryStore(root=self.root, worker_id=worker_id).update(
                increments={"ticks_total": 1},
                tick_duration_seconds=0.1,
            )

        snapshots = load_worker_metrics(self.root)

        self.assertEqual({item["worker_id"] for item in snapshots}, {"worker-a", "worker-b"})
        self.assertEqual(len(list(self.root.glob("worker-*.json"))), 2)

    def test_same_worker_concurrent_updates_do_not_lose_counts(self) -> None:
        def update(_index: int) -> None:
            WorkerTelemetryStore(root=self.root, worker_id="worker-shared").update(
                increments={"ticks_total": 1},
                tick_duration_seconds=0.01,
            )

        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(update, range(40)))

        snapshot = load_worker_metrics(self.root)[0]
        self.assertEqual(snapshot["counters"]["ticks_total"], 40)

    def test_corrupt_or_symlink_snapshot_fails_closed(self) -> None:
        store = WorkerTelemetryStore(root=self.root, worker_id="worker-corrupt")
        store.root.mkdir(parents=True, exist_ok=True)
        store.path.write_text("not-json\n", encoding="utf-8")
        with self.assertRaisesRegex(WorkerTelemetryError, "unreadable"):
            store.update(increments={"ticks_total": 1}, tick_duration_seconds=0.1)

        store.path.unlink()
        outside = self.root / "outside.json"
        outside.write_text("{}\n", encoding="utf-8")
        store.path.symlink_to(outside)
        with self.assertRaisesRegex(WorkerTelemetryError, "regular file"):
            store.update(increments={"ticks_total": 1}, tick_duration_seconds=0.1)

    def test_symlink_lock_is_rejected_without_modifying_target(self) -> None:
        store = WorkerTelemetryStore(root=self.root, worker_id="worker-lock-link")
        outside = self.root / "outside-lock-target"
        outside.write_text("keep\n", encoding="utf-8")
        store.lock_path.symlink_to(outside)

        with self.assertRaises(OSError):
            store.update(increments={"ticks_total": 1}, tick_duration_seconds=0.1)

        self.assertEqual(outside.read_text(encoding="utf-8"), "keep\n")

    def test_redaction_covers_structured_keys_urls_bearer_and_assignments(self) -> None:
        value = {
            "token": "literal-token",
            "fencing_token": 7,
            "llm_tokens": 42,
            "message": (
                "postgresql://alice:db-password@db/control "
                "Authorization=Bearer abc.def password=hunter2"
            ),
            "nested": ["api_key=top-secret", "safe"],
        }

        redacted = redact_sensitive_structure(value)
        encoded = json.dumps(redacted, sort_keys=True)

        for secret in ("literal-token", "db-password", "abc.def", "hunter2", "top-secret"):
            self.assertNotIn(secret, encoded)
        self.assertIn("safe", encoded)
        self.assertEqual(redacted["fencing_token"], 7)
        self.assertEqual(redacted["llm_tokens"], 42)
        self.assertEqual(
            redact_sensitive_text("opaque", secrets=("opaque",)),
            "<redacted>",
        )


    def test_stale_metrics_missing_new_counters_are_backfilled(self) -> None:
        """A metrics file from an older revision that lacks counters added
        later (e.g. capsule_*_total) must be upgraded on read, not rejected.
        This is the volume-persistence upgrade-compatibility path.
        """
        from pilot107.worker.telemetry import COUNTERS, WORKER_METRICS_SCHEMA

        store = WorkerTelemetryStore(root=self.root, worker_id="worker-stale")
        stale_counters = {name: 0 for name in COUNTERS if not name.startswith("capsule_")}
        store.path.write_text(
            json.dumps(
                {
                    "schema": WORKER_METRICS_SCHEMA,
                    "worker_id": "worker-stale",
                    "first_tick_unix": 1.0,
                    "last_tick_unix": 2.0,
                    "last_tick_duration_seconds": 0.01,
                    "active": False,
                    "stopped_at_unix": 2.0,
                    "counters": {**stale_counters, "ticks_total": 42},
                }
            )
        )

        updated = store.update(
            increments={"ticks_total": 1},
            tick_duration_seconds=0.1,
            timestamp=3.0,
        )

        self.assertEqual(updated["counters"]["ticks_total"], 43)
        for capsule_counter in (
            "capsule_builds_attempted_total",
            "capsule_builds_succeeded_total",
            "capsule_errors_total",
        ):
            self.assertIn(capsule_counter, updated["counters"])
            self.assertEqual(updated["counters"][capsule_counter], 0)


if __name__ == "__main__":
    unittest.main()
