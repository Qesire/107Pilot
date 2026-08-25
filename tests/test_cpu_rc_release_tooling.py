from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_cpu_rc_bundle_includes_agentd_in_every_binding_stage() -> None:
    for path in (
        "scripts/build-cpu-rc-images.sh",
        "scripts/export-cpu-rc-bundle.sh",
        "scripts/import-cpu-rc-images.sh",
        "scripts/verify-cpu-rc-image-binding.sh",
    ):
        assert "pilot107/agentd:cpu-rc-$" in (ROOT / path).read_text()


def test_cpu_rc_binding_requires_all_eleven_services() -> None:
    verifier = (ROOT / "scripts/verify-cpu-rc-image-binding.sh").read_text()
    acceptance = (ROOT / "scripts/accept-runtime-bundle.sh").read_text()

    assert "pilot-agentd" in verifier
    assert "pilot-agentd" in acceptance


def test_systemd_installer_exit_trap_expands_the_temp_path() -> None:
    script = (ROOT / "scripts/install-systemd-units.sh").read_text()

    assert "trap 'rm -f \"$tmp\"' EXIT" not in script


def test_agentd_binding_requires_its_revision_tagged_manifest_record(tmp_path: Path) -> None:
    revision = "a" * 40
    digest = f"sha256:{'b' * 64}"
    manifest = tmp_path / "RELEASE_MANIFEST.json"
    manifest.write_text(
        json.dumps(
            {
                "release_revision": revision,
                "images": [
                    {
                        "reference": f"pilot107/api:cpu-rc-{revision[:12]}",
                        "content_digest": digest,
                    }
                ],
            }
        )
    )

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker = fake_bin / "docker"
    docker.write_text(
        """#!/usr/bin/env python3
import json
import sys

services = (
    "mariadb", "slurmdbd", "slurmctld", "worker-1", "slurmrestd",
    "pilot107-command-gateway", "pilot-agentd", "pilot107-api",
    "pilot107-worker", "pilot107-web", "pilot107-reverse-proxy",
)
if len(sys.argv) > 1 and sys.argv[1] == "compose":
    for service in services:
        print(json.dumps({
            "Service": service,
            "Name": f"test-{service}-1",
            "ID": f"cid-{service}",
            "State": "running",
        }))
    raise SystemExit(0)

format_value = sys.argv[sys.argv.index("--format") + 1]
service = sys.argv[-1].removeprefix("cid-")
if format_value == "{{.State.Running}}":
    print("true")
elif format_value == "{{.Image}}":
    print("sha256:" + "b" * 64)
elif format_value == "{{.Config.Image}}":
    if service == "pilot-agentd":
        print("pilot107/agentd:cpu-rc-" + "a" * 12)
    else:
        print("pilot107/runtime:" + service)
else:
    raise SystemExit(2)
"""
    )
    docker.chmod(0o755)

    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    env["PILOT107_RELEASE_MANIFEST_PATH"] = str(manifest)
    completed = subprocess.run(
        ["bash", str(ROOT / "scripts/verify-cpu-rc-image-binding.sh")],
        capture_output=True,
        check=False,
        env=env,
        text=True,
    )

    assert completed.returncode == 1
    report = json.loads(completed.stdout)
    agentd = next(item for item in report["running_images"] if item["service"] == "pilot-agentd")
    assert agentd["matches_manifest"] is False


def test_export_rejects_untracked_agentd_source(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    git = fake_bin / "git"
    git.write_text(
        """#!/usr/bin/env bash
if [[ "$*" == *"rev-parse HEAD"* ]]; then
  printf '%s\n' aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
elif [[ "$*" == *"status"* && "$*" == *"services"* ]]; then
  printf '%s\n' '?? services/pilot-agentd/injected.js'
fi
"""
    )
    git.chmod(0o755)

    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    env["PILOT107_BUNDLE_DIR"] = str(tmp_path / "bundles")
    env["PILOT107_SKIP_BUILD"] = "1"
    completed = subprocess.run(
        ["bash", str(ROOT / "scripts/export-cpu-rc-bundle.sh")],
        capture_output=True,
        check=False,
        env=env,
        text=True,
    )

    assert completed.returncode == 1
    assert "refusing to export a bundle with untracked files" in completed.stderr
    assert "services/pilot-agentd/injected.js" in completed.stderr
