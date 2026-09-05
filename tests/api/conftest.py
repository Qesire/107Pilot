"""Shared fixtures for tests/api/."""

from __future__ import annotations

import pytest


@pytest.fixture()
def cpu_rc_env(tmp_path, monkeypatch):
    """Minimal CPU-RC env for build_api_service."""
    monkeypatch.setenv("PILOT107_ENV", "cpu-rc")
    monkeypatch.setenv("PILOT107_HTTP_PORT", "8080")
    monkeypatch.setenv("PILOT107_HTTPS_PORT", "8443")
    monkeypatch.setenv("PILOT107_DB_PATH", str(tmp_path / "pilot107.db"))
    monkeypatch.setenv("PILOT107_EVIDENCE_ROOT", str(tmp_path / "evidence"))
    monkeypatch.setenv("PILOT107_CAPSULE_ROOT", str(tmp_path / "capsules"))
    monkeypatch.setenv("PILOT107_WORKER_METRICS_ROOT", str(tmp_path / "worker-metrics"))
    monkeypatch.setenv("PILOT107_PUBLIC_ROOT", str(tmp_path / "public"))
    monkeypatch.setenv("PILOT107_RECIPE_TEMPLATE_DIR", "")
    monkeypatch.setenv("PILOT107_CONTRACT_PROFILE", "cpu-only")
    monkeypatch.setenv("PILOT107_CAPABILITY_PROFILE_PATH", "")
    monkeypatch.setenv("PILOT107_JWT_SECRET", "test-secret")
    monkeypatch.setenv("PILOT107_GATEWAY_HMAC_SECRET", "test-gateway-secret")
    monkeypatch.setenv("PILOT107_REST_TOKEN_PROVIDER", "0")
    monkeypatch.setenv("PILOT107_AGENTD_URL", "")
    monkeypatch.setenv("PILOT107_AGENTD_TOKEN", "")
    monkeypatch.setenv("PILOT107_AGENTD_MODEL_PROFILE", "")
    yield
