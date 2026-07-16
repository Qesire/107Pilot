import argparse
import tempfile
import unittest
from pathlib import Path

from pilot107.api.dev_server import _config_with_cli_overrides
from pilot107.api.service import config_from_env


class ApiDevServerConfigTests(unittest.TestCase):
    def test_cli_overrides_preserve_environment_only_capabilities(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            env_config = config_from_env(
                {
                    "PILOT107_TEMPLATE_REVIEWERS": "reviewer",
                    "PILOT107_TEMPLATE_VERIFICATION_ENVIRONMENT": "docker",
                    "PILOT107_CONTRACT_PROFILE": "real107-sim",
                    "PILOT107_SLURM_USER_NAME": "alice",
                },
                project_root=root,
            )
            config = _config_with_cli_overrides(
                env_config,
                argparse.Namespace(
                    db_path=root / "cli.db",
                    evidence_root=root / "cli-evidence",
                    backend="demo",
                    allowed_roots=None,
                    command_timeout_seconds=None,
                    slurmrestd_url=None,
                    slurm_api_version=None,
                    slurm_token=None,
                    auth_required=True,
                    trusted_user_header=None,
                ),
            )

        self.assertEqual(config.db_path, root / "cli.db")
        self.assertEqual(config.backend, "demo")
        self.assertTrue(config.auth_required)
        self.assertEqual(config.template_reviewers, frozenset({"reviewer"}))
        self.assertEqual(config.template_verification_environment, "docker")
        self.assertEqual(config.contract_profile, "real107-sim")
        self.assertEqual(config.slurm_username, "alice")


if __name__ == "__main__":
    unittest.main()
