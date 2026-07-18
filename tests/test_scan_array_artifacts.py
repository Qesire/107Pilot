from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCANNER = ROOT / "scripts/scan-array-artifacts.py"


class ScanArrayArtifactsTests(unittest.TestCase):
    def test_complete_set_returns_empty_spec_and_writes_report(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._write_complete_tasks(root, {0, 1, 2})
            report = root / "scan.json"

            result = self._run(root, expected=3, output=report, require_complete=True)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "")
            payload = json.loads(report.read_text())
            self.assertEqual(payload["complete_tasks"], 3)
            self.assertEqual(payload["missing_tasks"], [])

    def test_missing_empty_metadata_and_marker_compress_to_array_spec(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._write_complete_tasks(root, {0, 2, 4})
            (root / "shards/task_2.json").write_text("")
            (root / "complete/task_4.COMPLETE").unlink()
            report = root / "scan.json"

            result = self._run(root, expected=6, output=report, require_complete=True)

            self.assertEqual(result.returncode, 1)
            self.assertEqual(result.stdout.strip(), "1-5")
            payload = json.loads(report.read_text())
            self.assertEqual(payload["missing_tasks"], [1, 2, 3, 4, 5])

    def test_symlink_escape_is_treated_as_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, tempfile.TemporaryDirectory() as outside:
            root = Path(temporary)
            self._write_complete_tasks(root, {0})
            external = Path(outside) / "artifact.bin"
            external.write_bytes(b"not-owned-by-root")
            (root / "shards/task_0.bin").unlink()
            (root / "shards/task_0.bin").symlink_to(external)

            result = self._run(root, expected=1, require_complete=True)

            self.assertEqual(result.returncode, 1)
            self.assertEqual(result.stdout.strip(), "0")

    def test_unsafe_pattern_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCANNER),
                    "--root",
                    temporary,
                    "--expected-tasks",
                    "1",
                    "--artifact-pattern",
                    "../task_{task}.bin",
                ],
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("pattern must stay below --root", result.stderr)

    def _run(
        self,
        root: Path,
        *,
        expected: int,
        output: Path | None = None,
        require_complete: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        command = [
            sys.executable,
            str(SCANNER),
            "--root",
            str(root),
            "--expected-tasks",
            str(expected),
            "--metadata-pattern",
            "shards/task_{task}.json",
        ]
        if output is not None:
            command.extend(["--output", str(output)])
        if require_complete:
            command.append("--require-complete")
        return subprocess.run(command, capture_output=True, text=True, check=False)

    @staticmethod
    def _write_complete_tasks(root: Path, tasks: set[int]) -> None:
        (root / "shards").mkdir(parents=True)
        (root / "complete").mkdir(parents=True)
        for task in tasks:
            (root / f"shards/task_{task}.bin").write_bytes(b"artifact")
            (root / f"shards/task_{task}.json").write_text("{}\n")
            (root / f"complete/task_{task}.COMPLETE").write_text("ok\n")


if __name__ == "__main__":
    unittest.main()
