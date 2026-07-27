from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from pilot107.adapters.slurm import CommandResult
from pilot107.adapters.ssh_relay import (
    FixedRemoteProgram,
    SshRelayCheck,
    SshRelayConfig,
    SshSessionState,
)
from pilot107.core.identity import UserIdentity
from pilot107.core.paths import PathPolicyError, SafePath
from pilot107.worker.evidence import EvidencePolicy
from pilot107.worker.ssh_evidence import (
    SSH_EVIDENCE_FS_PROGRAM,
    SshEvidenceTransport,
)


class LocalFixedProgramClient:
    def __init__(self, config: SshRelayConfig) -> None:
        self.config = config

    def check(self) -> SshRelayCheck:
        return SshRelayCheck(
            state=SshSessionState.ACTIVE,
            checked_at="2026-07-26T00:00:00+00:00",
            status_code="SSH.ACTIVE",
            message="active",
        )

    def execute(
        self,
        argv: tuple[str, ...],
        *,
        cwd: str | None = None,
        portal_owner: str,
        stdin: str | None = None,
        timeout_seconds: float | None = None,
    ) -> CommandResult:
        raise AssertionError((argv, cwd, portal_owner, stdin, timeout_seconds))

    def execute_fixed_program(
        self,
        program: FixedRemoteProgram,
        args: tuple[str, ...],
        *,
        portal_owner: str,
        timeout_seconds: float | None = None,
    ) -> CommandResult:
        assert program == FixedRemoteProgram.EVIDENCE_FS
        assert portal_owner == "alice"
        completed = subprocess.run(
            ["python3", "-c", SSH_EVIDENCE_FS_PROGRAM, *args],
            check=False,
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
        )
        return CommandResult(
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )


def build_transport(root: Path) -> SshEvidenceTransport:
    config = SshRelayConfig(
        connection_id="real107",
        target_id="real107",
        target="pilot107-slurm",
        control_path=root / "relay.sock",
        portal_owner="alice",
        slurm_user="alice",
        owner_roots=(str(root),),
    )
    return SshEvidenceTransport(client=LocalFixedProgramClient(config))


def safe(path: Path, root: Path) -> SafePath:
    return SafePath(original=str(path), resolved=path, root=root)


def test_ssh_evidence_tail_range_inventory_and_limits(tmp_path: Path) -> None:
    workdir = tmp_path / "project"
    workdir.mkdir()
    output = workdir / "result.txt"
    output.write_text("hello remote evidence\n", encoding="utf-8")
    (workdir / "slurm-12.out").write_text("excluded", encoding="utf-8")
    transport = build_transport(tmp_path)
    identity = UserIdentity(username="alice")

    stat = transport.stat(identity, safe(output, tmp_path))
    tail = transport.read_text_tail(identity, safe(output, tmp_path), max_bytes=8)
    data = transport.read_bytes_range(identity, safe(output, tmp_path), 0, 5)
    inventory = transport.inventory(
        identity,
        safe(workdir, tmp_path),
        EvidencePolicy(max_files=5, max_total_inventory_bytes=1024),
    )

    assert stat.kind == "regular file"
    assert tail.tail == "vidence\n"
    assert tail.truncated is True
    assert data == b"hello"
    assert [item.relative_path for item in inventory.files] == ["result.txt"]


def test_ssh_evidence_rejects_symlink_escape_and_wrong_identity(tmp_path: Path) -> None:
    workdir = tmp_path / "project"
    workdir.mkdir()
    link = workdir / "passwd"
    link.symlink_to("/etc/passwd")
    transport = build_transport(tmp_path)

    with pytest.raises(PathPolicyError):
        transport.stat(UserIdentity(username="alice"), safe(link, tmp_path))
    with pytest.raises(Exception, match="owner mismatch"):
        transport.stat(UserIdentity(username="bob"), safe(workdir, tmp_path))

