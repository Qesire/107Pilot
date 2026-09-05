import base64
import hashlib
import importlib.util
import io
import json
import os
import tarfile
import tempfile
import unittest
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

_GATEWAY_PATH = Path(__file__).resolve().parents[1] / "simulator/compose/scripts/command-gateway.py"
_SPEC = importlib.util.spec_from_file_location("pilot107_command_gateway", _GATEWAY_PATH)
assert _SPEC is not None
assert _SPEC.loader is not None
gateway = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(gateway)


@contextmanager
def _mock_ownership() -> Iterator[None]:
    """Stub out user lookup + chown so file ops run in an unprivileged test."""
    fake_pw = SimpleNamespace(pw_uid=0, pw_gid=0)
    with (
        mock.patch.object(gateway.pwd, "getpwnam", return_value=fake_pw),
        mock.patch.object(gateway.os, "chown"),
    ):
        yield


class CommandGatewayTests(unittest.TestCase):
    def test_publication_compare_and_swap_and_delete_are_digest_guarded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            staged = root / ".pilot107-stage"
            target = root / "project" / "solver.c"
            target.parent.mkdir()
            desired = b"new solver\n"
            staged.write_bytes(desired)
            desired_sha256 = hashlib.sha256(desired).hexdigest()
            config = gateway.GatewayConfig(token=None, allowed_roots=[tmp])

            with _mock_ownership():
                self.assertIsNone(
                    gateway._path_sha256({"path": str(target), "owner": "alice"}, config)["sha256"]
                )
                committed = gateway._compare_and_swap_file(
                    {
                        "staged_path": str(staged),
                        "target_path": str(target),
                        "expected_sha256": None,
                        "desired_sha256": desired_sha256,
                        "owner": "alice",
                    },
                    config,
                )
                stale = gateway._compare_and_swap_file(
                    {
                        "staged_path": str(root / "missing-stage"),
                        "target_path": str(target),
                        "expected_sha256": None,
                        "desired_sha256": desired_sha256,
                        "owner": "alice",
                    },
                    config,
                )
                deleted = gateway._compare_and_delete_file(
                    {
                        "target_path": str(target),
                        "expected_sha256": desired_sha256,
                        "owner": "alice",
                    },
                    config,
                )
                deleted_again = gateway._compare_and_delete_file(
                    {
                        "target_path": str(target),
                        "expected_sha256": desired_sha256,
                        "owner": "alice",
                    },
                    config,
                )

            self.assertEqual(committed["outcome"], "committed")
            self.assertEqual(stale["outcome"], "already_committed")
            self.assertEqual(deleted["outcome"], "committed")
            self.assertEqual(deleted_again["outcome"], "already_committed")
            self.assertFalse(target.exists())

    def test_publication_compare_and_swap_rejects_stale_or_unsafe_targets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            staged = root / "staged"
            target.write_bytes(b"current")
            staged.write_bytes(b"desired")
            desired_sha256 = hashlib.sha256(b"desired").hexdigest()
            config = gateway.GatewayConfig(token=None, allowed_roots=[tmp])

            with _mock_ownership():
                conflict = gateway._compare_and_swap_file(
                    {
                        "staged_path": str(staged),
                        "target_path": str(target),
                        "expected_sha256": hashlib.sha256(b"stale").hexdigest(),
                        "desired_sha256": desired_sha256,
                        "owner": "alice",
                    },
                    config,
                )
                target.unlink()
                target.symlink_to(root / "elsewhere")
                with self.assertRaisesRegex(gateway.GatewayError, "regular file"):
                    gateway._path_sha256({"path": str(target), "owner": "alice"}, config)

            self.assertEqual(conflict["outcome"], "conflict")
            self.assertTrue(staged.exists())

    def test_search_files_matches_name_and_relative_path_without_following_symlinks(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "public/home/alice"
            (root / "models/v1").mkdir(parents=True)
            (root / "models/v1/weights.bin").write_bytes(b"123")
            (root / "model-link").symlink_to(root / "models", target_is_directory=True)
            config = gateway.GatewayConfig(token=None, allowed_roots=[str(root)])

            page = gateway._search_files(
                {
                    "root": str(root),
                    "owner": "alice",
                    "q": "MODEL",
                    "kind": "all",
                    "limit": 100,
                    "cursor": None,
                    "scan_limit": 1000,
                    "time_limit_ms": 1000,
                },
                config,
            )

            self.assertEqual(
                [item["relative_path"] for item in page["items"]],
                ["models", "models/v1", "models/v1/weights.bin"],
            )

    def test_search_files_rejects_outside_root_and_oversized_page(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "alice"
            root.mkdir()
            config = gateway.GatewayConfig(token="cursor-secret", allowed_roots=[str(root)])

            with self.assertRaisesRegex(gateway.GatewayError, "outside allowed roots"):
                gateway._search_files(
                    {"root": str(Path(tmp) / "bob"), "owner": "alice", "q": "x"},
                    config,
                )
            with self.assertRaisesRegex(gateway.GatewayError, "limit"):
                gateway._search_files(
                    {"root": str(root), "owner": "alice", "q": "x", "limit": 101},
                    config,
                )

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO creation requires POSIX")
    def test_search_files_omits_symlinks_and_special_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "alice"
            root.mkdir()
            (root / "ordinary.txt").write_text("ok", encoding="utf-8")
            (root / "ordinary-link").symlink_to(root / "ordinary.txt")
            os.mkfifo(root / "ordinary-pipe")
            config = gateway.GatewayConfig(token=None, allowed_roots=[str(root)])

            page = gateway._search_files(
                {"root": str(root), "owner": "alice", "q": "ordinary", "kind": "all"},
                config,
            )

            self.assertEqual([item["relative_path"] for item in page["items"]], ["ordinary.txt"])

    def test_search_files_budget_returns_bound_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "alice"
            root.mkdir()
            for index in range(5):
                (root / f"model-{index}.txt").write_text(str(index), encoding="utf-8")
            alternate_root = root / "z-alternate"
            alternate_root.mkdir()
            config = gateway.GatewayConfig(token="cursor-secret", allowed_roots=[str(root)])
            request = {
                "root": str(root),
                "owner": "alice",
                "q": "model",
                "kind": "file",
                "limit": 100,
                "scan_limit": 2,
                "time_limit_ms": 1000,
            }

            first = gateway._search_files(request, config)

            self.assertTrue(first["incomplete"])
            self.assertIsInstance(first["next_cursor"], str)
            self.assertEqual(len(first["items"]), 2)
            second = gateway._search_files(
                {**request, "cursor": first["next_cursor"], "scan_limit": 100}, config
            )
            self.assertFalse(second["incomplete"])
            self.assertEqual(
                [item["relative_path"] for item in first["items"] + second["items"]],
                [f"model-{index}.txt" for index in range(5)],
            )
            for changed in (
                {"owner": "bob"},
                {"root": str(alternate_root)},
                {"q": "other"},
            ):
                with self.assertRaisesRegex(gateway.GatewayError, "cursor"):
                    gateway._search_files(
                        {**request, **changed, "cursor": first["next_cursor"]}, config
                    )

    def test_rejects_wrong_bearer_token(self) -> None:
        config = gateway.GatewayConfig(token="expected", allowed_roots=["/public/home/alice"])

        with self.assertRaises(gateway.GatewayError) as caught:
            gateway._check_auth(config, "Bearer wrong")

        self.assertEqual(caught.exception.status, 401)

    def test_rejects_shell_command(self) -> None:
        config = gateway.GatewayConfig(token=None, allowed_roots=["/public/home/alice"])

        with self.assertRaises(gateway.GatewayError) as caught:
            gateway._run({"argv": ["sh", "-c", "id"]}, config)

        self.assertIn("command not allowed", str(caught.exception))

    def test_runs_structured_argv_without_shell(self) -> None:
        config = gateway.GatewayConfig(token=None, allowed_roots=["/public/home/alice"])

        with _mock_ownership(), mock.patch.object(gateway.subprocess, "run") as fake_run:
            fake_run.return_value = SimpleNamespace(returncode=0, stdout="123\n", stderr="")

            result = gateway._run(
                {
                    "argv": ["sbatch", "--parsable", "/public/home/alice/job.sbatch"],
                    "timeout_seconds": 2,
                },
                config,
            )

        self.assertEqual(result["stdout"], "123\n")
        args, kwargs = fake_run.call_args
        self.assertEqual(args[0], ["sbatch", "--parsable", "/public/home/alice/job.sbatch"])
        self.assertNotIn("shell", kwargs)

    def test_environment_commands_have_exact_argument_allowlist(self) -> None:
        config = gateway.GatewayConfig(token=None, allowed_roots=["/public/home/alice"])

        with self.assertRaisesRegex(gateway.GatewayError, "arguments not allowed"):
            gateway._run({"argv": ["env", "sh", "-c", "id"]}, config)
        with self.assertRaisesRegex(gateway.GatewayError, "arguments not allowed"):
            gateway._run({"argv": ["python", "-c", "print('unsafe')"]}, config)

    def test_allows_only_the_readonly_terminal_sinfo_projection(self) -> None:
        config = gateway.GatewayConfig(token=None, allowed_roots=["/public/home/alice"])

        with mock.patch.object(gateway.subprocess, "run") as fake_run:
            fake_run.return_value = SimpleNamespace(
                returncode=0,
                stdout="debug|8|16000|(null)|idle\n",
                stderr="",
            )
            result = gateway._run(
                {"argv": ["sinfo", "-h", "-o", "%P|%c|%m|%G|%T"]},
                config,
            )

        self.assertEqual(result["returncode"], 0)
        with self.assertRaisesRegex(gateway.GatewayError, "arguments not allowed"):
            gateway._run({"argv": ["sinfo", "-R"]}, config)

    def test_allows_exact_platform_snapshot_probes(self) -> None:
        config = gateway.GatewayConfig(
            token=None,
            allowed_roots=["/public/home/{user}"],
        )
        accepted = (
            ["scontrol", "show", "part"],
            ["scontrol", "show", "nodes"],
            ["sinfo", "-h", "-o", "%N|%P|%t|%c|%m|%G|%E"],
            ["conda", "env", "list", "--json"],
            ["df", "-P", "-h", "/public", "/public/home/alice"],
        )

        with _mock_ownership(), mock.patch.object(gateway.subprocess, "run") as fake_run:
            fake_run.return_value = SimpleNamespace(returncode=0, stdout="", stderr="")
            for argv in accepted:
                result = gateway._run({"argv": argv, "user": "alice"}, config)
                self.assertEqual(result["returncode"], 0)

        executed = [
            call.args[0]
            for call in fake_run.call_args_list
            if call.args and call.args[0][0] == "gosu"
        ]
        self.assertEqual(executed, [["gosu", "alice", *argv] for argv in accepted])

    def test_platform_snapshot_probes_reject_widened_arguments(self) -> None:
        config = gateway.GatewayConfig(
            token=None,
            allowed_roots=["/public/home/{user}"],
        )
        rejected = (
            ["scontrol", "show", "secrets"],
            ["sinfo", "-R"],
            ["conda", "run", "python"],
            ["df", "-P", "-h", "/public", "/etc"],
            ["df", "-P", "-h", "/public", "/public/home/bob"],
        )

        with _mock_ownership():
            for argv in rejected:
                with (
                    self.subTest(argv=argv),
                    self.assertRaisesRegex(gateway.GatewayError, "arguments not allowed"),
                ):
                    gateway._run({"argv": argv, "user": "alice"}, config)

    def test_missing_optional_conda_is_a_127_observation(self) -> None:
        config = gateway.GatewayConfig(token=None, allowed_roots=["/public"])

        with (
            _mock_ownership(),
            mock.patch.object(
                gateway.subprocess,
                "run",
                side_effect=FileNotFoundError("conda"),
            ),
        ):
            result = gateway._run(
                {
                    "argv": ["conda", "env", "list", "--json"],
                    "user": "alice",
                },
                config,
            )

        self.assertEqual(
            result,
            {
                "returncode": 127,
                "stdout": "",
                "stderr": "conda: command not found\n",
            },
        )

    def test_allows_only_bounded_observability_sstat_projection(self) -> None:
        config = gateway.GatewayConfig(token=None, allowed_roots=["/public/home/alice"])
        fields = "JobID,NTasks,AllocTRES,AveCPU,MaxRSS,TRESUsageInTot,TRESUsageOutTot"
        argv = [
            "sstat",
            "-nP",
            "--allsteps",
            "-j",
            "101,102",
            "-o",
            fields,
        ]

        with _mock_ownership(), mock.patch.object(gateway.subprocess, "run") as fake_run:
            fake_run.return_value = SimpleNamespace(returncode=0, stdout="", stderr="")
            result = gateway._run({"argv": argv, "user": "alice"}, config)

        self.assertEqual(result["returncode"], 0)
        self.assertEqual(fake_run.call_args.args[0], ["gosu", "alice", *argv])
        with (
            _mock_ownership(),
            self.assertRaisesRegex(gateway.GatewayError, "arguments not allowed"),
        ):
            gateway._run({"argv": ["sstat", "--help"], "user": "alice"}, config)

    def test_allows_only_fixed_observability_scontrol_probe(self) -> None:
        config = gateway.GatewayConfig(token=None, allowed_roots=["/public/home/alice"])

        with _mock_ownership(), mock.patch.object(gateway.subprocess, "run") as fake_run:
            fake_run.return_value = SimpleNamespace(returncode=0, stdout="", stderr="")
            result = gateway._run(
                {"argv": ["scontrol", "show", "config"], "user": "alice"},
                config,
            )

        self.assertEqual(result["returncode"], 0)
        with (
            _mock_ownership(),
            self.assertRaisesRegex(gateway.GatewayError, "arguments not allowed"),
        ):
            gateway._run(
                {"argv": ["scontrol", "show", "secrets"], "user": "alice"},
                config,
            )

    def test_path_probe_rejects_path_outside_allowed_roots(self) -> None:
        config = gateway.GatewayConfig(token=None, allowed_roots=["/public/home/alice"])

        with self.assertRaisesRegex(gateway.GatewayError, "outside allowed roots"):
            gateway._run({"argv": ["test", "-r", "/etc/shadow"]}, config)

    def test_owner_root_template_is_scoped_to_the_supplied_user(self) -> None:
        config = gateway.GatewayConfig(token=None, allowed_roots=["/public/home/{user}"])

        with self.assertRaisesRegex(gateway.GatewayError, "outside allowed roots"):
            gateway._authorize_path("/public/home/bob/result.txt", config, user="alice")

        self.assertEqual(
            gateway._authorize_path("/public/home/bob/result.txt", config, user="bob"),
            "/public/home/bob/result.txt",
        )
        with self.assertRaisesRegex(gateway.GatewayError, "requires a user"):
            gateway._authorize_path("/public/home/bob/result.txt", config)

    def test_realpath_rejects_relative_and_nul_paths(self) -> None:
        with self.assertRaises(gateway.GatewayError):
            gateway._realpath("relative/path")
        with self.assertRaises(gateway.GatewayError):
            gateway._realpath("/public/home/alice\x00/secret")

    def test_write_text_rejects_path_outside_allowed_roots(self) -> None:
        config = gateway.GatewayConfig(token=None, allowed_roots=["/public/home/alice"])

        with self.assertRaises(gateway.GatewayError) as caught:
            gateway._write_text(
                {
                    "path": "/public/home/bob/job.sbatch",
                    "content": "#!/bin/bash\n",
                    "owner": "alice",
                },
                config,
            )

        self.assertIn("path outside allowed roots", str(caught.exception))

    def test_request_id_reuses_safe_header_and_replaces_unsafe_header(self) -> None:
        self.assertEqual(gateway._request_id("req-123_ABC"), "req-123_ABC")

        generated = gateway._request_id("../../bad")

        self.assertTrue(generated.startswith("gw-"))
        self.assertNotEqual(generated, "../../bad")

    def test_rate_limit_rejects_after_configured_window_budget(self) -> None:
        config = gateway.GatewayConfig(
            token=None,
            allowed_roots=["/public/home/alice"],
            rate_limit_max_requests=2,
            rate_limit_window_seconds=60,
        )

        gateway._check_rate_limit(config, "127.0.0.1")
        gateway._check_rate_limit(config, "127.0.0.1")
        with self.assertRaises(gateway.GatewayError) as caught:
            gateway._check_rate_limit(config, "127.0.0.1")

        self.assertEqual(caught.exception.status, 429)

    def test_audit_log_redacts_content_stdin_and_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            audit_path = Path(tmp) / "gateway-audit.jsonl"
            config = gateway.GatewayConfig(
                token="secret-token",
                allowed_roots=["/public/home/alice"],
                audit_log_path=str(audit_path),
            )

            gateway._audit_request(
                config,
                request_id="req-1",
                remote_addr="127.0.0.1",
                method="POST",
                path="/write_text",
                status=200,
                payload={
                    "path": "/public/home/alice/job.sbatch",
                    "owner": "alice",
                    "content": "PRIVATE=secret\n",
                    "stdin": "SECRET_STDIN",
                    "token": "secret-token",
                },
                error=None,
                duration_ms=1.25,
            )

            record = json.loads(audit_path.read_text(encoding="utf-8"))

        self.assertEqual(record["request_id"], "req-1")
        self.assertEqual(record["request"]["path"], "/public/home/alice/job.sbatch")
        self.assertEqual(record["request"]["content_bytes"], len("PRIVATE=secret\n"))
        serialized = json.dumps(record, sort_keys=True)
        self.assertNotIn("PRIVATE=secret", serialized)
        self.assertNotIn("SECRET_STDIN", serialized)
        self.assertNotIn("secret-token", serialized)

    def test_write_bytes_rejects_path_outside_allowed_roots(self) -> None:
        config = gateway.GatewayConfig(token=None, allowed_roots=["/public/home/alice"])

        with self.assertRaisesRegex(gateway.GatewayError, "outside allowed roots"):
            gateway._write_bytes(
                {
                    "path": "/public/home/bob/blob.bin",
                    "data_b64": base64.b64encode(b"x").decode(),
                    "offset": 0,
                    "owner": "alice",
                },
                config,
            )

    def test_read_bytes_rejects_path_outside_allowed_roots(self) -> None:
        config = gateway.GatewayConfig(token=None, allowed_roots=["/public/home/alice"])

        with self.assertRaisesRegex(gateway.GatewayError, "outside allowed roots"):
            gateway._read_bytes(
                {"path": "/public/home/bob/blob.bin", "offset": 0, "length": 4, "owner": "alice"},
                config,
            )

    def test_list_dir_rejects_path_outside_allowed_roots(self) -> None:
        config = gateway.GatewayConfig(token=None, allowed_roots=["/public/home/alice"])

        with self.assertRaisesRegex(gateway.GatewayError, "outside allowed roots"):
            gateway._list_dir({"path": "/public/home/bob", "owner": "alice"}, config)

    def test_disk_usage_rejects_path_outside_allowed_roots(self) -> None:
        config = gateway.GatewayConfig(token=None, allowed_roots=["/public/home/alice"])

        with self.assertRaisesRegex(gateway.GatewayError, "outside allowed roots"):
            gateway._disk_usage({"path": "/public/home/bob", "owner": "alice"}, config)

    def test_disk_usage_sums_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = gateway.GatewayConfig(token=None, allowed_roots=[tmp])
            (Path(tmp) / "a.txt").write_bytes(b"12345")
            (Path(tmp) / "sub").mkdir()
            (Path(tmp) / "sub" / "b.txt").write_bytes(b"678")

            result = gateway._disk_usage({"path": tmp, "owner": "alice"}, config)

            self.assertEqual(result["used_bytes"], 8)
            self.assertIsNotNone(result["total_bytes"])
            self.assertGreater(result["total_bytes"], 0)

    def test_write_bytes_rejects_invalid_base64(self) -> None:
        config = gateway.GatewayConfig(token=None, allowed_roots=["/public/home/alice"])

        with self.assertRaisesRegex(gateway.GatewayError, "base64"):
            gateway._write_bytes(
                {
                    "path": "/public/home/alice/blob.bin",
                    "data_b64": "!!!",
                    "offset": 0,
                    "owner": "alice",
                },
                config,
            )

    def test_write_read_sha256_roundtrip_with_mocked_ownership(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = gateway.GatewayConfig(token=None, allowed_roots=[tmp])
            target = str(Path(tmp) / "blob.bin")
            payload = b"hello \x00\x01 binary"
            with _mock_ownership():
                gateway._write_bytes(
                    {
                        "path": target,
                        "data_b64": base64.b64encode(payload).decode(),
                        "offset": 0,
                        "owner": "alice",
                    },
                    config,
                )
                read_back = gateway._read_bytes(
                    {
                        "path": target,
                        "offset": 0,
                        "length": len(payload) + 8,
                        "owner": "alice",
                    },
                    config,
                )
                digest = gateway._file_sha256({"path": target, "owner": "alice"}, config)

            self.assertEqual(base64.b64decode(read_back["data_b64"]), payload)
            self.assertEqual(read_back["size"], len(payload))
            self.assertEqual(digest["sha256"], hashlib.sha256(payload).hexdigest())

    def test_write_bytes_append_and_offset_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = gateway.GatewayConfig(token=None, allowed_roots=[tmp])
            target = str(Path(tmp) / "blob.bin")
            with _mock_ownership():
                gateway._write_bytes(
                    {
                        "path": target,
                        "data_b64": base64.b64encode(b"abc").decode(),
                        "offset": 0,
                        "owner": "alice",
                    },
                    config,
                )
                gateway._write_bytes(
                    {
                        "path": target,
                        "data_b64": base64.b64encode(b"def").decode(),
                        "offset": -1,
                        "owner": "alice",
                    },
                    config,
                )
                self.assertEqual(Path(target).read_bytes(), b"abcdef")
                with self.assertRaisesRegex(gateway.GatewayError, "does not match file size"):
                    gateway._write_bytes(
                        {
                            "path": target,
                            "data_b64": base64.b64encode(b"z").decode(),
                            "offset": 99,
                            "owner": "alice",
                        },
                        config,
                    )

    def _make_tar(self, members: dict[str, bytes], *, symlink: str | None = None) -> bytes:
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as tar:
            for name, content in members.items():
                info = tarfile.TarInfo(name=name)
                info.size = len(content)
                tar.addfile(info, io.BytesIO(content))
            if symlink is not None:
                link = tarfile.TarInfo(name="evil_link")
                link.type = tarfile.SYMTYPE
                link.linkname = symlink
                tar.addfile(link)
        return buffer.getvalue()

    def test_extract_rejects_traversal_member(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = gateway.GatewayConfig(token=None, allowed_roots=[tmp])
            archive = Path(tmp) / "evil.tar"
            archive.write_bytes(self._make_tar({"../evil.txt": b"x"}))
            dest = Path(tmp) / "out"
            with (
                _mock_ownership(),
                self.assertRaisesRegex(gateway.GatewayError, "escapes destination"),
            ):
                gateway._extract_archive(
                    {"path": str(archive), "dest_dir": str(dest), "owner": "alice"},
                    config,
                )

    def test_extract_rejects_symlink_member(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = gateway.GatewayConfig(token=None, allowed_roots=[tmp])
            archive = Path(tmp) / "link.tar"
            archive.write_bytes(self._make_tar({"ok.txt": b"x"}, symlink="/etc/passwd"))
            dest = Path(tmp) / "out"
            with _mock_ownership(), self.assertRaisesRegex(gateway.GatewayError, "link members"):
                gateway._extract_archive(
                    {"path": str(archive), "dest_dir": str(dest), "owner": "alice"},
                    config,
                )

    def test_extract_valid_archive_reports_member_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = gateway.GatewayConfig(token=None, allowed_roots=[tmp])
            archive = Path(tmp) / "good.tar"
            archive.write_bytes(self._make_tar({"a.txt": b"1", "sub/b.txt": b"22"}))
            dest = Path(tmp) / "out"
            with _mock_ownership():
                result = gateway._extract_archive(
                    {"path": str(archive), "dest_dir": str(dest), "owner": "alice"},
                    config,
                )
            self.assertEqual(result["members"], 2)
            self.assertEqual((dest / "sub" / "b.txt").read_bytes(), b"22")

    def _make_zip(self, members: dict[str, bytes]) -> bytes:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as zf:
            for name, content in members.items():
                zf.writestr(name, content)
        return buffer.getvalue()

    def test_extract_zip_archive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = gateway.GatewayConfig(token=None, allowed_roots=[tmp])
            archive = Path(tmp) / "good.zip"
            archive.write_bytes(self._make_zip({"a.txt": b"1", "sub/b.txt": b"22"}))
            dest = Path(tmp) / "out"
            with _mock_ownership():
                result = gateway._extract_archive(
                    {"path": str(archive), "dest_dir": str(dest), "owner": "alice"},
                    config,
                )
            self.assertEqual(result["members"], 2)
            self.assertEqual((dest / "sub" / "b.txt").read_bytes(), b"22")

    def test_extract_zip_rejects_traversal_member(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = gateway.GatewayConfig(token=None, allowed_roots=[tmp])
            archive = Path(tmp) / "evil.zip"
            archive.write_bytes(self._make_zip({"../evil.txt": b"x"}))
            dest = Path(tmp) / "out"
            with (
                _mock_ownership(),
                self.assertRaisesRegex(gateway.GatewayError, "escapes destination"),
            ):
                gateway._extract_archive(
                    {"path": str(archive), "dest_dir": str(dest), "owner": "alice"},
                    config,
                )

    def test_extract_rar_dispatches_to_unar(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = gateway.GatewayConfig(token=None, allowed_roots=[tmp])
            archive = Path(tmp) / "box.rar"
            archive.write_bytes(b"Rar!\x1a\x07\x00fake")
            dest = Path(tmp) / "out"
            real_run = gateway.subprocess.run

            def fake_run(argv, *args, **kwargs):
                if argv and argv[0] == "unar":
                    return SimpleNamespace(returncode=0, stdout="a.txt\n", stderr="")
                return real_run(argv, *args, **kwargs)

            with (
                _mock_ownership(),
                mock.patch.object(gateway.subprocess, "run", side_effect=fake_run) as patched_run,
            ):
                result = gateway._extract_archive(
                    {"path": str(archive), "dest_dir": str(dest), "owner": "alice"},
                    config,
                )
            unar_calls = [
                call
                for call in patched_run.call_args_list
                if call.args and call.args[0] and call.args[0][0] == "unar"
            ]
            self.assertEqual(len(unar_calls), 1)
            self.assertEqual(unar_calls[0].args[0], ["unar", "-f", "-o", str(dest), str(archive)])
            self.assertEqual(unar_calls[0].kwargs.get("timeout"), 300)
            self.assertEqual(result["status"], "ok")

    def test_extract_rar_failure_raises_gateway_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = gateway.GatewayConfig(token=None, allowed_roots=[tmp])
            archive = Path(tmp) / "broken.rar"
            archive.write_bytes(b"not really rar")
            dest = Path(tmp) / "out"
            real_run = gateway.subprocess.run

            def fake_run(argv, *args, **kwargs):
                if argv and argv[0] == "unar":
                    return SimpleNamespace(
                        returncode=1, stdout="", stderr="unar: corrupt archive\n"
                    )
                return real_run(argv, *args, **kwargs)

            with (
                _mock_ownership(),
                mock.patch.object(gateway.subprocess, "run", side_effect=fake_run),
                self.assertRaisesRegex(gateway.GatewayError, "rar extraction failed"),
            ):
                gateway._extract_archive(
                    {"path": str(archive), "dest_dir": str(dest), "owner": "alice"},
                    config,
                )

    def test_extract_unsupported_format_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = gateway.GatewayConfig(token=None, allowed_roots=[tmp])
            archive = Path(tmp) / "notes.txt"
            archive.write_bytes(b"hello")
            dest = Path(tmp) / "out"
            with (
                _mock_ownership(),
                self.assertRaisesRegex(gateway.GatewayError, "unsupported archive format"),
            ):
                gateway._extract_archive(
                    {"path": str(archive), "dest_dir": str(dest), "owner": "alice"},
                    config,
                )

    def test_create_archive_packs_sources_into_tarball(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = gateway.GatewayConfig(token=None, allowed_roots=[tmp])
            (Path(tmp) / "a.txt").write_bytes(b"1")
            (Path(tmp) / "b.txt").write_bytes(b"22")
            with _mock_ownership():
                result = gateway._create_archive(
                    {
                        "paths": [str(Path(tmp) / "a.txt"), str(Path(tmp) / "b.txt")],
                        "dest_dir": tmp,
                        "archive_name": "bundle.tar.gz",
                        "owner": "alice",
                    },
                    config,
                )
            self.assertEqual(result["members"], 2)
            archive = Path(tmp) / "bundle.tar.gz"
            self.assertEqual(result["path"], str(archive))
            self.assertEqual(result["size"], archive.stat().st_size)
            with tarfile.open(archive, "r:gz") as tar:
                self.assertEqual(sorted(tar.getnames()), ["a.txt", "b.txt"])

    def test_create_archive_rejects_unsafe_name_and_bad_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = gateway.GatewayConfig(token=None, allowed_roots=[tmp])
            with self.assertRaisesRegex(gateway.GatewayError, "unsafe archive name"):
                gateway._create_archive(
                    {
                        "paths": [tmp],
                        "dest_dir": tmp,
                        "archive_name": "../x.tar.gz",
                        "owner": "alice",
                    },
                    config,
                )
            with self.assertRaisesRegex(gateway.GatewayError, "non-empty list"):
                gateway._create_archive(
                    {
                        "paths": [],
                        "dest_dir": tmp,
                        "archive_name": "x.tar.gz",
                        "owner": "alice",
                    },
                    config,
                )


if __name__ == "__main__":
    unittest.main()
