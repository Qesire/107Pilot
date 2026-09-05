import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from pilot107.adapters.slurm import InMemorySlurmBackend
from pilot107.api.evidence_query import EvidenceQueryService
from pilot107.api.http_app import Pilot107HttpApi
from pilot107.core.advice import (
    AgentAdviceError,
    AgentAdviceService,
    AgentPolicyEngine,
)
from pilot107.core.agent import AgentExplainService
from pilot107.core.contracts import (
    ContractService,
    ContractStore,
    RecipeCatalog,
    RecipeVersion,
)
from pilot107.core.evidence_binding import EvidenceBinder
from pilot107.core.platform import load_capability_profile
from pilot107.core.run_service import RunService
from pilot107.core.run_store import RunRecord, RunStore
from pilot107.core.states import RunState
from pilot107.worker.evidence import EvidenceStore

ROOT = Path(__file__).resolve().parents[1]
CPU_RC_PROFILE_PATH = ROOT / "config" / "platform_profiles" / "cpu-only-8c16g.json"


class AgentAdviceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.store = RunStore(root / "pilot107.db")
        self.evidence_store = EvidenceStore(root / "evidence")
        self.contract_service = ContractService(
            catalog=RecipeCatalog(),
            store=ContractStore(root / "pilot107.db"),
        )
        self.backend = InMemorySlurmBackend()
        self.run_service = RunService(store=self.store, backend=self.backend)
        self.explain_service = AgentExplainService(
            store=self.store,
            evidence_binder=EvidenceBinder(
                store=self.store,
                evidence_root=self.evidence_store.root,
            ),
        )
        self.service = AgentAdviceService(
            store=self.store,
            explain_service=self.explain_service,
            policy_engine=AgentPolicyEngine(contract_service=self.contract_service),
            contract_service=self.contract_service,
            run_service=self.run_service,
        )

    def _profiled_service(self, *, profile) -> AgentAdviceService:
        """An AgentAdviceService whose policy engine has a capability profile
        and whose ContractService enforces partition_qos/qos_limits, so
        null-placeholder patches can resolve to concrete legal values and
        pass validation (allowed_preview). Uses a synthetic CPU-RC-compatible
        recipe so the partition compatibility check passes for CPU-RC.
        """
        profiled_contract_service = ContractService(
            catalog=RecipeCatalog(recipes=[_cpu_rc_recipe()]),
            store=ContractStore(Path(self._tmp.name) / "pilot107.db"),
            partition_qos=profile.partition_qos(),
            qos_limits=profile.qos_limits(),
        )
        return AgentAdviceService(
            store=self.store,
            explain_service=self.explain_service,
            policy_engine=AgentPolicyEngine(
                contract_service=profiled_contract_service,
                capability_profile=profile,
            ),
            contract_service=profiled_contract_service,
            run_service=self.run_service,
        )

    def _ready_run_profiled(
        self,
        *,
        patch: dict[str, Any],
        run_id: str,
        service: AgentAdviceService,
    ) -> RunRecord:
        """A _ready_run variant whose run carries a CPU-RC partition/QoS in
        its resource_plan, so the capability-profile resolver can match the
        profile and the profiled ContractService.validate accepts the
        resolved candidate. The contract is created via the profiled service's
        contract_service (which has the CPU-RC-compatible recipe catalog).
        """
        contract_service = service.contract_service
        assert contract_service is not None
        contract = contract_service.create(
            owner="alice", payload=_cpu_rc_contract_payload()
        )
        self.store.create_run(
            run_id=run_id,
            contract_id=contract.contract_id,
            owner="alice",
            workdir="/public/home/alice",
            script="#!/bin/bash\necho contract-ok\n",
            resource_plan={
                "partition": "CPU-RC",
                "qos": "qos_cpu_rc",
                "nodes": 1,
                "ntasks": 1,
                "cpus_per_task": 1,
                "time_limit": "00:05:00",
            },
        )
        artifact = self.evidence_store.write_text(
            run_id=run_id,
            logical_path="logs/stderr.tail.txt",
            content="TIME LIMIT\n",
            content_type="text/plain",
        )
        evidence_ref = f"evidence://runs/{run_id}/{artifact.logical_path}"
        self.store.upsert_evidence_objects(
            run_id,
            [
                {
                    "object_id": f"ev_{run_id}",
                    "category": "logs",
                    "logical_path": artifact.logical_path,
                    "store_path": str(artifact.path),
                    "source_uri": evidence_ref,
                    "sha256": artifact.sha256,
                    "size_bytes": artifact.size_bytes,
                    "mime_type": artifact.content_type,
                    "collection_status": "collected",
                    "mutable_during_run": False,
                }
            ],
        )
        self.store.replace_diagnoses(
            run_id,
            [
                {
                    "diagnosis_id": f"diag_{run_id}",
                    "rule_id": "RUNTIME.TIMEOUT",
                    "severity": "error",
                    "summary": "time limit exceeded",
                    "evidence_refs": [evidence_ref],
                    "suggested_patch": patch,
                    "retryable": True,
                    "confidence": "high",
                }
            ],
        )
        return self.store.get_run(run_id)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_rule_patch_is_preflighted_and_approved_with_audit_record(self) -> None:
        run = self._ready_run(patch={"resources.time_limit": "00:10:00"})

        result = self.service.advise(run.run_id, idempotency_key="request-1")
        action = result.record.payload["actions"][0]
        approved = self.service.approve(
            result.record.advice_id,
            expected_version=1,
            action_ids=[action["action_id"]],
            actor="alice",
        )

        self.assertTrue(result.created)
        self.assertEqual(result.record.state, "ready")
        self.assertEqual(action["policy_status"], "allowed_preview")
        self.assertEqual(approved.state, "approved")
        self.assertEqual(approved.version, 2)
        decisions = self.service.decisions(approved.advice_id)
        self.assertEqual(
            [(item.decision, item.actor) for item in decisions],
            [("approve", "alice")],
        )

    def test_idempotency_key_replays_the_original_advice(self) -> None:
        run = self._ready_run(patch={"resources.time_limit": "00:10:00"})

        first = self.service.advise(run.run_id, idempotency_key="same-key")
        second = self.service.advise(run.run_id, idempotency_key="same-key")

        self.assertTrue(first.created)
        self.assertFalse(second.created)
        self.assertEqual(second.record.advice_id, first.record.advice_id)

    def test_policy_blocks_unknown_fields_and_requires_concrete_values(self) -> None:
        blocked = self._ready_run(
            run_id="run_blocked",
            patch={"runtime.conda_env": "unsafe-env"},
        )
        unresolved = self._ready_run(
            run_id="run_unresolved",
            patch={"resources.qos": None},
        )

        blocked_advice = self.service.advise(blocked.run_id).record
        unresolved_advice = self.service.advise(unresolved.run_id).record

        self.assertEqual(blocked_advice.state, "no_safe_action")
        self.assertEqual(
            blocked_advice.payload["actions"][0]["reasons"],
            ["field_not_patchable:runtime.conda_env"],
        )
        # No capability_profile wired -> null stays requires_input (backward compat).
        self.assertEqual(unresolved_advice.state, "needs_input")
        self.assertEqual(
            unresolved_advice.payload["actions"][0]["reasons"],
            ["value_required:resources.qos"],
        )

    def test_profile_resolves_null_qos_to_default_qos(self) -> None:
        profile = load_capability_profile(CPU_RC_PROFILE_PATH)
        service = self._profiled_service(profile=profile)
        run = self._ready_run_profiled(
            run_id="run_qos_resolve",
            patch={"resources.qos": None},
            service=service,
        )

        advice = service.advise(run.run_id).record
        action = advice.payload["actions"][0]

        self.assertEqual(advice.state, "ready")
        self.assertEqual(action["policy_status"], "allowed_preview")
        self.assertEqual(action["proposed_patch"]["resources.qos"], "qos_cpu_rc")
        self.assertIn("resolved:resources.qos=qos_cpu_rc", action["resolution"])

    def test_profile_resolves_null_partition_memory_and_time_limit(self) -> None:
        profile = load_capability_profile(CPU_RC_PROFILE_PATH)
        service = self._profiled_service(profile=profile)

        # INVALID_PARTITION -> default_partition="CPU-RC".
        run_part = self._ready_run_profiled(
            run_id="run_part_resolve",
            patch={"resources.partition": None},
            service=service,
        )
        action_part = service.advise(run_part.run_id).record.payload["actions"][0]
        self.assertEqual(action_part["policy_status"], "allowed_preview")
        self.assertEqual(action_part["proposed_patch"]["resources.partition"], "CPU-RC")

        # OOM resolves to the bounded VM-local Slurm memory envelope.
        run_oom = self._ready_run_profiled(
            run_id="run_oom_resolve",
            patch={"resources.memory": None},
            service=service,
        )
        action_oom = service.advise(run_oom.run_id).record.payload["actions"][0]
        self.assertEqual(action_oom["policy_status"], "allowed_preview")
        self.assertEqual(action_oom["proposed_patch"]["resources.memory"], "10G")

        # TIMEOUT -> max_wall_hours=4 -> "04:00:00".
        run_to = self._ready_run_profiled(
            run_id="run_timeout_resolve",
            patch={"resources.time_limit": None},
            service=service,
        )
        action_to = service.advise(run_to.run_id).record.payload["actions"][0]
        self.assertEqual(action_to["policy_status"], "allowed_preview")
        self.assertEqual(
            action_to["proposed_patch"]["resources.time_limit"], "04:00:00"
        )

    def test_profile_does_not_resolve_command_or_conda_env(self) -> None:
        profile = load_capability_profile(CPU_RC_PROFILE_PATH)
        service = self._profiled_service(profile=profile)

        # entry.command has no capability source -> stays requires_input.
        run_cmd = self._ready_run(
            run_id="run_cmd_resolve",
            patch={"entry.command": None},
        )
        action_cmd = service.advise(run_cmd.run_id).record.payload["actions"][0]
        self.assertEqual(action_cmd["policy_status"], "requires_input")
        self.assertEqual(action_cmd["proposed_patch"]["entry.command"], None)

        # runtime.conda_env is not patchable -> blocked even with a profile.
        run_conda = self._ready_run(
            run_id="run_conda_resolve",
            patch={"runtime.conda_env": None},
        )
        action_conda = service.advise(run_conda.run_id).record.payload["actions"][0]
        self.assertEqual(action_conda["policy_status"], "blocked")
        self.assertEqual(
            action_conda["reasons"], ["field_not_patchable:runtime.conda_env"]
        )

    def test_policy_uses_canonical_v2_memory_field(self) -> None:
        legacy = self._ready_run(
            run_id="run_legacy_memory",
            patch={"resources.memory_value": 4096},
        )
        canonical = self._ready_run(
            run_id="run_v2_memory",
            patch={"resources.memory": "4G"},
        )

        legacy_action = self.service.advise(legacy.run_id).record.payload["actions"][0]
        canonical_action = self.service.advise(canonical.run_id).record.payload["actions"][0]

        self.assertEqual(legacy_action["policy_status"], "blocked")
        self.assertEqual(
            legacy_action["reasons"],
            ["field_not_patchable:resources.memory_value"],
        )
        self.assertEqual(canonical_action["policy_status"], "allowed_preview")
        self.assertEqual(
            canonical_action["candidate"]["contract"]["resources"]["memory"],
            "4G",
        )

    def test_approval_invalidates_advice_when_run_changes(self) -> None:
        run = self._ready_run(patch={"resources.time_limit": "00:10:00"})
        advice = self.service.advise(run.run_id).record
        self.store.update_state(run.run_id, RunState.FAILED, event_type="test.changed")

        with self.assertRaisesRegex(AgentAdviceError, "changed") as raised:
            self.service.approve(
                advice.advice_id,
                expected_version=1,
                action_ids=[advice.payload["actions"][0]["action_id"]],
                actor="alice",
            )

        self.assertEqual(raised.exception.code, "AGENT.ADVICE_STALE")
        self.assertEqual(self.service.get(advice.advice_id).state, "stale")

    def test_approval_rejects_policy_bypass_and_old_version(self) -> None:
        run = self._ready_run(patch={"resources.time_limit": "00:10:00"})
        advice = self.service.advise(run.run_id).record

        with self.assertRaises(AgentAdviceError) as bypass:
            self.service.approve(
                advice.advice_id,
                expected_version=1,
                action_ids=["action_injected"],
                actor="alice",
            )
        self.assertEqual(bypass.exception.code, "AGENT.POLICY_DENIED")

        rejected = self.service.reject(
            advice.advice_id,
            expected_version=1,
            actor="alice",
        )
        self.assertEqual(rejected.state, "rejected")
        with self.assertRaises(AgentAdviceError) as replay:
            self.service.reject(advice.advice_id, expected_version=1, actor="alice")
        self.assertEqual(replay.exception.code, "AGENT.ADVICE_CONFLICT")

    def test_approved_action_prepares_then_submits_one_derived_run(self) -> None:
        source = self._ready_run(patch={"resources.time_limit": "00:10:00"})
        advice = self.service.advise(source.run_id).record
        action_id = advice.payload["actions"][0]["action_id"]
        self.service.approve(
            advice.advice_id,
            expected_version=1,
            action_ids=[action_id],
            actor="alice",
        )

        prepared = self.service.execute_action(
            advice.advice_id,
            action_id=action_id,
            actor="alice",
            submit=False,
        )
        submitted = self.service.execute_action(
            advice.advice_id,
            action_id=action_id,
            actor="alice",
            submit=True,
        )
        replay = self.service.execute_action(
            advice.advice_id,
            action_id=action_id,
            actor="alice",
            submit=True,
        )

        self.assertEqual(prepared.state, "prepared")
        self.assertEqual(submitted.state, "submitted")
        self.assertEqual(replay.execution_id, submitted.execution_id)
        self.assertEqual(len(self.service.executions(advice.advice_id)), 1)
        derived_contract = self.contract_service.get(submitted.derived_contract_id or "")
        derived_run = self.store.get_run(submitted.run_id or "")
        self.assertEqual(derived_contract.parent_contract_id, source.contract_id)
        self.assertEqual(derived_contract.source_advice_id, advice.advice_id)
        self.assertEqual(derived_contract.source_action_id, action_id)
        self.assertEqual(derived_contract.payload["resources"]["time_limit"], "00:10:00")
        self.assertEqual(derived_run.parent_run_id, source.run_id)
        self.assertEqual(derived_run.lineage_reason, "agent_remediation")
        self.assertEqual(derived_run.state, RunState.SUBMITTED)

    def test_action_execution_requires_approval_and_current_evidence(self) -> None:
        source = self._ready_run(patch={"resources.time_limit": "00:10:00"})
        advice = self.service.advise(source.run_id).record
        action_id = advice.payload["actions"][0]["action_id"]

        with self.assertRaises(AgentAdviceError) as not_approved:
            self.service.execute_action(
                advice.advice_id,
                action_id=action_id,
                actor="alice",
            )
        self.assertEqual(not_approved.exception.code, "AGENT.NOT_APPROVED")

        self.service.approve(
            advice.advice_id,
            expected_version=1,
            action_ids=[action_id],
            actor="alice",
        )
        self.store.update_state(source.run_id, RunState.FAILED, event_type="test.changed")
        with self.assertRaises(AgentAdviceError) as stale:
            self.service.execute_action(
                advice.advice_id,
                action_id=action_id,
                actor="alice",
            )
        self.assertEqual(stale.exception.code, "AGENT.APPROVED_ACTION_STALE")

    def test_http_advice_flow_and_owner_authorization(self) -> None:
        run = self._ready_run(patch={"resources.time_limit": "00:10:00"})
        api = Pilot107HttpApi(
            store=self.store,
            evidence_query=EvidenceQueryService(
                store=self.store,
                evidence_store=self.evidence_store,
            ),
            contract_service=self.contract_service,
            agent_explain_service=self.explain_service,
            agent_advice_service=self.service,
            auth_required=True,
        )

        denied = api.handle_post(
            f"/api/v1/runs/{run.run_id}/agent/advise",
            body=b"{}",
            headers={"X-Pilot107-User": "bob"},
        )
        created = api.handle_post(
            f"/api/v1/runs/{run.run_id}/agent/advise",
            body=json.dumps({"idempotency_key": "http-1"}).encode(),
            headers={"X-Pilot107-User": "alice"},
        )
        fetched = api.handle_get(
            f"/api/v1/agent/advice/{created.payload['advice_id']}",
            headers={"X-Pilot107-User": "alice"},
        )
        action_id = created.payload["actions"][0]["action_id"]
        approved = api.handle_post(
            f"/api/v1/agent/advice/{created.payload['advice_id']}/approve",
            body=json.dumps({"expected_version": 1, "action_ids": [action_id]}).encode(),
            headers={"X-Pilot107-User": "alice"},
        )
        denied_execute = api.handle_post(
            f"/api/v1/agent/advice/{created.payload['advice_id']}/actions/"
            f"{action_id}/execute",
            body=b"{}",
            headers={"X-Pilot107-User": "bob"},
        )
        executed = api.handle_post(
            f"/api/v1/agent/advice/{created.payload['advice_id']}/actions/"
            f"{action_id}/execute",
            body=json.dumps({"submit": True}).encode(),
            headers={"X-Pilot107-User": "alice"},
        )

        self.assertEqual(denied.status, 403)
        self.assertEqual(created.status, 201)
        self.assertEqual(fetched.status, 200)
        self.assertEqual(approved.status, 200)
        self.assertEqual(approved.payload["state"], "approved")
        self.assertEqual(approved.payload["decisions"][0]["actor"], "alice")
        self.assertEqual(denied_execute.status, 403)
        self.assertEqual(executed.status, 200)
        self.assertEqual(executed.payload["state"], "submitted")
        self.assertIsNotNone(executed.payload["derived_contract_id"])
        self.assertIsNotNone(executed.payload["run_id"])

    def _ready_run(
        self,
        *,
        patch: dict[str, Any],
        run_id: str = "run_advice",
    ) -> RunRecord:
        contract = self.contract_service.create(owner="alice", payload=_contract_payload())
        self.store.create_run(
            run_id=run_id,
            contract_id=contract.contract_id,
            owner="alice",
            workdir="/public/home/alice",
            script="#!/bin/bash\necho contract-ok\n",
        )
        artifact = self.evidence_store.write_text(
            run_id=run_id,
            logical_path="logs/stderr.tail.txt",
            content="TIME LIMIT\n",
            content_type="text/plain",
        )
        evidence_ref = f"evidence://runs/{run_id}/{artifact.logical_path}"
        self.store.upsert_evidence_objects(
            run_id,
            [
                {
                    "object_id": f"ev_{run_id}",
                    "category": "logs",
                    "logical_path": artifact.logical_path,
                    "store_path": str(artifact.path),
                    "source_uri": evidence_ref,
                    "sha256": artifact.sha256,
                    "size_bytes": artifact.size_bytes,
                    "mime_type": artifact.content_type,
                    "collection_status": "collected",
                    "mutable_during_run": False,
                }
            ],
        )
        self.store.replace_diagnoses(
            run_id,
            [
                {
                    "diagnosis_id": f"diag_{run_id}",
                    "rule_id": "RUNTIME.TIMEOUT",
                    "severity": "error",
                    "summary": "time limit exceeded",
                    "evidence_refs": [evidence_ref],
                    "suggested_patch": patch,
                    "retryable": True,
                    "confidence": "high",
                }
            ],
        )
        return self.store.get_run(run_id)


