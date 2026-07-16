import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

from pilot107.core.platform_snapshot import (
    CommandObservation,
    PlatformSnapshot,
    PlatformSnapshotScope,
)

_PROBE_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts/real107_probe/probe_real107_cli_snapshot.py"
)
_SPEC = importlib.util.spec_from_file_location("pilot107_real107_cli_probe", _PROBE_PATH)
assert _SPEC is not None
assert _SPEC.loader is not None
cli_probe = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(cli_probe)


class Real107CliProbeTests(unittest.TestCase):
    def test_write_snapshot_artifacts_creates_design_tree(self) -> None:
        snapshot = PlatformSnapshot(
            snapshot_id="snapshot-cli-test",
            scope=PlatformSnapshotScope.LOGIN_NODE,
            captured_at="2026-07-15T00:00:00+00:00",
            collector_version="test",
            command_results=(
                CommandObservation(
                    name="scontrol_show_part",
                    argv=("scontrol", "show", "part"),
                    returncode=0,
                    stdout=(
                        "PartitionName=Students AllowQos=qos_stu_default "
                        "MaxTime=04:00:00 State=UP Nodes=anode[05-17]\n"
                    ),
                    stderr="",
                ),
                CommandObservation(
                    name="hostname",
                    argv=("hostname",),
                    returncode=0,
                    stdout="tradmin-redacted\n",
                    stderr="",
                ),
            ),
        )
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "snapshot"
            cli_probe.write_snapshot_artifacts(
                snapshot=snapshot,
                out_dir=out_dir,
                source_name="real107-cli-test-only",
                expires_at="2026-07-16T00:00:00+00:00",
            )

            manifest = json.loads((out_dir / "manifest.json").read_text())
            platform_snapshot = json.loads((out_dir / "platform_snapshot.json").read_text())

            self.assertEqual(manifest["source_name"], "real107-cli-test-only")
            self.assertEqual(platform_snapshot["snapshot_id"], "snapshot-cli-test")
            self.assertTrue((out_dir / "raw/scontrol-show-part.txt").is_file())
            self.assertTrue((out_dir / "parsed/partitions.json").is_file())
            self.assertTrue((out_dir / "redaction-report.json").is_file())

    def test_main_supports_help_without_import_side_effects(self) -> None:
        with self.assertRaises(SystemExit) as ctx:
            cli_probe.main(["--help"])

        self.assertEqual(ctx.exception.code, 0)


if __name__ == "__main__":
    unittest.main()

