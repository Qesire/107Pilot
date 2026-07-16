"""Development server entrypoint for the minimal Phase 0A API."""

from __future__ import annotations

import argparse
import os
from dataclasses import replace
from pathlib import Path

from pilot107.api.http_app import run_http_server
from pilot107.api.service import ApiServiceConfig, build_api_service, config_from_env


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the minimal 107Pilot HTTP API.")
    parser.add_argument("--db-path", type=Path, required=True)
    parser.add_argument("--evidence-root", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8070)
    parser.add_argument("--auth-required", action="store_true")
    parser.add_argument("--trusted-user-header")
    parser.add_argument(
        "--backend",
        choices=[
            "none",
            "in-memory",
            "demo",
            "rest-native",
            "command",
            "docker-compose-command",
            "command-gateway",
        ],
    )
    parser.add_argument("--allowed-roots")
    parser.add_argument("--command-timeout-seconds", type=float)
    parser.add_argument("--slurmrestd-url")
    parser.add_argument("--slurm-api-version")
    parser.add_argument("--slurm-token")
    args = parser.parse_args()

    env_config = config_from_env(os.environ)
    api = build_api_service(_config_with_cli_overrides(env_config, args))
    run_http_server(
        api=api,
        host=args.host,
        port=args.port,
    )
    return 0


def _config_with_cli_overrides(
    env_config: ApiServiceConfig,
    args: argparse.Namespace,
) -> ApiServiceConfig:
    """Apply CLI transport overrides without dropping environment-only capabilities."""
    return replace(
        env_config,
        db_path=args.db_path,
        evidence_root=args.evidence_root,
        backend=args.backend or env_config.backend,
        allowed_roots=(
            tuple(_split_csv(args.allowed_roots))
            if args.allowed_roots
            else env_config.allowed_roots
        ),
        command_timeout_seconds=(
            args.command_timeout_seconds or env_config.command_timeout_seconds
        ),
        slurmrestd_url=args.slurmrestd_url or env_config.slurmrestd_url,
        slurm_api_version=args.slurm_api_version or env_config.slurm_api_version,
        slurm_token=args.slurm_token or env_config.slurm_token,
        auth_required=args.auth_required or env_config.auth_required,
        trusted_user_header=args.trusted_user_header or env_config.trusted_user_header,
    )


def _split_csv(value: str) -> list[str]:
    items = [item.strip() for item in value.split(",")]
    return [item for item in items if item]


if __name__ == "__main__":
    raise SystemExit(main())
