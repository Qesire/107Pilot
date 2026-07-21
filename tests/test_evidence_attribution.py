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

    # Round-8 P1-1: baseline entries written by ``_capture_baseline`` with a
    # probe-failure ``status`` (timeout / path_invalid / path_too_long / error)
    # are unusable for attribution. They must NOT be treated as baseline-missing
    # (which would let a pre-existing file masquerade as ``created`` /
    # ``modified`` and falsely satisfy expected-output verification). Instead
    # emit the stricter ``baseline_unavailable`` so remediation fails closed.

    def test_baseline_timeout_with_existing_final_is_baseline_unavailable(self) -> None:
        # Pre-create result.txt; baseline probe times out; job does NOT modify
        # the file; final still exists. MUST NOT classify as ``created``.
        result = compute_file_attribution(
            mtime_epoch=1000.0,
            started_at_epoch=500.0,
            relative_path="r",
            expected_outputs=["r"],
            baseline_entry={"path": "r", "status": "timeout"},
            is_expected=True,
            final_sha256="abc",
        )
        self.assertEqual(result["attribution"], "baseline_unavailable")

    def test_baseline_path_invalid_with_existing_final_is_baseline_unavailable(self) -> None:
        result = compute_file_attribution(
            mtime_epoch=1000.0,
            started_at_epoch=500.0,
            relative_path="r",
            expected_outputs=["r"],
            baseline_entry={"path": "r", "status": "path_invalid"},
            is_expected=True,
            final_sha256="abc",
        )
        self.assertEqual(result["attribution"], "baseline_unavailable")

    def test_baseline_error_with_missing_final_is_baseline_unavailable(self) -> None:
        # Even when the final file is missing, an unavailable baseline must not
        # downgrade to ``missing`` (which the verifier could misread).
        result = compute_file_attribution(
            mtime_epoch=None,  # type: ignore[arg-type]
            started_at_epoch=500.0,
            relative_path="r",
            expected_outputs=["r"],
            baseline_entry={"path": "r", "status": "error"},
            is_expected=True,
            final_sha256=None,
        )
        self.assertEqual(result["attribution"], "baseline_unavailable")

    def test_captured_baseline_unchanged_is_regression_guard(self) -> None:
        # A captured baseline (no ``status`` key) with matching final sha must
        # still classify as ``unchanged`` — the legitimate path must not regress.
        result = compute_file_attribution(
            mtime_epoch=1000.0,
            started_at_epoch=500.0,
            relative_path="r",
            expected_outputs=["r"],
            baseline_entry={"path": "r", "exists": True, "sha256": "abc"},
            is_expected=True,
            final_sha256="abc",
        )
        self.assertEqual(result["attribution"], "unchanged")

    def test_captured_missing_baseline_with_final_is_created_regression_guard(self) -> None:
        # A captured-missing baseline (no ``status``, ``exists=False``) with a
        # final file present must still classify as ``created`` — the LEGITIMATE
        # created path must keep working.
        result = compute_file_attribution(
            mtime_epoch=1000.0,
            started_at_epoch=500.0,
            relative_path="r",
            expected_outputs=["r"],
            baseline_entry={"path": "r", "exists": False},
            is_expected=True,
            final_sha256="abc",
        )
        self.assertEqual(result["attribution"], "created")

    # Round-11 P1-1: the new stat-classification error codes (errno_13,
    # errno_5, stat_permission_denied, stat_timeout, stat_unclassified,
    # sha256_read_failed) all carry a truthy ``status`` key, so
    # _baseline_entry_unavailable rejects them → baseline_unavailable. These
    # prove the round-11 fix closes the false-green where a non-ENOENT stat
    # error became exists=false → created.

    def test_baseline_errno_13_with_final_is_baseline_unavailable(self) -> None:
        # PermissionError on os.stat → status=error, error_code=errno_13.
        result = compute_file_attribution(
            mtime_epoch=1000.0,
            started_at_epoch=500.0,
            relative_path="r",
            expected_outputs=["r"],
            baseline_entry={"path": "r", "status": "error", "error_code": "errno_13"},
            is_expected=True,
            final_sha256="abc",
        )
        self.assertEqual(result["attribution"], "baseline_unavailable")

    def test_baseline_errno_5_with_final_is_baseline_unavailable(self) -> None:
        # OSError(EIO) on os.stat → status=error, error_code=errno_5.
        result = compute_file_attribution(
            mtime_epoch=1000.0,
            started_at_epoch=500.0,
            relative_path="r",
            expected_outputs=["r"],
            baseline_entry={"path": "r", "status": "error", "error_code": "errno_5"},
            is_expected=True,
            final_sha256="abc",
        )
        self.assertEqual(result["attribution"], "baseline_unavailable")

    def test_baseline_stat_unclassified_with_final_is_baseline_unavailable(self) -> None:
        # Simulator non-zero stat with no recognizable marker →
        # status=error, error_code=stat_unclassified (fail-closed default).
        result = compute_file_attribution(
            mtime_epoch=1000.0,
            started_at_epoch=500.0,
            relative_path="r",
            expected_outputs=["r"],
            baseline_entry={
                "path": "r",
                "status": "error",
                "error_code": "stat_unclassified",
            },
            is_expected=True,
            final_sha256="abc",
        )
        self.assertEqual(result["attribution"], "baseline_unavailable")

    def test_baseline_sha256_read_failed_with_final_is_baseline_unavailable(self) -> None:
        # stat succeeded but sha256sum/open failed → status=error,
        # error_code=sha256_read_failed. Previously returned exists=True,
        # sha=None → false ``unchanged``; now baseline_unavailable.
        result = compute_file_attribution(
            mtime_epoch=1000.0,
            started_at_epoch=500.0,
            relative_path="r",
            expected_outputs=["r"],
            baseline_entry={
                "path": "r",
                "status": "error",
                "error_code": "sha256_read_failed",
            },
            is_expected=True,
            final_sha256="abc",
        )
        self.assertEqual(result["attribution"], "baseline_unavailable")


if __name__ == "__main__":
    unittest.main()