def _contract_payload() -> dict[str, Any]:
    return {
        "recipe_version_id": "recipe_python_cpu@1.0.0",
        "project": {"workdir": "/public/home/alice"},
        "entry": {"command": "echo contract-ok", "expected_outputs": ["result.txt"]},
        "resources": {
            "partition": "debug",
            "qos": "normal",
            "nodes": 1,
            "ntasks": 1,
            "cpus_per_task": 1,
            "time_limit": "00:05:00",
        },
    }


def _cpu_rc_contract_payload() -> dict[str, Any]:
    """A contract payload that validates against the CPU-RC capability
    profile (CPU-RC partition + qos_cpu_rc), used by profiled advice tests so
    the resolved candidate passes ContractService.validate. Uses the synthetic
    CPU-RC-compatible recipe (see _cpu_rc_recipe).
    """
    payload = _contract_payload()
    payload["recipe_version_id"] = "recipe_cpu_rc_test@1.0.0"
    payload["resources"]["partition"] = "CPU-RC"
    payload["resources"]["qos"] = "qos_cpu_rc"
    payload["resources"]["memory"] = "4G"
    payload["resources"]["time_limit"] = "00:05:00"
    return payload


def _cpu_rc_recipe() -> RecipeVersion:
    """A synthetic recipe whose compatibility allows the CPU-RC partition,
    so the profiled ContractService.validate does not BLOCK with
    RECIPE.PARTITION_INCOMPATIBLE. Mirrors _python_cpu_recipe's schema but
    with CPU-RC + qos_cpu_rc in allowed partitions/QoS.
    """
    return RecipeVersion(
        recipe_id="recipe_cpu_rc_test",
        version="1.0.0",
        title="CPU-RC test recipe",
        description="Synthetic recipe for profile-aware advice tests on CPU-RC.",
        trust_level="L1",
        parameter_schema={
            "required": [
                "project.workdir",
                "entry.command",
                "resources.partition",
                "resources.time_limit",
            ],
            "entry.command": {"type": "plain_shell_command", "raw_shell": False},
        },
        compatibility={
            "slurm": {"min_version": "23.0"},
            "platform": {"docker_l2": True, "school_l3": False, "requires_gpu": False},
            "partitions": {
                "default": "CPU-RC",
                "allowed": ["CPU-RC", "debug"],
            },
            "qos": {
                "default": "qos_cpu_rc",
                "allowed_by_partition": {"CPU-RC": ("qos_cpu_rc",)},
            },
        },
        risk_declaration={
            "blocks": ["empty command", "missing workdir", "invalid resource plan"],
            "warns": ["rm -rf", "curl|bash", "background process"],
        },
    )


if __name__ == "__main__":
    unittest.main()
