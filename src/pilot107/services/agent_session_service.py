"""Application service for durable Agent Session and Turn submission."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from pilot107.agent.project import (
    EXPERIMENT_BUILDER_PROFILE,
    RUN_DIAGNOSIS_REPAIR_PROFILE,
    is_project_agent_profile,
)
from pilot107.agent.session import AgentSessionRecord, AgentTurnRecord
from pilot107.agent.store import AgentSessionStore
from pilot107.agent.tasks import AgentResourceEnvelope
from pilot107.core.contracts import ContractRecord
from pilot107.core.control_repository import ControlRepository, OutboxMessage
from pilot107.core.evidence_binding import EvidenceBinder
from pilot107.core.run_store import EvidenceObjectRecord, RunRecord
from pilot107.core.states import TERMINAL_RUN_STATES, CollectionState
from pilot107.runtime_watch.model import RuntimeWatchRecord, RuntimeWatchState

AGENT_TURN_TOPIC = "agent.turn.execute.v1"
FORMAL_RESULT_TOPIC = "agent.formal-result.v1"


class FormalResultRunStore(Protocol):
    def get_run(self, run_id: str) -> RunRecord: ...

    def list_evidence_objects(self, run_id: str) -> list[EvidenceObjectRecord]: ...


class FormalResultWatchStore(Protocol):
    def get_watch_for_run(self, run_id: str, *, owner: str) -> RuntimeWatchRecord: ...


@dataclass(frozen=True)
class FormalResultDispatchBatch:
    checked: int
    succeeded: int
    errors: tuple[str, ...] = ()


class AgentSessionService:
    def __init__(
        self,
        *,
        store: AgentSessionStore,
        control_repository: ControlRepository,
    ) -> None:
        self.store = store
        self.control_repository = control_repository

    def create_session(
        self,
        *,
        owner: str,
        request_key: str,
        model_profile_id: str,
        source: Mapping[str, object],
        profile_id: str = "hpc-readonly-v1",
    ) -> tuple[AgentSessionRecord, bool]:
        if profile_id not in {
            "hpc-readonly-v1",
            "platform_coach",
            EXPERIMENT_BUILDER_PROFILE,
            RUN_DIAGNOSIS_REPAIR_PROFILE,
        }:
            raise ValueError("Agent profile is not supported")
        project_profile = is_project_agent_profile(profile_id)
        expected_source_keys = (
            {"project_id", "workspace_id"}
            if profile_id == EXPERIMENT_BUILDER_PROFILE
            else {
                "project_id",
                "workspace_id",
                "run_id",
                "remediation_session_id",
            }
        )
        if project_profile and (
            set(source)
            not in (
                expected_source_keys,
                expected_source_keys | {"resource_envelope"},
            )
            or any(
                not isinstance(source.get(key), str) or not source.get(key)
                for key in expected_source_keys
            )
        ):
            raise ValueError(f"{profile_id} requires its exact Project source bindings")
        normalized_source = dict(source)
        envelope_value = normalized_source.get("resource_envelope")
        if not project_profile and envelope_value is not None:
            raise ValueError("resource envelope requires a Project profile")
        if project_profile and envelope_value is not None:
            if not isinstance(envelope_value, Mapping):
                raise ValueError("Project resource envelope is invalid")
            envelope = AgentResourceEnvelope(**dict(envelope_value))
            if envelope.approved_by != owner:
                raise ValueError("Project resource envelope owner is invalid")
            normalized_source["resource_envelope"] = _envelope_payload(envelope)
        return self.store.create_session(
            owner=owner,
            request_key=request_key,
            profile_id=profile_id,
            model_profile_id=model_profile_id,
            source=normalized_source,
        )

    def submit_message(
        self,
        *,
        session_id: str,
        owner: str,
        request_key: str,
        message: str,
        expected_state_version: int,
    ) -> tuple[AgentTurnRecord, bool]:
        turn, created = self.store.create_turn(
            session_id=session_id,
            owner=owner,
            request_key=request_key,
            message=message,
            expected_state_version=expected_state_version,
        )
        self._enqueue(turn)
        return turn, created

    def recover_pending_turns(self, *, limit: int = 100) -> int:
        created = 0
        for turn in self.store.list_recoverable_turns(limit=limit):
            _, was_created = self._enqueue(turn)
            created += int(was_created)
        return created

    def enqueue_result_explanation(
        self,
        *,
        session_id: str,
        owner: str,
        run_id: str,
        evidence_bundle_sha256: str,
    ) -> AgentTurnRecord:
        """Enqueue exactly one evidence-bound explanation Turn for a formal Run."""

        session = self.store.get_session(session_id, owner=owner)
        message = (
            f"Explain terminal formal Run {run_id} using Evidence bundle "
            f"sha256:{evidence_bundle_sha256}. Report scheduler and collection facts, "
            "but state explicitly that scheduler success does not establish scientific validity."
        )
        turn, _ = self.submit_message(
            session_id=session_id,
            owner=owner,
            request_key=f"formal-run:{run_id}:result-explanation",
            message=message,
            expected_state_version=session.state_version,
        )
        return turn

    def enqueue_formal_result_handoff(
        self,
        *,
        run: RunRecord,
        contract: ContractRecord,
    ) -> tuple[OutboxMessage, bool]:
        """Persist the post-Watch handoff for one terminal formal Run."""

        if (
            run.state not in TERMINAL_RUN_STATES
            or run.lineage_reason != "agent_formal_run"
            or run.contract_id != contract.contract_id
            or contract.derivation_reason != "agent_formal_run"
        ):
            raise ValueError("formal result handoff requires terminal formal lineage")
        binding = _formal_approval_binding(contract)
        if binding["approved_by"] != run.owner or binding["validation_run_id"] != run.parent_run_id:
            raise ValueError("formal result handoff lineage binding is invalid")
        return self.control_repository.enqueue(
            message_id=f"agent-formal-result:{run.run_id}",
            topic=FORMAL_RESULT_TOPIC,
            aggregate_id=run.run_id,
            payload={
                "run_id": run.run_id,
                "owner": run.owner,
                "session_id": binding["session_id"],
                "approval_digest": binding["approval_digest"],
            },
        )

    def dispatch_formal_result_handoffs(
        self,
        *,
        run_store: FormalResultRunStore,
        runtime_watch_store: FormalResultWatchStore,
        evidence_binder: EvidenceBinder,
        worker_id: str,
        limit: int = 100,
    ) -> FormalResultDispatchBatch:
        """Turn drained Watches and collected Evidence into idempotent explanation Turns."""

        messages = self.control_repository.claim_outbox(
            owner=worker_id,
            limit=limit,
            lease_seconds=60,
            topics=(FORMAL_RESULT_TOPIC,),
        )
        succeeded = 0
        errors: list[str] = []
        for message in messages:
            try:
                payload = message.payload
                if set(payload) != {"run_id", "owner", "session_id", "approval_digest"}:
                    raise ValueError("formal result handoff payload is invalid")
                run_id = _required_handoff_string(payload, "run_id")
                owner = _required_handoff_string(payload, "owner")
                session_id = _required_handoff_string(payload, "session_id")
                _required_handoff_string(payload, "approval_digest")
                if run_id != message.aggregate_id:
                    raise ValueError("formal result handoff aggregate is invalid")
                run = run_store.get_run(run_id)
                if (
                    run.owner != owner
                    or run.state not in TERMINAL_RUN_STATES
                    or run.lineage_reason != "agent_formal_run"
                    or run.collection_state
                    not in {CollectionState.SUCCEEDED, CollectionState.DEGRADED}
                ):
                    raise ValueError("formal result Evidence is not terminal")
                watch = runtime_watch_store.get_watch_for_run(run_id, owner=owner)
                if watch.state is not RuntimeWatchState.STOPPED:
                    raise ValueError("formal result Watch has not drained")
                refs = tuple(
                    _evidence_ref(run_id, item)
                    for item in run_store.list_evidence_objects(run_id)
                    if _explainable_evidence(item)
                )
                bundle = evidence_binder.bind(run_id, refs)
                if not bundle.objects or bundle.rejected_refs:
                    raise ValueError("formal result Evidence is incomplete or untrusted")
                self.enqueue_result_explanation(
                    session_id=session_id,
                    owner=owner,
                    run_id=run_id,
                    evidence_bundle_sha256=bundle.sha256,
                )
                if message.lease_owner is None:
                    raise RuntimeError("formal result handoff has no active lease")
                self.control_repository.acknowledge(
                    message_id=message.message_id,
                    owner=message.lease_owner,
                    fencing_token=message.fencing_token,
                )
            except Exception as exc:
                errors.append(f"{message.aggregate_id}:{type(exc).__name__}")
                if message.lease_owner is not None:
                    self.control_repository.retry(
                        message_id=message.message_id,
                        owner=message.lease_owner,
                        fencing_token=message.fencing_token,
                        error=str(exc),
                        delay_seconds=1,
                        max_attempts=1000,
                    )
                continue
            succeeded += 1
        return FormalResultDispatchBatch(
            checked=len(messages),
            succeeded=succeeded,
            errors=tuple(errors),
        )

    def _enqueue(self, turn: AgentTurnRecord) -> tuple[OutboxMessage, bool]:
        return self.control_repository.enqueue(
            message_id=f"agent-turn:{turn.turn_id}",
            topic=AGENT_TURN_TOPIC,
            aggregate_id=turn.turn_id,
            payload={"turn_id": turn.turn_id},
        )


def _envelope_payload(value: AgentResourceEnvelope) -> dict[str, object]:
    return {
        "partition": value.partition,
        "qos": value.qos,
        "cpus": value.cpus,
        "memory_mib": value.memory_mib,
        "gpu_type": value.gpu_type,
        "gpus": value.gpus,
        "walltime_seconds": value.walltime_seconds,
        "max_tasks": value.max_tasks,
        "max_submissions": value.max_submissions,
        "workspace_snapshot_digest": value.workspace_snapshot_digest,
        "expires_at": value.expires_at,
        "approved_by": value.approved_by,
    }


def _formal_approval_binding(contract: ContractRecord) -> dict[str, str]:
    matches = [
        item
        for item in contract.field_sources
        if item.get("source") == "agent_formal_approval" and item.get("field") == "formal_run"
    ]
    if len(matches) != 1:
        raise ValueError("formal Contract approval binding is missing")
    required = {
        "approval_digest",
        "approved_by",
        "session_id",
        "validation_run_id",
    }
    result: dict[str, str] = {}
    for key in required:
        value = matches[0].get(key)
        if not isinstance(value, str) or not value:
            raise ValueError("formal Contract approval binding is invalid")
        result[key] = value
    return result


def _required_handoff_string(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"formal result handoff {key} is invalid")
    return value


def _explainable_evidence(item: EvidenceObjectRecord) -> bool:
    mime_type = (item.mime_type or "").lower()
    return (
        item.collection_status == "collected"
        and bool(item.sha256)
        and (
            mime_type.startswith("text/")
            or mime_type
            in {
                "application/json",
                "application/xml",
                "application/yaml",
                "application/x-yaml",
            }
        )
    )


def _evidence_ref(run_id: str, item: EvidenceObjectRecord) -> str:
    if item.source_uri and item.source_uri.startswith(f"evidence://runs/{run_id}/"):
        return item.source_uri
    return f"evidence://runs/{run_id}/{item.logical_path}"
