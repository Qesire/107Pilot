from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_DIR = ROOT / "simulator" / "compose"


def _services(name: str) -> dict[str, dict[str, Any]]:
    payload = yaml.safe_load((COMPOSE_DIR / name).read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    services = payload.get("services")
    assert isinstance(services, dict)
    return services


def _compose(name: str) -> dict[str, Any]:
    payload = yaml.safe_load((COMPOSE_DIR / name).read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def _environment(service: dict[str, Any]) -> dict[str, Any]:
    environment = service.get("environment", {})
    assert isinstance(environment, dict)
    return environment


@pytest.mark.parametrize("compose_name", ["compose.yml", "compose.competition-app-node.yml"])
def test_only_agentd_receives_llm_configuration(compose_name: str) -> None:
    services = _services(compose_name)
    holders = {
        name
        for name, service in services.items()
        if any(key.startswith("PILOT107_LLM_") for key in _environment(service))
    }

    assert holders == {"pilot-agentd"}


@pytest.mark.parametrize("compose_name", ["compose.yml", "compose.competition-app-node.yml"])
def test_agentd_has_no_cluster_mount_or_host_port(compose_name: str) -> None:
    agentd = _services(compose_name)["pilot-agentd"]
    serialized_mounts = repr(agentd.get("volumes", [])).lower()

    assert agentd["image"] == "${PILOT107_AGENTD_IMAGE:-pilot107/agentd:local}"
    assert agentd["user"] == "10701:10701"
    assert agentd["read_only"] is True
    assert agentd["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in agentd["security_opt"]
    assert agentd.get("ports", []) == []
    assert "/public" not in serialized_mounts
    assert "ssh" not in serialized_mounts
    assert "slurm" not in serialized_mounts


@pytest.mark.parametrize("compose_name", ["compose.yml", "compose.competition-app-node.yml"])
def test_agentd_receives_only_private_tool_gateway_location(compose_name: str) -> None:
    environment = _environment(_services(compose_name)["pilot-agentd"])

    assert environment["PILOT107_AGENTD_TOOL_GATEWAY_URL"] == (
        "http://pilot107-api:8080/internal/v1/agent-tools/invoke"
    )
    for forbidden in (
        "PILOT107_AGENT_CAPABILITY_HMAC_SECRET_FILE",
        "PILOT107_POSTGRES_DSN",
        "PILOT107_SLURM_TOKEN",
    ):
        assert forbidden not in environment


@pytest.mark.parametrize("compose_name", ["compose.yml", "compose.competition-app-node.yml"])
def test_api_runtime_configures_the_required_outer_bwrap_boundary(
    compose_name: str,
) -> None:
    api = _services(compose_name)["pilot107-api"]

    assert api["user"] == "10700:10700"
    assert api["read_only"] is True
    assert api["cap_drop"] == ["ALL"]
    assert set(api["security_opt"]) == {
        "no-new-privileges:true",
        "seccomp=unconfined",
        "apparmor=bwrap",
        "systempaths=unconfined",
    }
    assert api["tmpfs"] == ["/tmp:rw,nosuid,nodev,size=64m,mode=1777"]


@pytest.mark.parametrize("compose_name", ["compose.yml", "compose.competition-app-node.yml"])
@pytest.mark.parametrize("service_name", ["pilot107-api", "pilot107-worker"])
def test_python_services_receive_agent_capability_secret_file(
    compose_name: str,
    service_name: str,
) -> None:
    compose = _compose(compose_name)
    service = compose["services"][service_name]

    assert _environment(service)["PILOT107_AGENT_CAPABILITY_HMAC_SECRET_FILE"] == (
        "/run/secrets/pilot107-agent-capability-hmac"
    )
    assert "pilot107-agent-capability-hmac" in service["secrets"]
    assert compose["secrets"]["pilot107-agent-capability-hmac"]["file"] == (
        "${PILOT107_AGENT_CAPABILITY_HMAC_SECRET_FILE:-"
        "./secrets/agent-capability-hmac.local}"
    )


def test_competition_overlay_keeps_a1_secret_and_gateway_boundary() -> None:
    services = _services("compose.competition.yml")

    assert _environment(services["pilot-agentd"])["PILOT107_AGENTD_TOOL_GATEWAY_URL"] == (
        "http://pilot107-api:8080/internal/v1/agent-tools/invoke"
    )
    for service_name in ("pilot107-api", "pilot107-worker"):
        service = services[service_name]
        assert _environment(service)["PILOT107_AGENT_CAPABILITY_HMAC_SECRET_FILE"] == (
            "/run/secrets/pilot107-agent-capability-hmac"
        )
        assert "pilot107-agent-capability-hmac" in service["secrets"]


@pytest.mark.parametrize("compose_name", ["compose.yml", "compose.competition-app-node.yml"])
@pytest.mark.parametrize("service_name", ["pilot107-api", "pilot107-worker"])
def test_python_services_receive_only_agentd_model_configuration(
    compose_name: str,
    service_name: str,
) -> None:
    service = _services(compose_name)[service_name]
    environment = _environment(service)

    assert environment["PILOT107_AGENTD_URL"] == "http://pilot-agentd:8091"
    assert "PILOT107_AGENTD_TOKEN" in environment
    assert "PILOT107_AGENTD_MODEL_PROFILE" in environment
    assert environment["PILOT107_AGENTD_TIMEOUT_SECONDS"] == (
        "${PILOT107_AGENTD_TIMEOUT_SECONDS:-60}"
    )
    assert environment["PILOT107_AGENTD_MAX_OUTPUT_TOKENS"] == (
        "${PILOT107_AGENTD_MAX_OUTPUT_TOKENS:-1200}"
    )
    assert not any(key.startswith("PILOT107_LLM_") for key in environment)
    assert service["depends_on"]["pilot-agentd"] == {"condition": "service_healthy"}


def test_competition_and_cpu_overrides_keep_agentd_in_the_selected_profile() -> None:
    competition = _services("compose.competition.yml")
    cpu = _services("compose.cpu-rc.yml")

    assert competition["pilot-agentd"]["profiles"] == ["competition"]
    assert competition["pilot107-api"]["depends_on"]["pilot-agentd"] == {
        "condition": "service_healthy"
    }
    assert competition["pilot107-worker"]["depends_on"]["pilot-agentd"] == {
        "condition": "service_healthy"
    }
    assert cpu["pilot-agentd"]["cpus"] == 0.5
    assert cpu["pilot-agentd"]["mem_limit"] == "768m"


@pytest.mark.parametrize(
    ("script_name", "expected_commands"),
    [
        (
            "build-app-images.sh",
            ["build -t pilot107/agentd:test -f", "services/pilot-agentd/Dockerfile"],
        ),
        (
            "check-app-images.sh",
            [
                "run --rm pilot107/agentd:test node --version",
                "run --rm pilot107/agentd:test node -e",
                "@earendil-works/pi-agent-core",
            ],
        ),
    ],
)
def test_app_image_scripts_include_agentd(
    tmp_path: Path,
    script_name: str,
    expected_commands: list[str],
) -> None:
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    docker_log = tmp_path / "docker.log"
    fake_docker = binary_dir / "docker"
    fake_docker.write_text(
        "#!/usr/bin/env sh\nprintf '%s\\n' \"$*\" >> \"$PILOT107_TEST_DOCKER_LOG\"\n",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)
    environment = {
        **os.environ,
        "PATH": f"{binary_dir}:{os.environ['PATH']}",
        "PILOT107_AGENTD_IMAGE": "pilot107/agentd:test",
        "PILOT107_TEST_DOCKER_LOG": str(docker_log),
    }

    subprocess.run(
        ["bash", str(ROOT / "scripts" / script_name)],
        cwd=ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )

    commands = docker_log.read_text(encoding="utf-8")
    for expected in expected_commands:
        assert expected in commands


@pytest.mark.parametrize(
    "env_name",
    [".env.example", ".env.competition.example", ".env.cpu-rc.example"],
)
def test_environment_templates_define_agentd_boundary(env_name: str) -> None:
    assignments = {
        line.split("=", 1)[0]: line.split("=", 1)[1]
        for line in (COMPOSE_DIR / env_name).read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#") and "=" in line
    }

    assert assignments["PILOT107_AGENTD_IMAGE"]
    assert "PILOT107_AGENTD_TOKEN" in assignments
    assert assignments["PILOT107_AGENTD_MODEL_PROFILE"] == "campus-default"
    assert assignments["PILOT107_AGENT_CAPABILITY_HMAC_SECRET_FILE"] == (
        "./secrets/agent-capability-hmac.local"
    )
    assert "PILOT107_LLM_STRUCTURED_OUTPUT_MODE" not in assignments


def test_local_secret_initializer_creates_distinct_agent_capability_secret() -> None:
    script = (ROOT / "scripts" / "init-local-secrets.sh").read_text(encoding="utf-8")

    assert "agent-capability-hmac.local" in script
    assert "proxy-hmac.local" in script
