import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

_GATEWAY_PATH = Path(__file__).resolve().parents[1] / "simulator/compose/scripts/command-gateway.py"
_SPEC = importlib.util.spec_from_file_location("pilot107_command_gateway", _GATEWAY_PATH)
assert _SPEC is not None
assert _SPEC.loader is not None
gateway = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(gateway)


class CommandGatewayTests(unittest.TestCase):
    def test_rejects_wrong_bearer_token(self) -> None:
        config = gateway.GatewayConfig(token="expected", allowed_roots=["/public/home/alice"])

        with self.assertRaises(gateway.GatewayError) as caught:
            gateway._check_auth(config, "Bearer wrong")

        self.assertEqual(caught.exception.status, 401)

    def test_rejects_shell_command(self) -> None:
        config = gateway.GatewayConfig(token=None, allowed_roots=["/public/home/alice"])

        with self.assertRaises(gateway.GatewayError) as caught:
            gateway._run({"argv": ["sh", "-c", "id"]}, config)

        self.assertIn("command not allowed", str(caught.exception))

    def test_runs_structured_argv_without_shell(self) -> None:
        config = gateway.GatewayConfig(token=None, allowed_roots=["/public/home/alice"])

        with mock.patch.object(gateway.subprocess, "run") as fake_run:
            fake_run.return_value = SimpleNamespace(returncode=0, stdout="123\n", stderr="")

            result = gateway._run(
                {
                    "argv": ["sbatch", "--parsable", "/public/home/alice/job.sbatch"],
                    "timeout_seconds": 2,
                },
                config,
            )

        self.assertEqual(result["stdout"], "123\n")
        args, kwargs = fake_run.call_args
        self.assertEqual(args[0], ["sbatch", "--parsable", "/public/home/alice/job.sbatch"])
        self.assertNotIn("shell", kwargs)

    def test_environment_commands_have_exact_argument_allowlist(self) -> None:
        config = gateway.GatewayConfig(token=None, allowed_roots=["/public/home/alice"])

        with self.assertRaisesRegex(gateway.GatewayError, "arguments not allowed"):
            gateway._run({"argv": ["env", "sh", "-c", "id"]}, config)
        with self.assertRaisesRegex(gateway.GatewayError, "arguments not allowed"):
            gateway._run({"argv": ["python", "-c", "print('unsafe')"]}, config)

    def test_allows_only_the_readonly_terminal_sinfo_projection(self) -> None:
        config = gateway.GatewayConfig(token=None, allowed_roots=["/public/home/alice"])

        with mock.patch.object(gateway.subprocess, "run") as fake_run:
            fake_run.return_value = SimpleNamespace(
                returncode=0,
                stdout="debug|8|16000|(null)|idle\n",
                stderr="",
            )
            result = gateway._run(
                {"argv": ["sinfo", "-h", "-o", "%P|%c|%m|%G|%T"]},
                config,
            )

        self.assertEqual(result["returncode"], 0)
        with self.assertRaisesRegex(gateway.GatewayError, "arguments not allowed"):
            gateway._run({"argv": ["sinfo", "-R"]}, config)

    def test_path_probe_rejects_path_outside_allowed_roots(self) -> None:
        config = gateway.GatewayConfig(token=None, allowed_roots=["/public/home/alice"])

        with self.assertRaisesRegex(gateway.GatewayError, "outside allowed roots"):
            gateway._run({"argv": ["test", "-r", "/etc/shadow"]}, config)

    def test_owner_root_template_is_scoped_to_the_supplied_user(self) -> None:
        config = gateway.GatewayConfig(token=None, allowed_roots=["/public/home/{user}"])

        with self.assertRaisesRegex(gateway.GatewayError, "outside allowed roots"):
            gateway._authorize_path("/public/home/bob/result.txt", config, user="alice")

        self.assertEqual(
            gateway._authorize_path("/public/home/bob/result.txt", config, user="bob"),
            "/public/home/bob/result.txt",
        )
        with self.assertRaisesRegex(gateway.GatewayError, "requires a user"):
            gateway._authorize_path("/public/home/bob/result.txt", config)

    def test_realpath_rejects_relative_and_nul_paths(self) -> None:
        with self.assertRaises(gateway.GatewayError):
            gateway._realpath("relative/path")
        with self.assertRaises(gateway.GatewayError):
            gateway._realpath("/public/home/alice\x00/secret")

    def test_write_text_rejects_path_outside_allowed_roots(self) -> None:
        config = gateway.GatewayConfig(token=None, allowed_roots=["/public/home/alice"])

        with self.assertRaises(gateway.GatewayError) as caught:
            gateway._write_text(
                {
                    "path": "/public/home/bob/job.sbatch",
                    "content": "#!/bin/bash\n",
                    "owner": "alice",
                },
                config,
            )

        self.assertIn("path outside allowed roots", str(caught.exception))

    def test_request_id_reuses_safe_header_and_replaces_unsafe_header(self) -> None:
        self.assertEqual(gateway._request_id("req-123_ABC"), "req-123_ABC")

        generated = gateway._request_id("../../bad")

        self.assertTrue(generated.startswith("gw-"))
        self.assertNotEqual(generated, "../../bad")

    def test_rate_limit_rejects_after_configured_window_budget(self) -> None:
        config = gateway.GatewayConfig(
            token=None,
            allowed_roots=["/public/home/alice"],
            rate_limit_max_requests=2,
            rate_limit_window_seconds=60,
        )

        gateway._check_rate_limit(config, "127.0.0.1")
        gateway._check_rate_limit(config, "127.0.0.1")
        with self.assertRaises(gateway.GatewayError) as caught:
            gateway._check_rate_limit(config, "127.0.0.1")

        self.assertEqual(caught.exception.status, 429)

    def test_audit_log_redacts_content_stdin_and_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            audit_path = Path(tmp) / "gateway-audit.jsonl"
            config = gateway.GatewayConfig(
                token="secret-token",
                allowed_roots=["/public/home/alice"],
                audit_log_path=str(audit_path),
            )

            gateway._audit_request(
                config,
                request_id="req-1",
                remote_addr="127.0.0.1",
                method="POST",
                path="/write_text",
                status=200,
                payload={
                    "path": "/public/home/alice/job.sbatch",
                    "owner": "alice",
                    "content": "PRIVATE=secret\n",
                    "stdin": "SECRET_STDIN",
                    "token": "secret-token",
                },
                error=None,
                duration_ms=1.25,
            )

            record = json.loads(audit_path.read_text(encoding="utf-8"))

        self.assertEqual(record["request_id"], "req-1")
        self.assertEqual(record["request"]["path"], "/public/home/alice/job.sbatch")
        self.assertEqual(record["request"]["content_bytes"], len("PRIVATE=secret\n"))
        serialized = json.dumps(record, sort_keys=True)
        self.assertNotIn("PRIVATE=secret", serialized)
        self.assertNotIn("SECRET_STDIN", serialized)
        self.assertNotIn("secret-token", serialized)


if __name__ == "__main__":
    unittest.main()
