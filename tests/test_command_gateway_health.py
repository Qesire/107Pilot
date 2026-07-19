import importlib.util
import io
import json
import unittest
from pathlib import Path
from unittest import mock

_GATEWAY_PATH = (
    Path(__file__).resolve().parent.parent
    / "simulator"
    / "compose"
    / "scripts"
    / "command-gateway.py"
)
_spec = importlib.util.spec_from_file_location("command_gateway", _GATEWAY_PATH)
assert _spec is not None and _spec.loader is not None
command_gateway = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(command_gateway)


def _request(handler_class, method: str, path: str) -> tuple[int, dict]:
    instance = handler_class.__new__(handler_class)

    captured: dict = {}

    instance.client_address = ("127.0.0.1", 0)
    instance.request_version = "HTTP/1.1"
    instance.command = method
    instance.path = path
    instance.headers = {}

    def send_response(status: int, _message: str | None = None) -> None:
        captured["status"] = status

    def send_header(_name: str, _value: str) -> None:
        return

    def end_headers() -> None:
        return

    def write(body: bytes) -> None:
        captured["body"] = body

    instance.send_response = send_response  # type: ignore[assignment]
    instance.send_header = send_header  # type: ignore[assignment]
    instance.end_headers = end_headers  # type: ignore[assignment]
    instance.wfile = io.BytesIO()
    instance.wfile.write = write  # type: ignore[assignment]
    instance.rfile = io.BytesIO()

    instance.do_GET()
    return captured["status"], json.loads(captured["body"].decode("utf-8"))


class _Completed:
    def __init__(self, returncode: int, stdout: str, stderr: str) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class CommandGatewayHealthTests(unittest.TestCase):
    def _handler(self) -> type:
        config = command_gateway.GatewayConfig(token=None, allowed_roots=[])
        return command_gateway.make_handler(config)

    def test_healthz_returns_ok(self) -> None:
        status, body = _request(self._handler(), "GET", "/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"status": "ok"})

    def test_health_ready_returns_200_when_slurm_healthy(self) -> None:
        def fake_run(argv, **kwargs):
            if argv[0] == "scontrol":
                return _Completed(0, "Slurmctld(primary) is UP\n", "")
            if argv[0] == "sinfo":
                return _Completed(0, "normal\n", "")
            return _Completed(1, "", "unknown")

        with mock.patch.object(command_gateway.subprocess, "run", side_effect=fake_run):
            status, body = _request(self._handler(), "GET", "/health/ready")

        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ready")
        checks = {item["name"]: item for item in body["checks"]}
        self.assertEqual(checks["scontrol_ping"]["status"], "ok")
        self.assertEqual(checks["sinfo_partitions"]["status"], "ok")

    def test_health_ready_returns_503_when_scontrol_fails(self) -> None:
        def fake_run(argv, **kwargs):
            if argv[0] == "scontrol":
                return _Completed(1, "", "Slurmctld is DOWN")
            if argv[0] == "sinfo":
                return _Completed(0, "normal\n", "")
            return _Completed(1, "", "unknown")

        with mock.patch.object(command_gateway.subprocess, "run", side_effect=fake_run):
            status, body = _request(self._handler(), "GET", "/health/ready")

        self.assertEqual(status, 503)
        self.assertEqual(body["status"], "not_ready")
        checks = {item["name"]: item for item in body["checks"]}
        self.assertEqual(checks["scontrol_ping"]["status"], "fail")
        self.assertEqual(checks["sinfo_partitions"]["status"], "ok")

    def test_health_ready_returns_503_when_sinfo_empty(self) -> None:
        def fake_run(argv, **kwargs):
            if argv[0] == "scontrol":
                return _Completed(0, "Slurmctld(primary) is UP\n", "")
            if argv[0] == "sinfo":
                return _Completed(0, "", "")
            return _Completed(1, "", "unknown")

        with mock.patch.object(command_gateway.subprocess, "run", side_effect=fake_run):
            status, body = _request(self._handler(), "GET", "/health/ready")

        self.assertEqual(status, 503)
        checks = {item["name"]: item for item in body["checks"]}
        self.assertEqual(checks["sinfo_partitions"]["status"], "fail")

    def test_health_ready_returns_503_when_binary_missing(self) -> None:
        def fake_run(argv, **kwargs):
            raise FileNotFoundError(f"[Errno 2] No such file: '{argv[0]}'")

        with mock.patch.object(command_gateway.subprocess, "run", side_effect=fake_run):
            status, body = _request(self._handler(), "GET", "/health/ready")

        self.assertEqual(status, 503)
        checks = {item["name"]: item for item in body["checks"]}
        self.assertEqual(checks["scontrol_ping"]["status"], "fail")
        self.assertEqual(checks["sinfo_partitions"]["status"], "fail")

    def test_health_ready_returns_503_on_timeout(self) -> None:
        import subprocess as subprocess_module

        def fake_run(argv, **kwargs):
            raise subprocess_module.TimeoutExpired(cmd=argv, timeout=3)

        with mock.patch.object(command_gateway.subprocess, "run", side_effect=fake_run):
            status, body = _request(self._handler(), "GET", "/health/ready")

        self.assertEqual(status, 503)
        checks = {item["name"]: item for item in body["checks"]}
        self.assertEqual(checks["scontrol_ping"]["status"], "fail")
        self.assertTrue(checks["scontrol_ping"]["detail"])

    def test_health_ready_detail_truncated_to_200_chars(self) -> None:
        long_stderr = "x" * 1000

        def fake_run(argv, **kwargs):
            if argv[0] == "scontrol":
                return _Completed(1, "", long_stderr)
            if argv[0] == "sinfo":
                return _Completed(0, "normal\n", "")
            return _Completed(1, "", "unknown")

        with mock.patch.object(command_gateway.subprocess, "run", side_effect=fake_run):
            status, body = _request(self._handler(), "GET", "/health/ready")

        self.assertEqual(status, 503)
        checks = {item["name"]: item for item in body["checks"]}
        self.assertLessEqual(len(checks["scontrol_ping"]["detail"]), 200)


if __name__ == "__main__":
    unittest.main()
