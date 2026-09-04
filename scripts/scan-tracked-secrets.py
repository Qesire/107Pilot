#!/usr/bin/env python3
"""Fail on high-confidence credential material in release candidate files."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

PATTERNS = {
    "private-key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "aws-access-key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "github-token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),
    "slack-token": re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
    "openai-style-key": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    "embedded-url-password": re.compile(r"https?://[^\s/:]+:[^\s/@]+@"),
    "literal-deployment-secret": re.compile(
        r"^\s*PILOT107_(?:PROXY_HMAC_SECRET|LLM_API_KEY)\s*:\s*[\"']?"
        r"(?!\$\{|/run/secrets|[\"']?$)[^\s#]+"
    ),
}
SKIP_PREFIXES = (
    "src/pilot107/web/static/assets/",
    "artifacts/",
)
SKIP_NAMES = {"package-lock.json", "uv.lock"}
ALLOW_MARKER = "secret-scan: allow"

# These values are deliberately fake credentials used to assert redaction and
# unsafe-URL rejection.  The allowlist binds each literal to the exact source
# file that owns the fixture; an identical-looking value in any other file is
# still reported.  Keep this list small and review additions as security
# changes rather than excluding entire test or documentation directories.
SYNTHETIC_FIXTURES: dict[str, tuple[str, ...]] = {
    "docs/superpowers/plans/2026-08-10-pilot-agentd-a0.md": (
        'PILOT107_LLM_API_KEY: "llm-secret"',
    ),
    "services/pilot-agentd/tests/config.test.ts": (
        'PILOT107_LLM_API_KEY: "llm-secret"',
        "http://student:secret@pilot107-api/internal/v1/agent-tools/invoke",
        "https://student:password@gateway.example.edu/v1",
    ),
    "services/pilot-agentd/tests/models.test.ts": (
        'PILOT107_LLM_API_KEY: "llm-secret"',
    ),
    "services/pilot-agentd/tests/server.test.ts": (
        'PILOT107_LLM_API_KEY: "secret-api-key"',
    ),
    "tests/agent/test_client.py": (
        "http://user:password@agentd:8091",
    ),
}


def candidate_paths(root: Path) -> list[Path]:
    output = subprocess.check_output(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=root,
    )
    return [root / item.decode("utf-8") for item in output.split(b"\0") if item]


def _is_synthetic_fixture(relative: str, line: str) -> bool:
    return any(literal in line for literal in SYNTHETIC_FIXTURES.get(relative, ()))


def findings(root: Path) -> list[tuple[str, int, str]]:
    found: list[tuple[str, int, str]] = []
    for path in candidate_paths(root):
        relative = path.relative_to(root).as_posix()
        if relative in SKIP_NAMES or relative.startswith(SKIP_PREFIXES) or not path.is_file():
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            continue
        for line_number, line in enumerate(lines, 1):
            if ALLOW_MARKER in line or _is_synthetic_fixture(relative, line):
                continue
            for name, pattern in PATTERNS.items():
                if pattern.search(line):
                    found.append((relative, line_number, name))
    return found


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    found = findings(root)
    if found:
        for path, line_number, name in found:
            print(f"{path}:{line_number}: potential {name}")
        return 1
    print("tracked and candidate release files contain no high-confidence secret patterns")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
