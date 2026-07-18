from __future__ import annotations

import os
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
from pilot107.core.postgres_control_repository import PostgresControlRepository


class MutableClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 7, 18, 0, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


class SQLiteControlRepositoryTests(unittest.TestCase):
    store: ControlRepository

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "pilot107.db"
        self.clock = MutableClock()
        self.store = self.make_repository()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def make_repository(self) -> ControlRepository:
        return SQLiteControlRepository(self.db_path, clock=self.clock)

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
            repository = self.make_repository()
            return (
                repository.acquire_lease(
                    resource_kind="collection",
                    resource_id="task-1",
                    owner=owner,
                    lease_seconds=30,
                )
                is not None
            )

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
        self.assertEqual(self.store.claim_outbox(owner="worker-b", limit=10, lease_seconds=10), [])

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

    def test_specific_outbox_claim_does_not_consume_an_older_message(self) -> None:
        self.store.enqueue(
            message_id="message-old",
            topic="run.submit",
            aggregate_id="run_old",
            payload={"run_id": "run_old"},
        )
        self.clock.advance(1)
        self.store.enqueue(
            message_id="message-target",
            topic="run.submit",
            aggregate_id="run_target",
            payload={"run_id": "run_target"},
        )

        claimed = self.store.claim_outbox_message(
            message_id="message-target",
            owner="api-a",
            lease_seconds=30,
        )

        assert claimed is not None
        self.assertEqual(claimed.message_id, "message-target")
        self.assertEqual(claimed.attempts, 1)
        self.assertEqual(self.store.get_outbox("message-old").state, "pending")
        self.assertIsNone(
            self.store.claim_outbox_message(
                message_id="message-target",
                owner="api-b",
                lease_seconds=30,
            )
        )

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

    def test_topic_filter_leaves_other_due_messages_unclaimed(self) -> None:
        self.store.enqueue(
            message_id="submit-1",
            topic="run.submit",
            aggregate_id="run_1",
            payload={"run_id": "run_1"},
        )
        self.store.enqueue(
            message_id="agent-1",
            topic="agent.execute",
            aggregate_id="session_1",
            payload={"session_id": "session_1"},
        )

        claimed = self.store.claim_outbox(
            owner="submit-worker",
            limit=10,
            lease_seconds=30,
            topics=("run.submit",),
        )

        self.assertEqual([message.message_id for message in claimed], ["submit-1"])
        self.assertEqual(self.store.get_outbox("agent-1").state, "pending")

    def test_concurrent_outbox_claim_delivers_each_message_once(self) -> None:
        for index in range(40):
            self.store.enqueue(
                message_id=f"message-{index}",
                topic="collection.execute",
                aggregate_id=f"task-{index}",
                payload={"task_id": index},
            )

        def claim(owner: str) -> list[str]:
            repository = self.make_repository()
            return [
                message.message_id
                for message in repository.claim_outbox(
                    owner=owner,
                    limit=10,
                    lease_seconds=30,
                    topics=("collection.execute",),
                )
            ]

        with ThreadPoolExecutor(max_workers=4) as executor:
            batches = list(
                executor.map(claim, ("worker-a", "worker-b", "worker-c", "worker-d"))
            )
        claimed_ids = [message_id for batch in batches for message_id in batch]
        self.assertEqual(len(claimed_ids), 40)
        self.assertEqual(len(set(claimed_ids)), 40)
        self.assertTrue(
            all(self.store.get_outbox(message_id).attempts == 1 for message_id in claimed_ids)
        )


@unittest.skipUnless(
    os.environ.get("PILOT107_TEST_POSTGRES_DSN")
    and os.environ.get("PILOT107_TEST_POSTGRES_ALLOW_RESET") == "1",
    "set a dedicated PILOT107_TEST_POSTGRES_DSN and explicit reset opt-in",
)
class PostgresControlRepositoryContractTests(SQLiteControlRepositoryTests):
    """Runs the exact SQLite contract suite against a dedicated PostgreSQL DB."""

    def setUp(self) -> None:
        self.clock = MutableClock()
        self.dsn = os.environ["PILOT107_TEST_POSTGRES_DSN"]
        repository = PostgresControlRepository(self.dsn, clock=self.clock)
        with repository.connect() as conn:
            conn.execute("TRUNCATE control_outbox, control_leases")
        self.store = repository

    def tearDown(self) -> None:
        pass

    def make_repository(self) -> ControlRepository:
        return PostgresControlRepository(self.dsn, clock=self.clock)


if __name__ == "__main__":
    unittest.main()
