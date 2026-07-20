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
            {"attribution", "in_expected_outputs", "baseline_sha256", "final_sha256"},
        )

    def test_baseline_absent_and_file_present_is_created(self) -> None:
        result = compute_file_attribution(
            mtime_epoch=1000.0,
            started_at_epoch=500.0,
            relative_path="out/result.txt",
            expected_outputs=["out/result.txt"],
            baseline_entry={
                "path": "out/result.txt",
                "exists": False,
                "size_bytes": None,
                "mtime_epoch": None,
                "sha256": None,
            },
            is_expected=True,
            final_sha256="abc123",
        )
        self.assertEqual(result["attribution"], "created")
        self.assertIsNone(result["baseline_sha256"])
        self.assertEqual(result["final_sha256"], "abc123")

    def test_baseline_differs_from_final_is_modified(self) -> None:
        result = compute_file_attribution(
            mtime_epoch=1000.0,
            started_at_epoch=500.0,
            relative_path="out/result.txt",
            expected_outputs=["out/result.txt"],
            baseline_entry={
                "path": "out/result.txt",
                "exists": True,
                "size_bytes": 10,
                "mtime_epoch": 100.0,
                "sha256": "old",
            },
            is_expected=True,
            final_sha256="new",
        )
        self.assertEqual(result["attribution"], "modified")
        self.assertEqual(result["baseline_sha256"], "old")

    def test_baseline_equals_final_is_unchanged(self) -> None:
        result = compute_file_attribution(
            mtime_epoch=1000.0,
            started_at_epoch=500.0,
            relative_path="out/result.txt",
            expected_outputs=["out/result.txt"],
            baseline_entry={
                "path": "out/result.txt",
                "exists": True,
                "size_bytes": 10,
                "mtime_epoch": 100.0,
                "sha256": "same",
            },
            is_expected=True,
            final_sha256="same",
        )
        self.assertEqual(result["attribution"], "unchanged")

    def test_expected_output_absent_is_missing(self) -> None:
        # Expected output with a baseline but no current file (final_sha256 is
        # None and mtime_epoch is None) classifies as missing.
        result = compute_file_attribution(
            mtime_epoch=None,  # type: ignore[arg-type]
            started_at_epoch=500.0,
            relative_path="out/result.txt",
            expected_outputs=["out/result.txt"],
            baseline_entry={
                "path": "out/result.txt",
                "exists": False,
                "size_bytes": None,
                "mtime_epoch": None,
                "sha256": None,
            },
            is_expected=True,
            final_sha256=None,
        )
        self.assertEqual(result["attribution"], "missing")

    def test_baseline_exists_but_current_missing_is_missing(self) -> None:
        result = compute_file_attribution(
            mtime_epoch=None,  # type: ignore[arg-type]
            started_at_epoch=500.0,
            relative_path="out/result.txt",
            expected_outputs=["out/result.txt"],
            baseline_entry={
                "path": "out/result.txt",
                "exists": True,
                "size_bytes": 10,
                "mtime_epoch": 100.0,
                "sha256": "abc",
            },
            is_expected=True,
            final_sha256=None,
        )
        self.assertEqual(result["attribution"], "missing")

    def test_non_expected_with_baseline_keeps_mtime_logic(self) -> None:
        # A non-expected file with a baseline present must still use the
        # mtime-based fallback (baseline is only meaningful for expected
        # outputs).
        result = compute_file_attribution(
            mtime_epoch=1000.0,
            started_at_epoch=500.0,
            relative_path="other.txt",
            expected_outputs=["out/result.txt"],
            baseline_entry={
                "path": "other.txt",
                "exists": True,
                "size_bytes": 5,
                "mtime_epoch": 10.0,
                "sha256": "abc",
            },
            is_expected=False,
            final_sha256=None,
        )
        self.assertEqual(result["attribution"], "created_by_run")
        self.assertIsNone(result["baseline_sha256"])

    def test_expected_without_baseline_keeps_mtime_logic(self) -> None:
        # An expected output without a baseline entry falls back to mtime
        # classification (the legacy path used when no baseline was captured).
        result = compute_file_attribution(
            mtime_epoch=1000.0,
            started_at_epoch=500.0,
            relative_path="out/result.txt",
            expected_outputs=["out/result.txt"],
            baseline_entry=None,
            is_expected=True,
            final_sha256="abc",
        )
        self.assertEqual(result["attribution"], "created_by_run")
        self.assertIsNone(result["baseline_sha256"])


if __name__ == "__main__":
    unittest.main()
