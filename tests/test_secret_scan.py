from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_scanner():
    path = Path(__file__).resolve().parents[1] / "scripts" / "scan-tracked-secrets.py"
    spec = importlib.util.spec_from_file_location("pilot107_secret_scan", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _url(user: str, password: str, host: str) -> str:
    return "http" + "://" + user + ":" + password + "@" + host


def test_synthetic_fixture_allowlist_is_path_scoped() -> None:
    scanner = _load_scanner()
    line = '  PILOT107_LLM_API_KEY: "llm-secret",'

    assert scanner._is_synthetic_fixture(
        "services/pilot-agentd/tests/config.test.ts", line
    )
    assert not scanner._is_synthetic_fixture("src/pilot107/config.py", line)


def test_embedded_password_fixture_is_exact_literal_scoped() -> None:
    scanner = _load_scanner()
    path = "tests/agent/test_client.py"
    approved = _url("user", "password", "agentd:8091")
    unapproved = _url("alice", "real-secret", "agentd:8091")

    assert scanner._is_synthetic_fixture(path, f'({{"base_url": "{approved}"}}, "name")')
    assert not scanner._is_synthetic_fixture(
        path, f'({{"base_url": "{unapproved}"}}, "name")'
    )


def test_high_confidence_pattern_still_matches_unapproved_secret() -> None:
    scanner = _load_scanner()
    unapproved = _url("alice", "real-secret", "example.edu/api")

    assert scanner.PATTERNS["embedded-url-password"].search(f'url = "{unapproved}"')
