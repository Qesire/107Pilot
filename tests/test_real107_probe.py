import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

_PROBE_PATH = (
    Path(__file__).resolve().parents[1] / "scripts/real107_probe/probe_real107_snapshot.py"
)
_SPEC = importlib.util.spec_from_file_location("pilot107_real107_probe", _PROBE_PATH)
assert _SPEC is not None
assert _SPEC.loader is not None
probe = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(probe)


class Real107ProbeTests(unittest.TestCase):
    def test_build_snapshot_from_fake_read_only_probe(self) -> None:
        def request_json(path: str):
            payloads = {
                "/ping": {"meta": {"slurm_version": "25.11.0"}},
                "/partitions": {
                    "partitions": [
                        {"name": "debug", "qos": "normal high"},
                        {"partition": "cpu", "qos_allowed": ["normal"]},
                    ]
                },
                "/nodes": {"nodes": [{"name": "n1", "state": "idle"}]},
                "/jobs": {"jobs": [{"job_id": 1, "user_name": "alice", "job_state": "RUNNING"}]},
                "/openapi.json": {"openapi": "3.0.0", "Authorization": "Bearer secret"},
            }
            return 200, payloads[path]

        snapshot, report = probe.build_snapshot_from_probe(
            base_url="http://107.ustc.edu.cn:6820",
            api_version="v0.0.41",
            username="alice",
            request_json=request_json,
            captured_at="2026-07-12T00:00:00+00:00",
            token_source="test",
        )

        self.assertEqual(snapshot["cluster"]["source_authority"], "real_cluster_probe")
        self.assertEqual(snapshot["cluster"]["partitions"], ["cpu", "debug"])
        self.assertEqual(snapshot["cluster"]["qos"], ["high", "normal"])
        self.assertEqual(snapshot["cluster"]["slurm_version"], "25.11.0")
        self.assertEqual(snapshot["users"][0]["home"], "/public/home/alice")
        self.assertEqual(snapshot["auth_strategy"], "single_user_jwt_bearer")
        self.assertEqual(report["summary"]["status"], "ok")
        self.assertTrue(snapshot["openapi_digest"])
        self.assertNotIn("secret", json.dumps(report, sort_keys=True))

    def test_build_snapshot_reports_partial_when_optional_probe_fails(self) -> None:
        def request_json(path: str):
            if path == "/nodes":
                raise RuntimeError("network timeout")
            return 200, {"partitions": [{"name": "debug"}]} if path == "/partitions" else {}

        snapshot, report = probe.build_snapshot_from_probe(
            base_url="http://107.ustc.edu.cn:6820",
            api_version="v0.0.41",
            username="alice",
            request_json=request_json,
            captured_at="2026-07-12T00:00:00+00:00",
        )

        self.assertEqual(snapshot["cluster"]["partitions"], ["debug"])
        self.assertEqual(report["summary"]["status"], "partial")
        self.assertIn("nodes", report["summary"]["failed_probes"])

    def test_parse_token_text_accepts_scontrol_output(self) -> None:
        self.assertEqual(probe._parse_token_text("SLURM_JWT=abc.def\n"), "abc.def")
        self.assertEqual(probe._parse_token_text("abc.def\n"), "abc.def")

    def test_main_without_token_writes_fallback_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "out"
            rc = probe.main(
                [
                    "--base-url",
                    "http://107.ustc.edu.cn:6820",
                    "--api-version",
                    "v0.0.41",
                    "--username",
                    "alice",
                    "--out-dir",
                    str(out_dir),
                    "--token-env",
                    "PILOT107_TEST_TOKEN_NOT_SET",
                ]
            )
            snapshot = json.loads((out_dir / "configuration_snapshot.json").read_text())
            report = json.loads((out_dir / "probe_report.json").read_text())

        self.assertEqual(rc, 0)
        self.assertEqual(snapshot["cluster"]["source_authority"], "real_cluster_probe")
        self.assertEqual(report["summary"]["status"], "auth_required")
        self.assertNotIn("token", json.dumps(snapshot).lower())


if __name__ == "__main__":
    unittest.main()
