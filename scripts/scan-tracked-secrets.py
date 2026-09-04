#!/usr/bin/env python3
"""Fail on high-confidence credential material in release candidate files."""

from __future__ import annotations

import hashlib
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

# Deliberate negative-test/documentation fixtures are allowed only by exact
# path + exact line digest.  Editing even one byte makes the scanner fail again,
# so this cannot silently turn into a file-level or rule-level exception.
AUDITED_FIXTURE_LINE_SHA256 = frozenset(
    {
        (
            "docs/superpowers/plans/2026-08-10-pilot-agentd-a0.md",
            "d8f4b485b4bc34858db088794947905f0d1cc9a3efffa2832583fc5f72545ef5",
        ),
        (
            "services/pilot-agentd/tests/config.test.ts",
            "d8f4b485b4bc34858db088794947905f0d1cc9a3efffa2832583fc5f72545ef5",
        ),
        (
            "services/pilot-agentd/tests/config.test.ts",
            "c4a13a7e03d5b87a57728d0c950acac4603a58e296022c86d15c94229aacfe3e",
        ),
        (
            "services/pilot-agentd/tests/config.test.ts",
            "e22478a5e35b5e64a1fb6f7e9bc1981004f5b9f4db5916fa651d5a8c130d0fe7",
        ),
        (
            "services/pilot-agentd/tests/models.test.ts",
            "d8f4b485b4bc34858db088794947905f0d1cc9a3efffa2832583fc5f72545ef5",
        ),
        (
            "services/pilot-agentd/tests/server.test.ts",
            "e2e0dc4d049dd8b16af13291bc3a77f8ac000fcf620a7efc68a70c3a68ea9e78",
        ),
        (
            "tests/agent/test_client.py",
            "fa29f884f5388b54c8ecda831889fc3305c0693b5717da85eec2e4c1b82e94fd",
        ),
    }
)


def candidate_paths(root: Path) -> list[Path]:
    output = subprocess.check_output(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=root,
    )
    return [root / item.decode("utf-8") for item in output.split(b"\0") if item]


def _audited_fixture_line(relative: str, line: str) -> bool:
    digest = hashlib.sha256(line.encode("utf-8")).hexdigest()
    return (relative, digest) in AUDITED_FIXTURE_LINE_SHA256


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
            if ALLOW_MARKER in line or _audited_fixture_line(relative, line):
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
