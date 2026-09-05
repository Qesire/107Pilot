from __future__ import annotations

import json
import shlex
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from pilot107.adapters.slurm import (
    CommandResult,
    SshSlurmBackend,
    SubmitIntent,
)
from pilot107.adapters.ssh_relay import (
    FixedRemoteProgram,
    SshRelayCheck,
    SshRelayConfig,
    SshRelayExecutor,
    SshRelayPolicyError,
    SshSessionState,
    SubprocessSshRelayClient,
)
from pilot107.api.http_app import build_api
from pilot107.api.service import config_from_env as api_config_from_env
from pilot107.core.resources import ResourcePlan
from pilot107.core.ssh_connections import SshConnectionService, SshConnectionStore
from pilot107.worker.runtime_worker import (
    WorkerErrorCode,
    classify_worker_exception,
)
from pilot107.worker.service import config_from_env as worker_config_from_env


def relay_config(tmp_path: Path) -> SshRelayConfig:
    return SshRelayConfig(
        connection_id="real107",
        target_id="real107",
        target="pilot107-slurm",
        control_path=tmp_path / "relay.sock",
        portal_owner="alice",
        slurm_user="alice",
        owner_roots=("/public/home/{user}",),
        timeout_seconds=5,
    )


def completed(
    returncode: int,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["ssh"],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


def test_subprocess_relay_checks_master_and_quotes_structured_argv(tmp_path: Path) -> None:
    client = SubprocessSshRelayClient(relay_config(tmp_path))
    with patch(
        "pilot107.adapters.ssh_relay.subprocess.run",
        side_effect=[
            completed(0, "Master running\n"),
            completed(0, "42\n"),
        ],
    ) as run:
        result = client.execute(
            (
                "squeue",
                "-h",
                "-j",
                "42",
                "-o",
                "%i|%u|%T|%R",
            ),
            cwd="/public/home/alice/project with space",
            portal_owner="alice",
        )

    assert result.stdout == "42\n"
    remote_command = run.call_args_list[1].args[0][-1]
    assert remote_command.startswith("cd -- '/public/home/alice/project with space' && exec squeue")
    assert "BatchMode=yes" in run.call_args_list[1].args[0]


def test_subprocess_relay_allows_only_batched_readonly_sstat_shape(tmp_path: Path) -> None:
    client = SubprocessSshRelayClient(relay_config(tmp_path))
    fields = "JobID,NTasks,AllocTRES,AveCPU,MaxRSS,TRESUsageInTot,TRESUsageOutTot"
    with patch(
        "pilot107.adapters.ssh_relay.subprocess.run",
        side_effect=[completed(0, "Master running\n"), completed(0, "")],
    ) as run:
        client.execute(
            (
                "sstat",
                "-nP",
                "--allsteps",
                "-j",
                "101,102",
                "-o",
                fields,
            ),
            portal_owner="alice",
        )

    assert run.call_args_list[1].args[0][-1] == ("sstat -nP --allsteps -j 101,102 -o " + fields)
    with pytest.raises(SshRelayPolicyError, match="unsupported sstat flag"):
        client.execute(("sstat", "--help"), portal_owner="alice")


def test_subprocess_relay_allows_only_fixed_observability_platform_probes(
    tmp_path: Path,
) -> None:
    client = SubprocessSshRelayClient(relay_config(tmp_path))
    allowed = (
        ("scontrol", "show", "config"),
        ("sinfo", "-h", "-o", "%P|%c|%m|%G|%T"),
    )
    with patch(
        "pilot107.adapters.ssh_relay.subprocess.run",
        side_effect=[
            completed(0, "Master running\n"),
            completed(0, ""),
            completed(0, "Master running\n"),
            completed(0, ""),
        ],
    ):
        for argv in allowed:
            client.execute(argv, portal_owner="alice")

    with pytest.raises(SshRelayPolicyError):
        client.execute(("scontrol", "show", "secrets"), portal_owner="alice")
    with pytest.raises(SshRelayPolicyError):
        client.execute(("sinfo", "-R"), portal_owner="alice")


def test_subprocess_relay_is_fail_closed_for_owner_command_and_path(tmp_path: Path) -> None:
    client = SubprocessSshRelayClient(relay_config(tmp_path))
    with pytest.raises(SshRelayPolicyError, match="owner mismatch"):
        client.execute(("whoami",), portal_owner="bob")
    with pytest.raises(SshRelayPolicyError, match="unsupported"):
        client.execute(("bash", "-c", "id"), portal_owner="alice")
    with pytest.raises(SshRelayPolicyError, match="outside"):
        client.execute(
            ("stat", "-c", "%s|%Y", "--", "/etc/passwd"),
            portal_owner="alice",
        )


def test_subprocess_relay_reports_auth_required_without_interactive_retry(
    tmp_path: Path,
) -> None:
    client = SubprocessSshRelayClient(relay_config(tmp_path))
    with patch(
        "pilot107.adapters.ssh_relay.subprocess.run",
        return_value=completed(255, stderr="Control socket connect: No such file"),
    ):
        check = client.check()
    assert check.state == SshSessionState.AUTH_REQUIRED
    assert check.status_code == "SSH.AUTH_REQUIRED"


class FakeRelayClient:
    def __init__(self, config: SshRelayConfig) -> None:
        self.config = config
        self.commands: list[tuple[str, ...]] = []
        self.writes: list[tuple[str, str]] = []

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
        assert portal_owner == "alice"
        self.commands.append(argv)
        if argv[:3] == ("realpath", "-m", "--"):
            return CommandResult(0, f"{argv[3]}\n", "")
        if argv[0] == "tee":
            self.writes.append((argv[2], stdin or ""))
            return CommandResult(0, stdin or "", "")
        if argv[0] in {"chmod", "mkdir", "scancel"}:
            return CommandResult(0, "", "")
        if argv[0] == "sbatch":
            return CommandResult(0, "321;cluster\n", "")
        if argv[0] == "squeue":
            return CommandResult(0, "321|alice|RUNNING|None\n", "")
        if argv[0] == "sacct" and "-u" in argv:
            marker = argv[argv.index("-o") + 1]
            assert marker == "JobIDRaw,User,JobName"
            return CommandResult(0, "321|alice|pilot107-run-abc\n", "")
        if argv[0] == "sacct":
            return CommandResult(0, "321|alice|COMPLETED|0:0\n", "")
        raise AssertionError(argv)

    def execute_fixed_program(
        self,
        program: FixedRemoteProgram,
        args: tuple[str, ...],
        *,
        portal_owner: str,
        timeout_seconds: float | None = None,
    ) -> CommandResult:
        raise AssertionError((program, args, portal_owner))


class FileManifestRelayClient(FakeRelayClient):
    def __init__(self, config: SshRelayConfig) -> None:
        super().__init__(config)
        self.file_shell: str | None = None

    def run_file_shell(
        self,
        shell_command: str,
        *,
        stdin: str | None = None,
        timeout_seconds: float | None = None,
    ) -> CommandResult:
        self.file_shell = shell_command
        return CommandResult(
            0,
            json.dumps(
                {
                    "entries": [
                        {
                            "name": "control.sock",
                            "type": "socket",
                            "size": 0,
                            "mtime": 1,
                        }
                    ],
                    "has_more": False,
                    "directory_revision": "revision-1",
                }
            )
            + "\n",
            "",
        )


class PublicationRelayClient(FakeRelayClient):
    def __init__(self, config: SshRelayConfig, outputs: list[str]) -> None:
        super().__init__(config)
        self.outputs = outputs
        self.file_shell_commands: list[str] = []

    def run_file_shell(
        self,
        shell_command: str,
        *,
        stdin: str | None = None,
        timeout_seconds: float | None = None,
    ) -> CommandResult:
        del stdin, timeout_seconds
        self.file_shell_commands.append(shell_command)
        return CommandResult(0, self.outputs.pop(0), "")


class SearchRelayClient(FakeRelayClient):
    def __init__(self, config: SshRelayConfig, outputs: list[dict[str, object]]) -> None:
        super().__init__(config)
        self.outputs = outputs
        self.file_shell_commands: list[str] = []

    def run_file_shell(
        self,
        shell_command: str,
        *,
        stdin: str | None = None,
        timeout_seconds: float | None = None,
    ) -> CommandResult:
        del stdin, timeout_seconds
        self.file_shell_commands.append(shell_command)
        return CommandResult(0, json.dumps(self.outputs.pop(0)) + "\n", "")


def test_ssh_publication_relay_exposes_typed_digest_and_compare_swap(tmp_path: Path) -> None:
    client = PublicationRelayClient(
        relay_config(tmp_path),
        ["MISSING\n", "committed\n", "already_committed\n"],
    )
    relay = SshRelayExecutor(client)

    assert relay.path_sha256(path="/public/home/alice/project/new.py", owner="alice") is None
    assert (
        relay.compare_and_swap_file(
            staged_path="/public/home/alice/project/.107pilot/publish/c/new.py",
            target_path="/public/home/alice/project/new.py",
            expected_sha256=None,
            desired_sha256="a" * 64,
            owner="alice",
        )
        == "committed"
    )
    assert (
        relay.compare_and_delete_file(
            target_path="/public/home/alice/project/old.py",
            expected_sha256="b" * 64,
            owner="alice",
        )
        == "already_committed"
    )

    for command in client.file_shell_commands:
        tokens = shlex.split(command)
        assert tokens[:2] == ["python3", "-c"]
        compile(tokens[2], "<ssh-publication-operation>", "exec")


def test_ssh_publication_relay_rejects_unknown_protocol_status(tmp_path: Path) -> None:
    client = PublicationRelayClient(relay_config(tmp_path), ["surprise\n"])

    with pytest.raises(RuntimeError, match="SSH.PUBLICATION_PROTOCOL_INVALID"):
        SshRelayExecutor(client).compare_and_swap_file(
            staged_path="/public/home/alice/project/.107pilot/publish/c/main.py",
            target_path="/public/home/alice/project/main.py",
            expected_sha256="b" * 64,
            desired_sha256="a" * 64,
            owner="alice",
        )


def test_ssh_file_manifest_preserves_special_file_types(tmp_path: Path) -> None:
    client = FileManifestRelayClient(relay_config(tmp_path))

    page = SshRelayExecutor(client).list_dir(
        path="/public/home/alice/exp",
        owner="alice",
    )

    assert page.entries[0].type == "socket"
    assert page.has_more is False
    assert page.next_cursor is None
    assert client.file_shell is not None
    assert "stat.S_ISREG" in client.file_shell
    assert "stat.S_ISSOCK" in client.file_shell
    command = shlex.split(client.file_shell)
    compile(command[2], "<ssh-file-manifest>", "exec")


def test_ssh_file_search_uses_bounded_remote_projection_and_signed_cursor(
    tmp_path: Path,
) -> None:
    client = SearchRelayClient(
        relay_config(tmp_path),
        [
            {
                "items": [
                    {
                        "path": "/public/home/alice/models/a.bin",
                        "relative_path": "models/a.bin",
                        "type": "file",
                        "size": 7,
                        "mtime": 123,
                    }
                ],
                "incomplete": True,
                "stack": [{"relative_dir": "models", "index": 1}],
                "warnings": [],
            },
            {"items": [], "incomplete": False, "stack": [], "warnings": []},
        ],
    )
    executor = SshRelayExecutor(client)

    first = executor.search_files(
        root="/public/home/alice",
        q="model",
        kind="all",
        size_min=None,
        size_max=None,
        mtime_from=None,
        mtime_to=None,
        limit=20,
        cursor=None,
        scan_limit=1000,
        time_limit_ms=750,
        owner="alice",
    )
    second = executor.search_files(
        root="/public/home/alice",
        q="model",
        kind="all",
        size_min=None,
        size_max=None,
        mtime_from=None,
        mtime_to=None,
        limit=20,
        cursor=first.next_cursor,
        scan_limit=1000,
        time_limit_ms=750,
        owner="alice",
    )

    assert first.items[0].relative_path == "models/a.bin"
    assert first.next_cursor is not None
    assert second.next_cursor is None
    for command in client.file_shell_commands:
        tokens = shlex.split(command)
        assert tokens[:2] == ["python3", "-c"]
        compile(tokens[2], "<ssh-file-search>", "exec")

    with pytest.raises(RuntimeError, match="invalid search cursor"):
        executor.search_files(
            root="/public/home/alice",
            q="model",
            kind="all",
            size_min=None,
            size_max=None,
            mtime_from=None,
            mtime_to=None,
            limit=20,
            cursor=f"{first.next_cursor}tampered",
            scan_limit=1000,
            time_limit_ms=750,
            owner="alice",
        )


def resource_plan() -> ResourcePlan:
    return ResourcePlan(
        partition="cpu",
        qos=None,
        nodes=1,
        ntasks=1,
        cpus_per_task=1,
        time_limit="00:05:00",
        memory_value=1,
        memory_unit="G",
    )


def test_ssh_slurm_backend_materializes_private_run_directory_and_reconciles(
    tmp_path: Path,
) -> None:
    client = FakeRelayClient(relay_config(tmp_path))
    backend = SshSlurmBackend(
        executor=SshRelayExecutor(client),
        allowed_roots=["/public/home/alice"],
    )
    receipt = backend.submit(
        SubmitIntent(
            user="alice",
            workdir=Path("/public/home/alice/project"),
            script="#!/bin/bash\necho ok\n",
            resource_plan=resource_plan(),
            idempotency_key="run_abc:submit",
            job_name="pilot107-run-abc",
        )
    )

    assert receipt.job_id == "321"
    assert client.writes[0][0].endswith("/.107pilot/runs/run_abc/submission.sbatch")
    marker = json.loads(client.writes[1][1])
    assert marker["idempotency_key"] == "run_abc:submit"
    assert "script_sha256" in marker
    assert backend.find_jobs_by_marker(
        user="alice",
        job_name_marker="pilot107-run-abc",
        since_timestamp=0,
    ) == ["321"]
    snapshot = backend.get_job(user="alice", job_id="321")
    cancelled = backend.cancel(user="alice", job_id="321")
    assert snapshot.run_state.value == "RUNNING"
    assert cancelled.run_state.value == "CANCELLED"


def test_real107_backend_env_is_shared_by_api_and_worker(tmp_path: Path) -> None:
    values = {
        "PILOT107_BACKEND": "real107-ssh",
        "PILOT107_SSH_TARGET": "pilot107-slurm",
        "PILOT107_SSH_CONTROL_PATH": str(tmp_path / "relay.sock"),
        "PILOT107_SSH_PORTAL_OWNER": "alice",
        "PILOT107_SSH_SLURM_USER": "alice",
        "PILOT107_SSH_OWNER_ROOTS": "/public/home/alice",
    }
    api_config = api_config_from_env(values, project_root=tmp_path)
    worker_config = worker_config_from_env(values, project_root=tmp_path)

    assert api_config.backend == worker_config.backend == "real107-ssh"
    assert api_config.ssh_target_id == worker_config.ssh_target_id == "real107"
    assert api_config.ssh_owner_roots == worker_config.ssh_owner_roots


def test_ssh_auth_required_is_a_non_retryable_worker_action() -> None:
    classification = classify_worker_exception(
        RuntimeError("SSH.AUTH_REQUIRED"),
        default_code=WorkerErrorCode.SLURM_BACKEND_ERROR,
        default_retryable=True,
    )
    assert classification.code == WorkerErrorCode.AUTH_REQUIRED
    assert classification.auth_required is True
    assert classification.retryable is False


def test_connection_store_never_exposes_target_or_socket_and_is_owner_scoped(
    tmp_path: Path,
) -> None:
    config = relay_config(tmp_path)
    client = FakeRelayClient(config)
    service = SshConnectionService(
        config=config,
        client=client,
        store=SshConnectionStore(tmp_path / "pilot107.db"),
    )

    record = service.check_for_owner("alice", "real107")
    payload = record.public_payload()
    assert payload["state"] == "active"
    assert payload["owner"] == "current-user-only"
    assert "target" not in payload
    assert "control_path" not in payload
    assert service.list_for_owner("bob") == []


def test_connection_http_routes_require_identity_and_preserve_safe_payload(
    tmp_path: Path,
) -> None:
    config = relay_config(tmp_path)
    service = SshConnectionService(
        config=config,
        client=FakeRelayClient(config),
        store=SshConnectionStore(tmp_path / "pilot107.db"),
    )
    api = build_api(
        db_path=tmp_path / "pilot107.db",
        evidence_root=tmp_path / "evidence",
        auth_required=True,
    )
    api.ssh_connection_service = service

    missing = api.handle_get("/api/v1/platform/connections")
    listed = api.handle_get(
        "/api/v1/platform/connections",
        headers={"X-Pilot107-User": "alice"},
    )
    checked = api.handle_post(
        "/api/v1/platform/connections/real107/check",
        headers={"X-Pilot107-User": "alice"},
    )

    assert missing.status == 401
    assert listed.status == 200
    assert listed.payload["items"][0]["connection_id"] == "real107"
    assert checked.status == 200
    assert checked.payload["state"] == "active"
    assert "target" not in checked.payload
