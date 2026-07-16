import hashlib
import tempfile
import unittest
from pathlib import Path

from pilot107.core.identity import UserIdentity
from pilot107.core.paths import PathPolicyError, authorize_path
from pilot107.worker.evidence import (
    AuthorizedFilesystemEvidenceTransport,
    EvidencePolicy,
    EvidenceTransport,
)


class EvidenceTransportContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.alice = self.root / "public" / "home" / "alice"
        self.bob = self.root / "public" / "home" / "bob"
        self.alice.mkdir(parents=True)
        self.bob.mkdir(parents=True)
        (self.alice / "result.txt").write_text("hello evidence\n", encoding="utf-8")
        (self.alice / "slurm-123.out").write_text("internal log\n", encoding="utf-8")
        nested = self.alice / "nested" / "too" / "deep"
        nested.mkdir(parents=True)
        (nested / "deep.txt").write_text("deep\n", encoding="utf-8")
        (self.bob / "secret.txt").write_text("secret\n", encoding="utf-8")
        self.identity = UserIdentity(username="alice")
        self.transport: EvidenceTransport = AuthorizedFilesystemEvidenceTransport(
            allowed_roots=[self.alice],
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_probe_reports_authorized_filesystem_capabilities(self) -> None:
        capability = self.transport.probe(self.identity)

        self.assertEqual(capability.transport, "authorized_filesystem")
        self.assertTrue(capability.can_stat)
        self.assertTrue(capability.can_tail)
        self.assertEqual(capability.authorized_roots, (str(self.alice.resolve()),))

    def test_prepare_run_root_stays_under_authorized_root(self) -> None:
        root = self.transport.prepare_run_root(
            self.identity,
            "run_123",
            EvidencePolicy(),
        )

        self.assertEqual(root.run_id, "run_123")
        self.assertTrue(root.path.resolved.is_relative_to(self.alice.resolve()))

    def test_stat_tail_and_range_read_authorized_file(self) -> None:
        safe = authorize_path(str(self.alice / "result.txt"), [self.alice])

        stat_result = self.transport.stat(self.identity, safe)
        tail = self.transport.read_text_tail(self.identity, safe, max_bytes=8)
        data = self.transport.read_bytes_range(self.identity, safe, offset=0, length=5)

        self.assertEqual(stat_result.kind, "regular file")
        self.assertEqual(tail.tail, "vidence\n")
        self.assertTrue(tail.truncated)
        self.assertEqual(
            tail.sha256,
            hashlib.sha256((self.alice / "result.txt").read_bytes()).hexdigest(),
        )
        self.assertEqual(data, b"hello")

    def test_inventory_applies_policy_limits_and_exclusions(self) -> None:
        safe_root = authorize_path(str(self.alice), [self.alice])

        inventory = self.transport.inventory(
            self.identity,
            safe_root,
            EvidencePolicy(max_depth=2),
        )

        self.assertEqual([file.relative_path for file in inventory.files], ["result.txt"])
        self.assertIn("slurm-123.out: excluded", inventory.skipped)
        self.assertIn("nested/too/deep/deep.txt: depth", inventory.skipped)
        self.assertEqual(inventory.total_size_bytes, len("hello evidence\n"))

    def test_safe_path_rejects_symlink_escape_before_transport_read(self) -> None:
        link = self.alice / "bob-secret-link.txt"
        link.symlink_to(self.bob / "secret.txt")

        with self.assertRaises(PathPolicyError):
            authorize_path(str(link), [self.alice])

    def test_transport_reauthorizes_path_against_its_own_roots(self) -> None:
        bob_safe = authorize_path(str(self.bob / "secret.txt"), [self.bob])

        with self.assertRaises(PathPolicyError):
            self.transport.stat(self.identity, bob_safe)


if __name__ == "__main__":
    unittest.main()
