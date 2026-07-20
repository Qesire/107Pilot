"""Unit tests for the pure attribution helper in ``pilot107.worker.evidence``.

These tests target ``compute_file_attribution`` directly so the attribution
contract is verified without spinning up a collector or filesystem.
"""

from __future__ import annotations

import unittest

from pilot107.worker.evidence import compute_file_attribution


class ComputeFileAttributionTests(unittest.TestCase):
    def test_mtime_after_started_is_created_by_run(self) -> None:
        result = compute_file_attribution(
            mtime_epoch=1000.0,
            started_at_epoch=500.0,
            relative_path="out/result.txt",
            expected_outputs=["out/result.txt"],
        )
        self.assertEqual(result["attribution"], "created_by_run")

    def test_mtime_before_started_is_preexisting(self) -> None:
        result = compute_file_attribution(
            mtime_epoch=100.0,
            started_at_epoch=500.0,
            relative_path="old/result.txt",
            expected_outputs=[],
        )
        self.assertEqual(result["attribution"], "preexisting")

    def test_mtime_equal_started_is_preexisting(self) -> None:
        # Not strictly greater, so it is NOT treated as created by this run.
        result = compute_file_attribution(
            mtime_epoch=500.0,
            started_at_epoch=500.0,
            relative_path="equal/result.txt",
            expected_outputs=[],
        )
        self.assertEqual(result["attribution"], "preexisting")

    def test_missing_started_at_is_unknown(self) -> None:
        result = compute_file_attribution(
            mtime_epoch=1000.0,
            started_at_epoch=None,
            relative_path="out/result.txt",
            expected_outputs=["out/result.txt"],
        )
        self.assertEqual(result["attribution"], "unknown")

    def test_in_expected_outputs_true(self) -> None:
        result = compute_file_attribution(
            mtime_epoch=1000.0,
            started_at_epoch=500.0,
            relative_path="out/result.txt",
            expected_outputs=["out/result.txt", "other.txt"],
        )
        self.assertTrue(result["in_expected_outputs"])

    def test_not_in_expected_outputs_false(self) -> None:
        result = compute_file_attribution(
            mtime_epoch=1000.0,
            started_at_epoch=500.0,
            relative_path="out/result.txt",
            expected_outputs=["unrelated.txt"],
        )
        self.assertFalse(result["in_expected_outputs"])

    def test_empty_expected_outputs_is_false(self) -> None:
        result = compute_file_attribution(
            mtime_epoch=1000.0,
            started_at_epoch=500.0,
            relative_path="out/result.txt",
            expected_outputs=[],
        )
        self.assertFalse(result["in_expected_outputs"])

    def test_baseline_sha256_always_none(self) -> None:
        # Baseline capture is not yet implemented; field is reserved.
        for started_at in (500.0, None):
            result = compute_file_attribution(
                mtime_epoch=1000.0,
                started_at_epoch=started_at,
                relative_path="out/result.txt",
                expected_outputs=["out/result.txt"],
            )
            self.assertIsNone(result["baseline_sha256"])

    def test_result_keys(self) -> None:
        result = compute_file_attribution(
            mtime_epoch=1000.0,
            started_at_epoch=500.0,
            relative_path="out/result.txt",
            expected_outputs=["out/result.txt"],
        )
        self.assertEqual(
            set(result.keys()),
            {"attribution", "in_expected_outputs", "baseline_sha256"},
        )


if __name__ == "__main__":
    unittest.main()
