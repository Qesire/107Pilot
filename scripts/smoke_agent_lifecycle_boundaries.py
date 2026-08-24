#!/usr/bin/env python3
"""Objective boundary probes for the lifecycle D1 acceptance pack."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from smoke_pilot_agent_a4_live import _run_checked
from smoke_pilot_agent_repair_live import ComposeWorkspaceRelay

from pilot107.adapters.slurm import DockerComposeExecutor, DockerComposeTarget
from pilot107.agent.project import ExperimentProjectState
from pilot107.agent.project_store import SQLiteProjectStore
from pilot107.agent.session import AgentTurnState
from pilot107.agent.store import SQLiteAgentSessionStore
from pilot107.agent.workspace import WorkspaceImporter
from pilot107.api.evidence_query import EvidenceQueryService
from pilot107.api.http_app import Pilot107HttpApi
from pilot107.api.runtime_watch_routes import RuntimeWatchRoutes
from pilot107.core.agent import AgentExplainService
from pilot107.core.control_repository import SQLiteControlRepository
from pilot107.core.evidence_binding import EvidenceBinder
from pilot107.core.run_store import RunStore
from pilot107.runtime_watch.store import SQLiteRuntimeWatchStore
from pilot107.services.agent_session_service import AgentSessionService
from pilot107.worker.evidence import EvidenceStore

ROOT = Path(__file__).resolve().parents[1]


def live_large_file() -> int:
    compose_dir = ROOT / "simulator" / "compose"
    executor = DockerComposeExecutor(
        DockerComposeTarget(
            compose_file=compose_dir / "compose.yml",
            env_file=compose_dir / ".env.example",
            workdir=compose_dir,
        )
    )
    suffix = uuid4().hex[:10]
    source_root = f"/public/home/alice/pilot107-task21-large-{suffix}"
    _run_checked(executor, ["mkdir", "-p", source_root], user="alice")
    try:
        _run_checked(
            executor,
            ["tee", f"{source_root}/main.py"],
            user="alice",
            stdin="print('metadata boundary')\n",
        )
        _run_checked(
            executor,
            [
                "python3",
                "-c",
                "import sys; f=open(sys.argv[1],'wb'); f.truncate(5 * 1024**3); f.close()",
                f"{source_root}/model.ckpt",
            ],
            user="alice",
        )
        relay = ComposeWorkspaceRelay(executor)
        with tempfile.TemporaryDirectory(prefix="pilot107-task21-large-") as temporary:
            local_root = Path(temporary)
            store = SQLiteProjectStore(local_root / "project.db")
            project = store.create_project(
                owner="alice",
                origin="existing",
                goal="inspect a sparse large checkpoint without copying it",
                request_key="task21-large-file",
            )
            workspace = WorkspaceImporter(
                store=store,
                reader=relay,
                owner_roots=("/public/home/{user}",),
                workspace_root=local_root / "workspaces",
            ).create(project, source_ref=source_root)
            checkpoint = next(
                entry for entry in workspace.snapshot.entries if entry.path == "model.ckpt"
            )
            copied = Path(workspace.local_root) / "model.ckpt"
            if (
                checkpoint.classification != "metadata_only"
                or checkpoint.content_ref is not None
                or checkpoint.size_bytes != 5 * 1024**3
                or copied.exists()
            ):
                raise RuntimeError("large checkpoint crossed the metadata-only boundary")
            print(
                json.dumps(
                    {
                        "schema": "pilot107.agent-lifecycle-boundaries-live/v1",
                        "large_file_metadata_only": True,
                        "remote_size_bytes": checkpoint.size_bytes,
                        "remote_path": f"{source_root}/model.ckpt",
                        "content_copied": False,
                    },
                    sort_keys=True,
                )
            )
    finally:
        _run_checked(
            executor,
            ["python3", "-c", "import shutil,sys; shutil.rmtree(sys.argv[1])", source_root],
            user="alice",
        )
    return 0


def live_model_unavailable() -> int:
    database = Path(os.environ.get("PILOT107_DB_PATH", "/var/lib/pilot107/pilot107.db"))
    evidence_root = Path(
        os.environ.get("PILOT107_EVIDENCE_ROOT", "/var/lib/pilot107/evidence")
    )
    project_store = SQLiteProjectStore(database)
    session_store = SQLiteAgentSessionStore(database)
    control = SQLiteControlRepository(database)
    project = project_store.create_project(
        owner="alice",
        origin="blank",
        goal="generate a project while the model is unavailable",
        request_key=f"task21-model-project-{uuid4().hex}",
    )
    session, _ = session_store.create_session(
        owner="alice",
        request_key=f"task21-model-session-{uuid4().hex}",
        profile_id="experiment_builder",
        model_profile_id="campus-default",
        source={
            "project_id": project.project_id,
            "workspace_id": f"workspace-task21-{uuid4().hex}",
        },
    )
    turn, _ = AgentSessionService(
        store=session_store,
        control_repository=control,
    ).submit_message(
        session_id=session.session_id,
        owner="alice",
        request_key=f"task21-model-turn-{uuid4().hex}",
        message="generate the experiment files",
        expected_state_version=session.state_version,
    )
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        current_turn = session_store.get_turn(turn.turn_id, owner="alice")
        current_project = project_store.get_project(project.project_id, owner="alice")
        if (
            current_turn.state is AgentTurnState.FAILED
            and current_project.state is ExperimentProjectState.BLOCKED
        ):
            break
        if current_turn.state in {AgentTurnState.COMPLETED, AgentTurnState.CANCELLED}:
            raise RuntimeError(
                f"model-unavailable Turn reached an unexpected state: {current_turn.state}"
            )
        time.sleep(0.5)
    else:
        raise RuntimeError(
            "model-unavailable Turn did not fail with an explicitly blocked Project: "
            f"turn={current_turn.state.value} project={current_project.state.value} "
            f"error={current_turn.error}"
        )

    run_store = RunStore(database)
    run_id = f"run_task21_deterministic_{uuid4().hex}"
    run_store.create_run(
        run_id=run_id,
        owner="alice",
        workdir="/public/home/alice",
        script="#!/bin/bash\necho deterministic\n",
        resource_plan={"partition": "Students", "cpus_per_task": 1},
    )
    evidence_store = EvidenceStore(evidence_root)
    artifact = evidence_store.write_text(
        run_id=run_id,
        logical_path="acceptance/deterministic.json",
        content=json.dumps({"available": True}) + "\n",
        content_type="application/json",
    )
    run_store.upsert_evidence_objects(
        run_id,
        [
            {
                "object_id": f"evidence-{hashlib.sha256(run_id.encode()).hexdigest()[:20]}",
                "category": "result",
                "logical_path": "acceptance/deterministic.json",
                "store_path": str(artifact.path),
                "source_uri": f"evidence://runs/{run_id}/acceptance/deterministic.json",
                "sha256": artifact.sha256,
                "size_bytes": artifact.size_bytes,
                "mime_type": artifact.content_type,
                "collection_status": "collected",
                "mutable_during_run": False,
                "finalized_at": datetime.now(UTC).isoformat(),
            }
        ],
    )
    watch_store = SQLiteRuntimeWatchStore(
        database,
        segment_root=evidence_root / "task21-runtime-watch-segments",
    )
    watch_store.create_watch(run_id=run_id, owner="alice", connection_id="task21")
    evidence_query = EvidenceQueryService(store=run_store, evidence_store=evidence_store)
    api = Pilot107HttpApi(
        store=run_store,
        evidence_query=evidence_query,
        agent_explain_service=AgentExplainService(
            store=run_store,
            llm_provider=None,
            evidence_binder=EvidenceBinder(store=run_store, evidence_root=evidence_root),
        ),
        runtime_watch_routes=RuntimeWatchRoutes(watch_store),
    )
    headers = {"X-Pilot107-User": "alice"}
    run_response = api.handle_get(f"/api/v1/runs/{run_id}", headers=headers)
    evidence_response = api.handle_get(f"/api/v1/runs/{run_id}/evidence", headers=headers)
    watch_response = api.handle_get(f"/api/v1/runs/{run_id}/runtime-watch", headers=headers)
    suggest_response = api.handle_post(
        "/api/v1/contracts/agent/suggest",
        body=json.dumps(
            {
                "current_contract": {},
                "recipe_version_id": "recipe_python_cpu@1.0.0",
                "user_intent": "generate a contract patch",
                "provider": "local",
            }
        ).encode(),
        headers=headers,
    )
    deterministic_response = api.handle_post(
        "/api/v1/contracts/agent/suggest",
        body=json.dumps(
            {
                "current_contract": {},
                "recipe_version_id": "recipe_python_cpu@1.0.0",
                "user_intent": "use deterministic facts only",
                "provider": "none",
            }
        ).encode(),
        headers=headers,
    )
    if [run_response.status, evidence_response.status, watch_response.status] != [200, 200, 200]:
        raise RuntimeError("deterministic Run/Evidence/Watch reads became unavailable")
    if (
        suggest_response.status != 200
        or suggest_response.payload.get("status") != "degraded"
        or suggest_response.payload.get("reason") != "provider_unconfigured"
        or deterministic_response.status != 200
        or deterministic_response.payload.get("status") != "ok"
    ):
        raise RuntimeError("model-unavailable fallback did not fail closed")
    print(
        json.dumps(
            {
                "schema": "pilot107.agent-lifecycle-boundaries-live/v1",
                "model_unavailable_deterministic_fallback": True,
                "generative_project_state": current_project.state.value,
                "generative_turn_state": current_turn.state.value,
                "generative_error_code": (current_turn.error or {}).get("code"),
                "run_read_status": run_response.status,
                "evidence_read_status": evidence_response.status,
                "runtime_watch_read_status": watch_response.status,
                "generative_suggest_status": suggest_response.payload.get("status"),
                "deterministic_suggest_status": deterministic_response.payload.get("status"),
            },
            sort_keys=True,
        )
    )
    return 0


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("live-large-file", "live-model-unavailable"))
    arguments = parser.parse_args()
    if arguments.mode == "live-large-file":
        return live_large_file()
    return live_model_unavailable()


if __name__ == "__main__":
    raise SystemExit(main())
