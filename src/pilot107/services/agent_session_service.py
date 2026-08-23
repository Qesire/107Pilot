"""Application service for durable Agent Session and Turn submission."""

from __future__ import annotations

from collections.abc import Mapping

from pilot107.agent.session import AgentSessionRecord, AgentTurnRecord
from pilot107.agent.store import AgentSessionStore
from pilot107.agent.tasks import AgentResourceEnvelope
from pilot107.core.control_repository import ControlRepository, OutboxMessage

AGENT_TURN_TOPIC = "agent.turn.execute.v1"


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
        if profile_id not in {"hpc-readonly-v1", "platform_coach", "experiment_builder"}:
            raise ValueError("Agent profile is not supported")
        if profile_id == "experiment_builder" and (
            set(source) not in (
                {"project_id", "workspace_id"},
                {"project_id", "workspace_id", "resource_envelope"},
            )
            or any(
                not isinstance(source.get(key), str) or not source.get(key)
                for key in ("project_id", "workspace_id")
            )
        ):
            raise ValueError("experiment_builder requires Project and Workspace bindings")
        normalized_source = dict(source)
        envelope_value = normalized_source.get("resource_envelope")
        if profile_id != "experiment_builder" and envelope_value is not None:
            raise ValueError("resource envelope requires experiment_builder profile")
        if profile_id == "experiment_builder" and envelope_value is not None:
            if not isinstance(envelope_value, Mapping):
                raise ValueError("experiment_builder resource envelope is invalid")
            envelope = AgentResourceEnvelope(**dict(envelope_value))
            if envelope.approved_by != owner:
                raise ValueError("experiment_builder resource envelope owner is invalid")
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
