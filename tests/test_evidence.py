import json
import tempfile
import unittest
from pathlib import Path

from pilot107.adapters.slurm import CommandResult, SlurmTransportError
from pilot107.core.run_store import RunRecord, RunStore
from pilot107.core.states import (
    CapsuleState,
    CollectionState,
    DiagnosisState,
    ResultStatus,
    RunState,
)
from pilot107.worker.evidence import (
    AuthorizedFilesystemEvidenceTransport,
    DockerSlurmEvidenceCollector,
    EvidenceStore,
)


class FakeDockerExecutor:
    def realpath(self, path: str, *, timeout_seconds: float = 10.0) -> str:
        parts: list[str] = []
        for part in path.split("/"):
            if part in {"", "."}:
                continue
            if part == "..":
                if parts:
                    parts.pop()
                continue
            parts.append(part)
        return "/" + "/".join(parts)

    def run(
        self,
        argv: list[str],
        *,
        cwd: str | None = None,
        user: str | None = None,
        stdin: str | None = None,
        timeout_seconds: float = 10.0,
    ) -> CommandResult:
        match argv[0]:
            case "pwd":
                return CommandResult(0, f"{cwd or '/public/home/alice'}\n", "")
            case "whoami":
                return CommandResult(0, f"{user or 'alice'}\n", "")
            case "date":
                return CommandResult(0, "2026-07-10T00:00:00+00:00\n", "")
            case "hostname":
                return CommandResult(0, "login-node-sim\n", "")
            case "python":
                return CommandResult(0, "Python 3.12.3\n", "")
            case "which":
                return CommandResult(0, "/usr/bin/python\n", "")
            case "id":
                return CommandResult(
                    0,
                    "uid=1000(alice) gid=1000(alice) groups=1000(alice)\n",
                    "",
                )
            case "env":
                return CommandResult(
                    0,
                    "USER=alice\nHOME=/public/home/alice\nSLURM_JOB_ID=123\n"
                    "SECRET_TOKEN=redacted\n",
                    "",
                )
            case "nvidia-smi":
                return CommandResult(127, "", "nvidia-smi: command not found\n")
            case "find":
                return CommandResult(
                    0,
                    "/public/home/alice/result.txt|12|1780000000.0|alice|alice\n"
                    "/public/home/alice/slurm-123.out|12|1780000000.0|alice|alice\n"
                    "/public/home/alice/pilot107-submit-run_1_submit.sbatch|22|1780000000.0|alice|alice\n",
                    "",
                )
            case "sacct":
                return CommandResult(
                    0,
                    (
                        "123|alice|students|Students|qos_stu_default|COMPLETED|0:0|"
                        "00:00:01|1|cpu=1|cpu=1|worker-1|start|end\n"
                    ),
                    "",
                )
            case "squeue":
                return CommandResult(
                    0,
                    ("123|alice|PENDING|Resources|Students|pilot107|1|1G|gres/gpu:1\n"),
                    "",
                )
            case "scontrol":
                return CommandResult(0, "JobId=123 JobState=COMPLETED ExitCode=0:0\n", "")
            case "stat":
                if argv[-1].endswith(".err"):
                    return CommandResult(1, "", "missing")
                return CommandResult(0, "regular file|12|1780000000|alice|alice\n", "")
            case "tail":
                if argv[-1].endswith(".err"):
                    return CommandResult(1, "", "missing")
                return CommandResult(0, "hello\n", "")
            case "sha256sum":
                if argv[-1].endswith(".err"):
                    return CommandResult(1, "", "missing")
                return CommandResult(
                    0,
                    "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824  file\n",
                    "",
                )
            case _:
                return CommandResult(1, "", "unexpected")


class EscapingDockerExecutor(FakeDockerExecutor):
    def realpath(self, path: str, *, timeout_seconds: float = 10.0) -> str:
        if path == "/public/home/alice/slurm-123.out":
            return "/public/home/bob/secret.out"
        return super().realpath(path, timeout_seconds=timeout_seconds)


