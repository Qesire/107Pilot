#!/usr/bin/env python3
"""Authorized R1 facts through the typed SSH relay and persistence read models."""

from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
from pathlib import Path

from pilot107.adapters.ssh_relay import (
    FixedRemoteProgram,
    SshRelayConfig,
    SshRelayExecutor,
    SshSessionState,
    SubprocessSshRelayClient,
)
from pilot107.agent.project_store import SQLiteProjectStore
from pilot107.api.evidence_query import EvidenceQueryService
from pilot107.api.http_app import Pilot107HttpApi
from pilot107.api.runtime_watch_routes import RuntimeWatchRoutes
from pilot107.core.agent import AgentExplainService
from pilot107.core.evidence_binding import EvidenceBinder
from pilot107.core.identity import UserIdentity
from pilot107.core.paths import SafePath
from pilot107.core.run_store import RunStore
from pilot107.runtime_watch.reader import RuntimeLogSource
from pilot107.runtime_watch.service import RuntimeWatchPolicy, RuntimeWatchService
from pilot107.runtime_watch.store import SQLiteRuntimeWatchStore
from pilot107.worker.evidence import EvidenceStore
from pilot107.worker.ssh_evidence import SSH_EVIDENCE_FS_PROGRAM, SshEvidenceTransport


class FixedSourceResolver:
    def __init__(self, root: Path, job_id: str, owner: str) -> None:
        self.root = root
        self.job_id = job_id
        self.owner = owner

    def resolve(self, *, run_id: str, owner: str, connection_id: str) -> RuntimeLogSource:
        del connection_id
        if owner != self.owner:
            raise PermissionError("R1 Runtime Watch owner mismatch")
        return RuntimeLogSource(
            run_id=run_id,
            owner=owner,
            stdout_path=_safe(self.root / f"success-{self.job_id}.out", self.root),
            stderr_path=_safe(self.root / f"success-{self.job_id}.err", self.root),
        )


def _safe(path: Path, root: Path) -> SafePath:
    return SafePath(original=str(path), resolved=path, root=root)


