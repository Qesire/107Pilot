import unittest
from unittest.mock import patch

from pilot107.adapters.platform_cli import (
    ExecutorPlatformCliCollector,
    PlatformCliCollector,
    PlatformCommand,
    PlatformCommandSpec,
    default_login_snapshot_specs,
    user_entitlement_snapshot_specs,
)
from pilot107.adapters.slurm import CommandResult, SlurmTransportError


class PlatformCliTests(unittest.TestCase):
    def test_default_specs_are_structured_allowlisted_argv(self) -> None:
        specs = default_login_snapshot_specs(username="alice", home="/public/home/alice")
        by_name = {spec.name: spec for spec in specs}

        self.assertEqual(
            by_name[PlatformCommand.SQUEUE_USER_PIPE].argv[:4],
            ("squeue", "-h", "-u", "alice"),
        )
        self.assertIn(PlatformCommand.SCONTROL_SHOW_PART, by_name)
        self.assertIn(PlatformCommand.DF_PUBLIC_HOME, by_name)
        self.assertIn(PlatformCommand.CONDA_ENV_LIST_JSON, by_name)
        for spec in specs:
            self.assertIsInstance(spec.argv, tuple)
            self.assertNotIn(";", spec.argv)

    @patch("pilot107.adapters.platform_cli.subprocess.run")
    def test_collector_uses_subprocess_without_shell(self, run_mock) -> None:
        run_mock.return_value.returncode = 0
        run_mock.return_value.stdout = "ok\n"
        run_mock.return_value.stderr = ""
        collector = PlatformCliCollector(max_output_chars=10)

        result = collector.run(PlatformCommandSpec(PlatformCommand.HOSTNAME, ("hostname",)))

        self.assertEqual(result.stdout, "ok\n")
        _, kwargs = run_mock.call_args
        self.assertNotIn("shell", kwargs)
        self.assertEqual(kwargs["env"]["LC_ALL"], "C")

    def test_truncates_output(self) -> None:
        collector = PlatformCliCollector(max_output_chars=4)

        text, truncated = collector._truncate("abcdef")

        self.assertEqual(text, "abcd")
        self.assertTrue(truncated)

    def test_rejects_argv_that_does_not_match_allowlisted_command(self) -> None:
        with self.assertRaises(ValueError):
            PlatformCommandSpec(PlatformCommand.HOSTNAME, ("rm", "-rf", "/"))

    def test_user_entitlement_spec_is_exact_and_validates_username(self) -> None:
        spec = user_entitlement_snapshot_specs("alice")[0]

        self.assertEqual(spec.name, PlatformCommand.SACCTMGR_USER_ASSOC_PIPE)
        self.assertEqual(spec.argv[4], "name=alice")
        self.assertEqual(spec.argv[5], "WithAssoc")
        self.assertEqual(
            spec.argv[6],
            "format=User,DefaultAccount,Account,Partition,QOS,DefaultQOS",
        )
        with self.assertRaises(ValueError):
            user_entitlement_snapshot_specs("alice;rm")

    @patch("pilot107.adapters.platform_cli.subprocess.run")
    def test_missing_optional_command_becomes_observation(self, run_mock) -> None:
        run_mock.side_effect = FileNotFoundError("conda")
        collector = PlatformCliCollector()

        result = collector.run(
            PlatformCommandSpec(
                PlatformCommand.CONDA_ENV_LIST_JSON,
                ("conda", "env", "list", "--json"),
            )
        )

        self.assertEqual(result.returncode, 127)
        self.assertEqual(result.stderr, "command unavailable")

    def test_executor_collector_uses_structured_user_scoped_call(self) -> None:
        executor = FakeExecutor()
        collector = ExecutorPlatformCliCollector(
            executor=executor,  # type: ignore[arg-type]
            user="alice",
            cwd="/public/home/alice",
            max_output_chars=4,
        )

        result = collector.run(
            PlatformCommandSpec(PlatformCommand.HOSTNAME, ("hostname",))
        )

        self.assertEqual(executor.calls, [(["hostname"], "/public/home/alice", "alice", 10.0)])
        self.assertEqual(result.stdout, "logi")
        self.assertTrue(result.truncated)

    def test_executor_transport_failure_is_safe_observation(self) -> None:
        executor = FakeExecutor(error=SlurmTransportError("token=secret"))
        collector = ExecutorPlatformCliCollector(
            executor=executor,  # type: ignore[arg-type]
            user="alice",
            cwd="/public/home/alice",
        )

        result = collector.run(
            PlatformCommandSpec(PlatformCommand.HOSTNAME, ("hostname",))
        )

        self.assertEqual(result.returncode, 125)
        self.assertNotIn("secret", result.stderr)


class FakeExecutor:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[tuple[list[str], str | None, str | None, float]] = []

    def run(
        self,
        argv: list[str],
        *,
        cwd: str | None = None,
        user: str | None = None,
        stdin: str | None = None,
        timeout_seconds: float = 10.0,
    ) -> CommandResult:
        self.calls.append((argv, cwd, user, timeout_seconds))
        if self.error is not None:
            raise self.error
        return CommandResult(returncode=0, stdout="login-node\n", stderr="")


if __name__ == "__main__":
    unittest.main()