def _run(
    *,
    workdir: str = "/public/home/alice",
    resource_plan: dict[str, object] | None = None,
) -> RunRecord:
    return RunRecord(
        run_id="run_123",
        owner="alice",
        state=RunState.SUCCEEDED,
        collection_state=CollectionState.PENDING,
        diagnosis_state=DiagnosisState.PENDING,
        capsule_state=CapsuleState.PENDING,
        result_status=ResultStatus.COMPLETE,
        job_id="123",
        workdir=workdir,
        script="#!/bin/bash\nhostname\n",
        exit_code="0:0",
        terminal_state="COMPLETED",
        submit_strategy="command",
        submit_response={
            "stdout": "123\n",
            "stderr": "",
            "argv": ["sbatch", "--parsable", "--chdir", workdir, "pilot107-submit.sbatch"],
        },
        created_at="2026-07-10T00:00:00+00:00",
        updated_at="2026-07-10T00:00:00+00:00",
        resource_plan=resource_plan or {},
    )


class EvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.store = EvidenceStore(root / "evidence")
        self.run_store = RunStore(root / "pilot107.db")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_evidence_store_rejects_escaped_logical_path(self) -> None:
        with self.assertRaises(ValueError):
            self.store.write_text(
                run_id="run_1",
                logical_path="../outside.txt",
                content="bad",
                content_type="text/plain",
            )

    def test_docker_collector_writes_slurm_and_log_evidence(self) -> None:
        collector = DockerSlurmEvidenceCollector(
            store=self.store,
            executor=FakeDockerExecutor(),  # type: ignore[arg-type]
            allowed_roots=["/public/home/alice"],
            run_store=self.run_store,
        )
        self.run_store.create_run(
            run_id="run_123",
            owner="alice",
            workdir="/public/home/alice",
            script="#!/bin/bash\nhostname\n",
        )

        submission = collector.collect(run=_run(), task_type="submission_snapshot")
        runtime = collector.collect(run=_run(), task_type="runtime_status")
        accounting = collector.collect(run=_run(), task_type="terminal_accounting")
        logs = collector.collect(run=_run(), task_type="logs_finalize")
        environment = collector.collect(run=_run(), task_type="environment_finalize")
        outputs = collector.collect(run=_run(), task_type="outputs_inventory")
        summary = collector.collect(run=_run(), task_type="result_summary")

        run_root = self.store.run_root("run_123")
        self.assertTrue((run_root / "submission" / "slurm_submit_response.json").exists())
        self.assertTrue((run_root / "submission" / "user_script.original.sh").exists())
        self.assertTrue((run_root / "submission" / "submitted_script.resolved.sh").exists())
        self.assertTrue((run_root / "submission" / "execution_wrapper.generated.sh").exists())
        self.assertTrue((run_root / "slurm" / "accounting.json").exists())
        self.assertTrue((run_root / "slurm" / "runtime_status.json").exists())
        self.assertTrue((run_root / "slurm" / "job_detail.json").exists())
        self.assertTrue((run_root / "logs" / "stdout.tail.json").exists())
        self.assertTrue((run_root / "logs" / "stderr.tail.json").exists())
        self.assertTrue((run_root / "environment" / "summary.json").exists())
        self.assertTrue((run_root / "run" / "request" / "resource-plan.json").exists())
        self.assertTrue((run_root / "run" / "request" / "submitted-script.sbatch").exists())
        self.assertTrue((run_root / "run" / "request" / "sbatch-argv.json").exists())
        self.assertTrue((run_root / "run" / "environment" / "basic.json").exists())
        self.assertTrue((run_root / "run" / "timeline" / "events.jsonl").exists())
        self.assertTrue((run_root / "outputs" / "inventory.json").exists())
        self.assertTrue((run_root / "derived" / "result_summary.v1.json").exists())
        self.assertTrue((run_root / "manifest" / "manifest.json").exists())
        self.assertEqual(
            {artifact.logical_path for artifact in accounting.artifacts},
            {
                "slurm/accounting.json",
                "slurm/job_detail.json",
                "manifest/manifest.json",
            },
        )
        self.assertIn(
            "submission/slurm_submit_response.json",
            {artifact.logical_path for artifact in submission.artifacts},
        )
        self.assertEqual(
            {artifact.logical_path for artifact in runtime.artifacts},
            {"slurm/runtime_status.json", "manifest/manifest.json"},
        )
        self.assertIn(
            "environment/summary.json",
            {artifact.logical_path for artifact in environment.artifacts},
        )
        self.assertIn(
            "outputs/inventory.json", {artifact.logical_path for artifact in outputs.artifacts}
        )
        self.assertIn(
            "derived/result_summary.v1.json",
            {artifact.logical_path for artifact in summary.artifacts},
        )
        self.assertIn(
            "run/request/resource-plan.json",
            {artifact.logical_path for artifact in submission.artifacts},
        )
        self.assertIn(
            "run/environment/basic.json",
            {artifact.logical_path for artifact in environment.artifacts},
        )
        self.assertIn("stderr log missing", logs.warnings)
        env_payload = (run_root / "environment" / "summary.json").read_text(encoding="utf-8")
        self.assertIn("USER=alice", env_payload)
        self.assertNotIn("SECRET_TOKEN", env_payload)
        basic_env = json.loads(
            (run_root / "run" / "environment" / "basic.json").read_text(encoding="utf-8")
        )
        self.assertEqual(basic_env["pwd"], "/public/home/alice")
        self.assertEqual(basic_env["python_path"], "/usr/bin/python")
        self.assertEqual(basic_env["slurm_env"]["SLURM_JOB_ID"], "123")
        inventory = json.loads(
            (run_root / "outputs" / "inventory.json").read_text(encoding="utf-8")
        )
        inventory_paths = {item["relative_path"] for item in inventory["files"]}
        self.assertEqual(inventory_paths, {"result.txt"})
        indexed_paths = {
            obj.logical_path for obj in self.run_store.list_evidence_objects("run_123")
        }
        self.assertIn("environment/summary.json", indexed_paths)
        self.assertIn("outputs/inventory.json", indexed_paths)
        self.assertIn("submission/execution_wrapper.generated.sh", indexed_paths)
        self.assertIn("run/request/resource-plan.json", indexed_paths)
        self.assertIn("run/environment/basic.json", indexed_paths)
        self.assertIn("run/timeline/events.jsonl", indexed_paths)
        self.assertIn("derived/result_summary.v1.json", indexed_paths)
        self.assertIn("manifest/manifest.json", indexed_paths)
        manifest_object = next(
            obj
            for obj in self.run_store.list_evidence_objects("run_123")
            if obj.logical_path == "manifest/manifest.json"
        )
        self.assertIsNotNone(manifest_object.finalized_at)
        self.assertIsNotNone(manifest_object.sha256)
        summary_payload = json.loads(
            (run_root / "derived" / "result_summary.v1.json").read_text(encoding="utf-8")
        )
        self.assertEqual(summary_payload["run_state"], "SUCCEEDED")
        self.assertEqual(summary_payload["outputs"]["file_count"], 1)
        runtime_payload = json.loads(
            (run_root / "slurm" / "runtime_status.json").read_text(encoding="utf-8")
        )
        self.assertEqual(runtime_payload["job"]["state"], "PENDING")
        self.assertEqual(runtime_payload["job"]["reason"], "Resources")
        accounting_payload = json.loads(
            (run_root / "slurm" / "accounting.json").read_text(encoding="utf-8")
        )
        self.assertEqual(accounting_payload["records"][0]["account"], "students")
        self.assertEqual(accounting_payload["records"][0]["qos"], "qos_stu_default")

    def test_gpu_request_writes_explicit_unavailable_probe(self) -> None:
        collector = DockerSlurmEvidenceCollector(
            store=self.store,
            executor=FakeDockerExecutor(),  # type: ignore[arg-type]
            allowed_roots=["/public/home/alice"],
            run_store=self.run_store,
        )
        self.run_store.create_run(
            run_id="run_123",
            owner="alice",
            workdir="/public/home/alice",
            script="#!/bin/bash\nhostname\n",
            resource_plan={"gpus_per_node": 2},
        )

        result = collector.collect(
            run=_run(resource_plan={"gpus_per_node": 2}),
            task_type="environment_finalize",
        )

        run_root = self.store.run_root("run_123")
        gpu_payload = json.loads(
            (run_root / "run" / "environment" / "gpu.json").read_text(encoding="utf-8")
        )
        self.assertEqual(gpu_payload["requested_gpus"], 2)
        self.assertEqual(gpu_payload["probe"]["status"], "unavailable")
        self.assertEqual(gpu_payload["probe"]["reason"], "command_not_found")
        self.assertIn("gpu probe unavailable: command_not_found", result.warnings)

    def test_docker_collector_can_read_logs_and_outputs_through_evidence_transport(self) -> None:
        source_root = Path(self._tmp.name) / "public" / "home" / "alice"
        source_root.mkdir(parents=True)
        (source_root / "slurm-123.out").write_text("transport stdout\n", encoding="utf-8")
        (source_root / "result.txt").write_text("transport output\n", encoding="utf-8")
        (source_root / "slurm-123.err").write_text("", encoding="utf-8")
        run = _run(workdir=str(source_root))
        executor = FileCommandsForbiddenExecutor()
        collector = DockerSlurmEvidenceCollector(
            store=self.store,
            executor=executor,  # type: ignore[arg-type]
            allowed_roots=[str(source_root)],
            run_store=self.run_store,
            evidence_transport=AuthorizedFilesystemEvidenceTransport(
                allowed_roots=[source_root],
            ),
        )
        self.run_store.create_run(
            run_id="run_123",
            owner="alice",
            workdir=str(source_root),
            script="#!/bin/bash\nhostname\n",
        )

        collector.collect(run=run, task_type="logs_finalize")
        collector.collect(run=run, task_type="outputs_inventory")

        run_root = self.store.run_root("run_123")
        stdout_payload = json.loads(
            (run_root / "logs" / "stdout.tail.json").read_text(encoding="utf-8")
        )
        inventory_payload = json.loads(
            (run_root / "outputs" / "inventory.json").read_text(encoding="utf-8")
        )
        self.assertEqual(stdout_payload["transport"], "authorized_filesystem")
        self.assertIn("transport stdout", stdout_payload["tail"])
        self.assertEqual(inventory_payload["transport"], "authorized_filesystem")
        self.assertEqual(
            {item["relative_path"] for item in inventory_payload["files"]},
            {"result.txt"},
        )
        self.assertNotIn("stat", executor.commands)
        self.assertNotIn("tail", executor.commands)
        self.assertNotIn("sha256sum", executor.commands)
        self.assertNotIn("find", executor.commands)

    def test_docker_collector_rejects_log_path_outside_allowed_root(self) -> None:
        collector = DockerSlurmEvidenceCollector(
            store=self.store,
            executor=EscapingDockerExecutor(),  # type: ignore[arg-type]
            allowed_roots=["/public/home/alice"],
        )

        with self.assertRaises(SlurmTransportError):
            collector.collect(run=_run(), task_type="logs_finalize")

    def test_docker_collector_expands_owner_scoped_roots_for_evidence_reads(self) -> None:
        collector = DockerSlurmEvidenceCollector(
            store=self.store,
            executor=FakeDockerExecutor(),  # type: ignore[arg-type]
            allowed_roots=["/public/home/{user}"],
        )

        self.assertEqual(
            collector._authorize_source_path("/public/home/alice/result.txt", user="alice"),
            "/public/home/alice/result.txt",
        )
        with self.assertRaises(SlurmTransportError):
            collector._authorize_source_path("/public/home/bob/result.txt", user="alice")


class FileCommandsForbiddenExecutor(FakeDockerExecutor):
    def __init__(self) -> None:
        self.commands: list[str] = []

    def run(
        self,
        argv: list[str],
        *,
        cwd: str | None = None,
        user: str | None = None,
        stdin: str | None = None,
        timeout_seconds: float = 10.0,
    ) -> CommandResult:
        self.commands.append(argv[0])
        if argv[0] in {"stat", "tail", "sha256sum", "find"}:
            raise AssertionError(f"file command should go through EvidenceTransport: {argv}")
        return super().run(
            argv,
            cwd=cwd,
            user=user,
            stdin=stdin,
            timeout_seconds=timeout_seconds,
        )


if __name__ == "__main__":
    unittest.main()
