"""Derive template verification facts from adopted Contracts and Run Evidence."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from pilot107.core.contract_v2 import ContractV2Error, parse_expected_output
from pilot107.core.run_store import EvidenceObjectRecord, RunStore
from pilot107.core.states import CapsuleState, CollectionState, ResultStatus, RunState
from pilot107.core.template_market import (
    TemplateMarketError,
    TemplateMarketStore,
    TemplateVerificationRecord,
)
from pilot107.worker.capsule import (
    CapsuleError,
    capsule_authority_from_store,
    verify_raw_capsule,
)

_ENVIRONMENTS = frozenset({"docker", "real107_cpu", "real107_gpu"})
_REQUIRED_EVIDENCE = frozenset(
    {
        "manifest/manifest.json",
        "slurm/accounting.json",
        "derived/result_summary.v1.json",
    }
)


class TemplateVerificationService:
    def __init__(
        self,
        *,
        template_store: TemplateMarketStore,
        run_store: RunStore,
        environment: str,
        capsule_root: Path,
    ) -> None:
        if environment not in _ENVIRONMENTS:
            raise ValueError(f"unsupported template verification environment: {environment}")
        self.template_store = template_store
        self.run_store = run_store
        self.environment = environment
        self.capsule_root = capsule_root

    def verify_from_run(
        self,
        *,
        release_id: str,
        run_id: str,
        actor: str,
        request_key: str,
    ) -> TemplateVerificationRecord:
        release = self.template_store.get_release(release_id)
        run = self.run_store.get_run(run_id)
        if run.owner != actor:
            raise TemplateMarketError(
                "verification Run is not owned by this actor",
                code="TEMPLATE.FORBIDDEN",
            )
        if run.contract_id is None:
            raise TemplateMarketError(
                "verification Run is not bound to a Contract",
                code="TEMPLATE.VERIFICATION_LINEAGE_INVALID",
            )
        try:
            adoption = self.template_store.get_adoption_for_contract(
                release_id=release.release_id,
                adopter=actor,
                contract_id=run.contract_id,
            )
        except KeyError as exc:
            raise TemplateMarketError(
                "verification Run does not descend from this release adoption",
                code="TEMPLATE.VERIFICATION_LINEAGE_INVALID",
            ) from exc
        status = _derived_status(run.state, run.result_status)
        if run.collection_state not in {
            CollectionState.SUCCEEDED,
            CollectionState.DEGRADED,
        }:
            raise TemplateMarketError(
                "verification Evidence collection is not complete",
                code="TEMPLATE.VERIFICATION_EVIDENCE_INCOMPLETE",
            )
        if run.capsule_state != CapsuleState.READY:
            raise TemplateMarketError(
                "verification Capsule is not ready",
                code="TEMPLATE.VERIFICATION_CAPSULE_INCOMPLETE",
            )
        capsule_dir = (self.capsule_root / "runs" / run.run_id / "raw").absolute()
        try:
            capsule_authority = capsule_authority_from_store(self.run_store, run_id)
        except CapsuleError as exc:
            raise TemplateMarketError(
                "verification Capsule authority is unavailable",
                code="TEMPLATE.VERIFICATION_CAPSULE_INCOMPLETE",
            ) from exc
        capsule_check = verify_raw_capsule(capsule_dir, authority=capsule_authority)
        if not capsule_check.valid:
            raise TemplateMarketError(
                "verification Capsule failed integrity validation",
                code="TEMPLATE.VERIFICATION_CAPSULE_INCOMPLETE",
            )
        capsule_manifest_sha256 = hashlib.sha256(
            (capsule_dir / "manifest.json").read_bytes()
        ).hexdigest()
        evidence = self.run_store.list_evidence_objects(run_id)
        evidence_by_path = {item.logical_path: item for item in evidence}
        required_paths = set(_REQUIRED_EVIDENCE)
        expected_outputs = _expected_output_paths(release.payload)
        if expected_outputs:
            _require_expected_outputs_in_inventory(evidence_by_path, expected_outputs)
            required_paths.add("outputs/inventory.json")
        if self.environment == "real107_gpu":
            requested_gpus = max(
                int(run.resource_plan.get("gpus_total") or 0),
                int(run.resource_plan.get("gpus_per_node") or 0),
            )
            if requested_gpus <= 0:
                raise TemplateMarketError(
                    "real107_gpu verification requires a GPU Contract Run",
                    code="TEMPLATE.VERIFICATION_ENVIRONMENT_MISMATCH",
                )
            required_paths.add("environment/summary.json")
        selected = _require_final_evidence(evidence_by_path, required_paths)
        digest_payload = {
            item.logical_path: item.sha256
            for item in sorted(selected, key=lambda item: item.logical_path)
        }
        evidence_sha256 = hashlib.sha256(
            json.dumps(digest_payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return self.template_store.create_verification(
            release_id=release.release_id,
            run_id=run.run_id,
            environment=self.environment,
            status=status,
            evidence_ref=f"evidence://runs/{run.run_id}/manifest/manifest.json",
            evidence_sha256=evidence_sha256,
            verified_by=actor,
            request_key=request_key,
            detail={
                "adoption_id": adoption.adoption_id,
                "contract_id": run.contract_id,
                "run_state": run.state.value,
                "result_status": run.result_status.value,
                "collection_state": run.collection_state.value,
                "capsule_state": run.capsule_state.value,
                "capsule_id": capsule_check.capsule_id,
                "capsule_manifest_sha256": capsule_manifest_sha256,
                "evidence_paths": sorted(required_paths),
                "expected_outputs": sorted(expected_outputs),
            },
        )


def _derived_status(state: RunState, result_status: ResultStatus) -> str:
    if state == RunState.SUCCEEDED and result_status == ResultStatus.COMPLETE:
        return "passed"
    if state in {RunState.FAILED, RunState.CANCELLED} and result_status in {
        ResultStatus.INCOMPLETE,
        ResultStatus.INVALID,
    }:
        return "failed"
    raise TemplateMarketError(
        "Run is not a verifiable terminal result",
        code="TEMPLATE.VERIFICATION_RUN_NOT_READY",
    )


def _require_final_evidence(
    evidence_by_path: dict[str, EvidenceObjectRecord],
    required_paths: set[str],
) -> list[EvidenceObjectRecord]:
    selected: list[EvidenceObjectRecord] = []
    for path in sorted(required_paths):
        item = evidence_by_path.get(path)
        if (
            item is None
            or item.collection_status != "collected"
            or item.sha256 is None
            or item.finalized_at is None
        ):
            raise TemplateMarketError(
                f"required finalized Evidence is missing: {path}",
                code="TEMPLATE.VERIFICATION_EVIDENCE_INCOMPLETE",
            )
        selected.append(item)
    return selected


def _expected_output_paths(payload: dict[str, object]) -> set[str]:
    outputs = payload.get("outputs")
    if not isinstance(outputs, dict):
        return set()
    raw_expected = outputs.get("expected")
    if not isinstance(raw_expected, list):
        return set()
    try:
        return {parse_expected_output(item) for item in raw_expected}
    except ContractV2Error as exc:
        raise TemplateMarketError(
            "release has invalid expected output declarations",
            code="TEMPLATE.VERIFICATION_LINEAGE_INVALID",
        ) from exc


def _require_expected_outputs_in_inventory(
    evidence_by_path: dict[str, EvidenceObjectRecord],
    expected_outputs: set[str],
) -> None:
    inventory = evidence_by_path.get("outputs/inventory.json")
    if (
        inventory is None
        or inventory.collection_status != "collected"
        or inventory.sha256 is None
        or inventory.finalized_at is None
    ):
        raise TemplateMarketError(
            "expected-output inventory is not finalized",
            code="TEMPLATE.VERIFICATION_EVIDENCE_INCOMPLETE",
        )
    try:
        payload = json.loads(Path(inventory.store_path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TemplateMarketError(
            "expected-output inventory cannot be read",
            code="TEMPLATE.VERIFICATION_EVIDENCE_INCOMPLETE",
        ) from exc
    files = payload.get("files") if isinstance(payload, dict) else None
    if not isinstance(files, list):
        raise TemplateMarketError(
            "expected-output inventory is invalid",
            code="TEMPLATE.VERIFICATION_EVIDENCE_INCOMPLETE",
        )
    by_path = {
        str(item.get("relative_path")): item
        for item in files
        if isinstance(item, dict) and item.get("relative_path") is not None
    }
    for expected_path in expected_outputs:
        item = by_path.get(expected_path)
        status = "missing" if item is None else str(item.get("attribution") or "unknown")
        final_sha = None if item is None else item.get("final_sha256")
        if status not in {"created", "modified"} or not isinstance(final_sha, str):
            raise TemplateMarketError(
                "declared expected output is not newly verified: "
                f"{expected_path} (status={status}, final_sha256={final_sha})",
                code="TEMPLATE.VERIFICATION_EVIDENCE_INCOMPLETE",
            )
