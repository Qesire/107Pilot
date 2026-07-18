from __future__ import annotations

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pilot107.core.control_repository import (
    ControlRepository,
    ControlRepositoryConflict,
    SQLiteControlRepository,
)


class MutableClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 7, 18, 0, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


class SQLiteControlRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "pilot107.db"
        self.clock = MutableClock()
        self.store = SQLiteControlRepository(self.db_path, clock=self.clock)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_implements_backend_neutral_contract(self) -> None:
        repository: ControlRepository = self.store
        self.assertIs(repository, self.store)

    def test_lease_is_exclusive_and_reclaim_increments_fencing_token(self) -> None:
        first = self.store.acquire_lease(
            resource_kind="run.submit",
            resource_id="run_1",
            owner="api-a",
            lease_seconds=10,
        )
        blocked = self.store.acquire_lease(
            resource_kind="run.submit",
            resource_id="run_1",
            owner="api-b",
            lease_seconds=10,
        )
        self.assertIsNotNone(first)
        self.assertIsNone(blocked)

        self.clock.advance(11)
        reclaimed = self.store.acquire_lease(
            resource_kind="run.submit",
            resource_id="run_1",
            owner="api-b",
            lease_seconds=10,
        )
        assert first is not None and reclaimed is not None
        self.assertEqual(reclaimed.fencing_token, first.fencing_token + 1)
        self.assertFalse(self.store.release_lease(first))
        with self.assertRaises(ControlRepositoryConflict):
            self.store.renew_lease(first, lease_seconds=10)

    def test_concurrent_lease_claim_has_exactly_one_winner(self) -> None:
        def claim(owner: str) -> bool:
            repository = SQLiteControlRepository(self.db_path, clock=self.clock)
            return repository.acquire_lease(
                resource_kind="collection",
                resource_id="task-1",
                owner=owner,
                lease_seconds=30,
            ) is not None

        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(claim, ("worker-a", "worker-b", "worker-c", "worker-d")))
        self.assertEqual(results.count(True), 1)

    def test_outbox_enqueue_is_idempotent_and_content_bound(self) -> None:
        first, created = self.store.enqueue(
            message_id="message-1",
            topic="run.submit",
            aggregate_id="run_1",
            payload={"run_id": "run_1"},
        )
        duplicate, duplicate_created = self.store.enqueue(
            message_id="message-1",
            topic="run.submit",
            aggregate_id="run_1",
            payload={"run_id": "run_1"},
        )
        self.assertTrue(created)
        self.assertFalse(duplicate_created)
        self.assertEqual(duplicate, first)
        with self.assertRaises(ControlRepositoryConflict):
            self.store.enqueue(
                message_id="message-1",
                topic="run.submit",
                aggregate_id="run_2",
                payload={"run_id": "run_2"},
            )

    def test_outbox_claim_is_exclusive_and_stale_ack_is_fenced(self) -> None:
        self.store.enqueue(
            message_id="message-1",
            topic="run.submit",
            aggregate_id="run_1",
            payload={"run_id": "run_1"},
        )
        first = self.store.claim_outbox(owner="worker-a", limit=10, lease_seconds=10)[0]
        self.assertEqual(first.attempts, 1)
        self.assertEqual(
            self.store.claim_outbox(owner="worker-b", limit=10, lease_seconds=10), []
        )

        self.clock.advance(11)
        reclaimed = self.store.claim_outbox(owner="worker-b", limit=10, lease_seconds=10)[0]
        self.assertEqual(reclaimed.fencing_token, first.fencing_token + 1)
        self.assertEqual(reclaimed.attempts, 2)
        with self.assertRaises(ControlRepositoryConflict):
            self.store.acknowledge(
                message_id=first.message_id,
                owner="worker-a",
                fencing_token=first.fencing_token,
            )
        self.store.acknowledge(
            message_id=reclaimed.message_id,
            owner="worker-b",
            fencing_token=reclaimed.fencing_token,
        )
        self.assertEqual(self.store.get_outbox("message-1").state, "succeeded")

    def test_retry_delays_delivery_and_dead_letters_at_attempt_budget(self) -> None:
        self.store.enqueue(
            message_id="message-1",
            topic="agent.execute",
            aggregate_id="session_1",
            payload={"session_id": "session_1"},
        )
        first = self.store.claim_outbox(owner="worker-a", limit=1, lease_seconds=10)[0]
        pending = self.store.retry(
            message_id=first.message_id,
            owner="worker-a",
            fencing_token=first.fencing_token,
            error="gateway unavailable",
            delay_seconds=5,
            max_attempts=2,
        )
        self.assertEqual(pending.state, "pending")
        self.assertEqual(self.store.claim_outbox(owner="worker-b", limit=1, lease_seconds=10), [])

        self.clock.advance(5)
        second = self.store.claim_outbox(owner="worker-b", limit=1, lease_seconds=10)[0]
        dead = self.store.retry(
            message_id=second.message_id,
            owner="worker-b",
            fencing_token=second.fencing_token,
            error="gateway still unavailable",
            delay_seconds=10,
            max_attempts=2,
        )
        self.assertEqual(dead.state, "dead_letter")
        self.assertEqual(dead.last_error, "gateway still unavailable")
        self.clock.advance(10)
        self.assertEqual(self.store.claim_outbox(owner="worker-c", limit=1, lease_seconds=10), [])


if __name__ == "__main__":
    unittest.main()