def _success_job(summary: Path) -> str:
    values = dict(
        line.split("=", 1)
        for line in summary.read_text(encoding="utf-8").splitlines()
        if "=" in line
    )
    job_id = values.get("success", "").split("|", 1)[0]
    if not job_id.isdigit():
        raise RuntimeError("R1 job summary has no successful numeric job id")
    return job_id


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", required=True)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--approved-root", type=Path, required=True)
    parser.add_argument("--control-path", type=Path, required=True)
    parser.add_argument("--job-summary", type=Path, required=True)
    args = parser.parse_args()
    job_id = _success_job(args.job_summary)
    config = SshRelayConfig(
        connection_id="r1-acceptance",
        target_id="real107",
        target=args.target,
        control_path=args.control_path,
        portal_owner=args.owner,
        slurm_user=args.owner,
        owner_roots=(str(args.approved_root),),
        timeout_seconds=20,
    )
    client = SubprocessSshRelayClient(
        config,
        fixed_programs={FixedRemoteProgram.EVIDENCE_FS: SSH_EVIDENCE_FS_PROGRAM},
    )
    active = client.check()
    if active.state is not SshSessionState.ACTIVE:
        raise RuntimeError(f"authorized R1 ControlMaster is not active: {active.status_code}")
    expired_config = SshRelayConfig(
        connection_id="r1-expired-negative",
        target_id="real107",
        target=args.target,
        control_path=args.control_path.with_name(args.control_path.name + ".expired-negative"),
        portal_owner=args.owner,
        slurm_user=args.owner,
        owner_roots=(str(args.approved_root),),
        timeout_seconds=2,
    )
    expired = SubprocessSshRelayClient(expired_config).check()
    if expired.state is not SshSessionState.AUTH_REQUIRED:
        raise RuntimeError(f"expired ControlMaster was not fail-closed: {expired.state}")

    executor = SshRelayExecutor(client)
    resources = executor.run(
        ["sinfo", "--noheader", "--format", "%P|%a|%l"],
        user=args.owner,
        timeout_seconds=20,
    )
    if resources.returncode != 0 or not resources.stdout.strip():
        raise RuntimeError("R1 resource availability query failed")
    transport = SshEvidenceTransport(client=client)
    identity = UserIdentity(username=args.owner)
    stdout_path = args.approved_root / f"success-{job_id}.out"
    stat = transport.stat(identity, _safe(stdout_path, args.approved_root))
    tail = transport.read_text_tail(
        identity,
        _safe(stdout_path, args.approved_root),
        max_bytes=64 * 1024,
    )
    if stat.kind != "regular file" or "pilot107-real107-success" not in tail.tail:
        raise RuntimeError("R1 Evidence transport did not read the successful job output")

    with tempfile.TemporaryDirectory(prefix="pilot107-r1-acceptance-") as temporary:
        local = Path(temporary)
        database = local / "r1.db"
        run_store = RunStore(database)
        run_id = "run_r1_success"
        run_store.create_run(
            run_id=run_id,
            owner=args.owner,
            workdir=str(args.approved_root),
            script="#!/bin/bash\necho pilot107-real107-success\n",
            resource_plan={"partition": "Students", "cpus_per_task": 1},
        )
        watch_store = SQLiteRuntimeWatchStore(database, segment_root=local / "segments")
        watch_store.create_watch(
            run_id=run_id,
            owner=args.owner,
            connection_id="r1-acceptance",
        )
        watch = RuntimeWatchService(
            store=watch_store,
            transport_for_connection=lambda _connection_id: transport,
            source_resolver=FixedSourceResolver(args.approved_root, job_id, args.owner),
            worker_id="r1-acceptance-watch",
            policy=RuntimeWatchPolicy(active_poll_seconds=1, quiet_poll_seconds=1),
        )
        first_tick = watch.tick()
        watch.on_run_terminal(run_id=run_id, owner=args.owner)
        drain_tick = watch.tick()
        segments = watch_store.list_segments(run_id, owner=args.owner, stream="stdout")
        content = b"".join(
            watch_store.read_segment_content(segment.segment_id, owner=args.owner)
            for segment in segments
        )
        if first_tick.errors or drain_tick.errors or b"pilot107-real107-success" not in content:
            raise RuntimeError("R1 Runtime Watch did not persist and drain the real job log")

        evidence_store = EvidenceStore(local / "evidence")
        artifact = evidence_store.write_text(
            run_id=run_id,
            logical_path="r1/real-job-summary.json",
            content=json.dumps({"job_id": job_id, "sha256": tail.sha256}) + "\n",
            content_type="application/json",
        )
        run_store.upsert_evidence_objects(
            run_id,
            [
                {
                    "object_id": "evidence-r1-" + hashlib.sha256(job_id.encode()).hexdigest()[:16],
                    "category": "result",
                    "logical_path": "r1/real-job-summary.json",
                    "store_path": str(artifact.path),
                    "source_uri": f"ssh://{args.target}{stdout_path}",
                    "sha256": artifact.sha256,
                    "size_bytes": artifact.size_bytes,
                    "mime_type": artifact.content_type,
                    "collection_status": "collected",
                    "mutable_during_run": False,
                    "finalized_at": active.checked_at,
                }
            ],
        )
        query = EvidenceQueryService(store=run_store, evidence_store=evidence_store)
        api = Pilot107HttpApi(
            store=run_store,
            evidence_query=query,
            agent_explain_service=AgentExplainService(
                store=run_store,
                llm_provider=None,
                evidence_binder=EvidenceBinder(store=run_store, evidence_root=evidence_store.root),
            ),
            runtime_watch_routes=RuntimeWatchRoutes(watch_store),
        )
        headers = {"X-Pilot107-User": args.owner}
        run_response = api.handle_get(f"/api/v1/runs/{run_id}", headers=headers)
        evidence_response = api.handle_get(f"/api/v1/runs/{run_id}/evidence", headers=headers)
        watch_response = api.handle_get(f"/api/v1/runs/{run_id}/runtime-watch", headers=headers)
        degraded = api.handle_post(
            "/api/v1/contracts/agent/suggest",
            body=json.dumps(
                {
                    "current_contract": {},
                    "recipe_version_id": "recipe_python_cpu@1.0.0",
                    "user_intent": "generate a patch",
                    "provider": "local",
                }
            ).encode(),
            headers=headers,
        )
        project_store = SQLiteProjectStore(database)
        project = project_store.create_project(
            owner=args.owner,
            origin="blank",
            goal="R1 generative fallback",
            request_key="r1-model-unavailable",
        )
        if degraded.payload.get("status") == "degraded":
            project = project_store.block_for_model_unavailability(
                project.project_id,
                owner=args.owner,
            )
        if (
            [run_response.status, evidence_response.status, watch_response.status]
            != [200, 200, 200]
            or degraded.payload.get("reason") != "provider_unconfigured"
            or project.state.value != "blocked"
        ):
            raise RuntimeError("R1 model-unavailable deterministic fallback failed")

        print(
            json.dumps(
                {
                    "schema": "pilot107.agent-lifecycle-r1-smoke/v1",
                    "auth_expired": expired.status_code,
                    "resource_availability": resources.stdout.splitlines(),
                    "evidence": {"job_id": job_id, "bytes": stat.size_bytes},
                    "runtime_watch": {"segments": len(segments), "state": "stopped"},
                    "model_unavailable": {
                        "generative_project_state": project.state.value,
                        "run_evidence_watch_available": True,
                    },
                },
                sort_keys=True,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
