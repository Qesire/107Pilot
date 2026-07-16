#!/usr/bin/env python3
"""Smoke test the optional campus/USTC LLM explanation provider.

This script deliberately reads only PILOT107_LLM_* environment variables. It
does not read personal opencode configuration and never prints API keys.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

from pilot107.adapters.slurm import JobSnapshot, SubmissionStrategy, SubmitReceipt
from pilot107.core.agent import (
    AgentExplainService,
    AgentProviderError,
    OpenAICompatibleLLMProvider,
)
from pilot107.core.evidence_binding import EvidenceBinder
from pilot107.core.run_store import RunRecord, RunStore
from pilot107.core.states import RunState
from pilot107.worker.evidence import EvidenceStore

REQUIRED_ENV = (
    "PILOT107_LLM_BASE_URL",
    "PILOT107_LLM_MODEL",
)


def main() -> int:
    missing = [name for name in REQUIRED_ENV if not os.environ.get(name)]
    if missing:
        message = (
            "campus llm smoke skipped: missing "
            + ", ".join(missing)
            + "; set PILOT107_REQUIRE_LLM_SMOKE=1 to fail on missing config"
        )
        print(message)
        return 2 if os.environ.get("PILOT107_REQUIRE_LLM_SMOKE") == "1" else 0

    with tempfile.TemporaryDirectory(prefix="pilot107-campus-llm-") as tmpdir:
        root = Path(tmpdir)
        store = RunStore(root / "pilot107.db")
        evidence_store = EvidenceStore(root / "evidence")
        run = _seed_failed_run(store, evidence_store)
        provider = OpenAICompatibleLLMProvider.from_env()
        service = AgentExplainService(
            store=store,
            llm_provider=provider,
            evidence_binder=EvidenceBinder(store=store, evidence_root=evidence_store.root),
        )

        try:
            explanation = service.explain(run.run_id, provider="local")
        except AgentProviderError as exc:
            print(f"campus llm smoke failed: {exc}", file=sys.stderr)
            return 1

    if explanation.provider != "local":
        print("campus llm smoke failed: provider was not local", file=sys.stderr)
        return 1
    if explanation.status != "explained":
        print(f"campus llm smoke failed: status={explanation.status}", file=sys.stderr)
        return 1
    if explanation.model != os.environ["PILOT107_LLM_MODEL"]:
        print("campus llm smoke failed: response model did not match config", file=sys.stderr)
        return 1
    if not explanation.facts:
        print("campus llm smoke failed: no evidence-bound facts returned", file=sys.stderr)
        return 1
    if not explanation.narrative or any(
        warning.startswith("local_llm_fallback:") for warning in explanation.warnings
    ):
        fallback_codes = [
            warning.removeprefix("local_llm_fallback:")
            for warning in explanation.warnings
            if warning.startswith("local_llm_fallback:")
        ]
        detail = ",".join(fallback_codes) or "missing_narrative"
        print(
            f"campus llm smoke failed: model output failed validation: {detail}",
            file=sys.stderr,
        )
        return 1

    print(
        "campus llm smoke ok: "
        f"run_id={explanation.run_id} "
        f"provider={explanation.provider} "
        f"model={explanation.model} "
        f"facts={len(explanation.facts)} "
        f"narrative_chars={len(explanation.narrative or '')} "
        f"recommendations={len(explanation.recommendations)} "
        f"warnings={len(explanation.warnings)}"
    )
    return 0


def _seed_failed_run(store: RunStore, evidence_store: EvidenceStore) -> RunRecord:
    run = store.create_run(
        run_id="run_campus_llm_smoke",
        owner="alice",
        workdir="/public/home/alice/pilot107-smoke",
        script="#!/bin/bash\npython train.py\n",
        resource_plan={
            "partition": "gpu",
            "gres": "gpu:1",
            "time_limit_minutes": 5,
        },
    )
    store.apply_submit_receipt(
        run.run_id,
        SubmitReceipt(
            job_id="9001",
            run_state=RunState.SUBMITTED,
            strategy=SubmissionStrategy.COMMAND,
            raw_response={"smoke": "campus_llm"},
        ),
    )
    failed = store.apply_snapshot(
        run.run_id,
        JobSnapshot(
            job_id="9001",
            owner="alice",
            run_state=RunState.FAILED,
            raw_state_flags=["FAILED"],
            exit_code="1:0",
            reason="NonZeroExitCode",
        ),
    )
    artifacts = [
        evidence_store.write_text(
            run_id=run.run_id,
            logical_path="logs/stderr.tail.txt",
            content="ModuleNotFoundError: No module named 'torch'\n",
            content_type="text/plain",
        ),
        evidence_store.write_text(
            run_id=run.run_id,
            logical_path="environment/python.txt",
            content="Python 3.12\n",
            content_type="text/plain",
        ),
    ]
    store.upsert_evidence_objects(
        run.run_id,
        [
            {
                "object_id": f"ev_smoke_{index}",
                "category": artifact.logical_path.split("/", 1)[0],
                "logical_path": artifact.logical_path,
                "store_path": str(artifact.path),
                "source_uri": f"evidence://runs/{run.run_id}/{artifact.logical_path}",
                "sha256": artifact.sha256,
                "size_bytes": artifact.size_bytes,
                "mime_type": artifact.content_type,
                "collection_status": "collected",
                "mutable_during_run": False,
            }
            for index, artifact in enumerate(artifacts, start=1)
        ],
    )
    store.replace_diagnoses(
        run.run_id,
        [
            {
                "diagnosis_id": "diag_python_package_missing",
                "rule_id": "RUNTIME.PYTHON_PACKAGE_MISSING",
                "severity": "error",
                "summary": "Python 运行环境缺少作业需要的包。",
                "evidence_refs": [
                    f"evidence://runs/{run.run_id}/logs/stderr.tail.txt",
                    f"evidence://runs/{run.run_id}/environment/python.txt",
                ],
                "suggested_patch": {
                    "runtime.conda_env": "在环境中安装缺失包后重新提交",
                },
                "retryable": True,
                "confidence": "high",
            }
        ],
    )
    return failed


if __name__ == "__main__":
    raise SystemExit(main())
