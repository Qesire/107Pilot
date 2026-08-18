"""Durable outbox worker for A1 read-only Agent Turns."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any, Protocol

from pilot107.agent.capabilities import AgentCapabilityClaims, AgentCapabilitySigner
from pilot107.agent.protocol import AgentdClientError, AgentTurnEvent, DurableAgentTurnRequest
from pilot107.agent.session import AgentTurnLease, AgentTurnState
from pilot107.agent.store import AgentSessionStore
from pilot107.core.control_repository import ControlRepository, OutboxMessage
from pilot107.services.agent_session_service import AGENT_TURN_TOPIC

_A1_READ_TOOLS = frozenset(
    {
        "platform_get_snapshot",
        "workspace_list",
        "workspace_search",
        "workspace_read",
        "run_get",
        "run_log_read",
        "evidence_read",
    }
)
_TERMINAL_STATES = frozenset(
    {AgentTurnState.COMPLETED, AgentTurnState.CANCELLED, AgentTurnState.FAILED}
)


class AgentTurnClient(Protocol):
    def stream_durable_turn(
        self,
        request: DurableAgentTurnRequest,
        on_event: Callable[[AgentTurnEvent], None] | None = None,
    ) -> Iterator[AgentTurnEvent]: ...

    def cancel_turn(self, turn_id: str) -> str: ...


@dataclass(frozen=True)
class AgentTurnDispatchError:
    turn_id: str
    message: str
    code: str = "AGENT.TURN_DISPATCH_ERROR"
    retryable: bool = True


@dataclass(frozen=True)
class AgentTurnDispatchResult:
    checked: int
    succeeded: int
    errors: list[AgentTurnDispatchError] = field(default_factory=list)


class AgentTurnWorker:
    """Claims outbox and Turn leases, streams events, and commits them durably."""

    def __init__(
        self,
        *,
        store: AgentSessionStore,
        control_repository: ControlRepository,
        agentd_client: AgentTurnClient,
        capability_signer: AgentCapabilitySigner,
        worker_id: str,
        lease_seconds: int = 120,
        max_attempts: int = 5,
        clock: Callable[[], int] | None = None,
        publish_event_hint: Callable[[str, int], None] | None = None,
    ) -> None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        if max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        self.store = store
        self.control_repository = control_repository
        self.agentd_client = agentd_client
        self.capability_signer = capability_signer
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        self.max_attempts = max_attempts
        self._clock = clock or (lambda: int(time.time()))
        self._publish_event_hint = publish_event_hint or (lambda _session, _sequence: None)

    def dispatch_due(self, *, limit: int) -> AgentTurnDispatchResult:
        if limit <= 0:
            raise ValueError("limit must be positive")
        checked = 0
        succeeded = 0
        errors: list[AgentTurnDispatchError] = []
        while checked < limit:
            messages = self.control_repository.claim_outbox(
                owner=self.worker_id,
                limit=1,
                lease_seconds=self.lease_seconds,
                topics=(AGENT_TURN_TOPIC,),
            )
            if not messages:
                break
            checked += 1
            message = messages[0]
            try:
                if self._dispatch(message):
                    succeeded += 1
            except Exception:
                errors.append(
                    AgentTurnDispatchError(
                        turn_id=message.aggregate_id,
                        message="Agent Turn dispatch failed",
                    )
                )
        return AgentTurnDispatchResult(checked=checked, succeeded=succeeded, errors=errors)

    def _dispatch(self, message: OutboxMessage) -> bool:
        turn_id = self._turn_id(message)
        claim = self.store.claim_turn(
            turn_id,
            worker_id=self.worker_id,
            lease_seconds=self.lease_seconds,
        )
        if claim is None:
            current = self.store.get_turn_for_dispatch(turn_id)
            if current.state in _TERMINAL_STATES:
                self._acknowledge(message)
                return True
            self._retry(message, "Agent Turn is not currently claimable", delay_seconds=1)
            return False

        current = self.store.get_turn(turn_id, owner=claim.owner)
        if current.cancel_requested:
            self._cancel_and_complete(
                message,
                claim,
                current.final_checkpoint,
                next_sequence=current.event_sequence + 1,
            )
            return True

        session = self.store.get_session(claim.session_id, owner=claim.owner)
        request = DurableAgentTurnRequest(
            session_id=claim.session_id,
            turn_id=turn_id,
            owner=claim.owner,
            state_version=claim.state_version,
            model_profile_id=session.model_profile_id,
            message=current.message,
            context_refs=_context_refs(session.source),
            capability_token=self._capability(claim, session.profile_id),
            checkpoint=current.final_checkpoint,
        )
        checkpoint = current.final_checkpoint
        terminal: AgentTurnEvent | None = None
        cancel_sent = False
        try:
            for event in self.agentd_client.stream_durable_turn(request):
                self.store.append_event(
                    turn_id,
                    claim=claim,
                    sequence=event.sequence,
                    event_type=event.type,
                    payload=event.payload,
                )
                if event.type == "checkpoint":
                    checkpoint = _object_or_none(event.payload.get("checkpoint"))
                elif event.type == "turn_completed":
                    checkpoint = _object_or_none(event.payload.get("checkpoint")) or checkpoint
                self._publish_hint(claim.session_id, event.sequence)
                current = self.store.get_turn(turn_id, owner=claim.owner)
                if current.cancel_requested and not cancel_sent:
                    self.agentd_client.cancel_turn(turn_id)
                    cancel_sent = True
                if event.type in {"turn_completed", "turn_failed"}:
                    terminal = event
        except AgentdClientError as exc:
            checkpoint = exc.checkpoint or checkpoint
            self._interrupt_and_retry(message, claim, checkpoint, exc.code)
            raise
        except Exception:
            self._interrupt_and_retry(message, claim, checkpoint, "internal_error")
            raise

        if terminal is None:
            self._interrupt_and_retry(message, claim, checkpoint, "stream_ended")
            raise AgentdClientError(
                "pilot-agentd stream ended without a terminal event",
                code="protocol_error",
                retryable=True,
                checkpoint=checkpoint,
            )
        self._finish_terminal(message, claim, terminal, checkpoint)
        return True

    def _finish_terminal(
        self,
        message: OutboxMessage,
        claim: AgentTurnLease,
        terminal: AgentTurnEvent,
        checkpoint: Mapping[str, object] | None,
    ) -> None:
        if terminal.type == "turn_completed":
            usage = _object_or_empty(terminal.payload.get("usage"))
            outcome: dict[str, object] = {
                "status": "completed",
                "result": terminal.payload.get("result"),
            }
        else:
            error = _object_or_empty(terminal.payload.get("error"))
            error_code = error.get("code")
            cancelled = error_code == "aborted" or self.store.get_turn(
                claim.turn_id, owner=claim.owner
            ).cancel_requested
            usage = {}
            outcome = {
                "status": "aborted" if cancelled else "failed",
                "error": error,
            }
        self.store.complete_turn(
            claim.turn_id,
            claim=claim,
            final_checkpoint=checkpoint,
            resource_usage=usage,
            outcome=outcome,
        )
        self._acknowledge(message)

    def _cancel_and_complete(
        self,
        message: OutboxMessage,
        claim: AgentTurnLease,
        checkpoint: Mapping[str, object] | None,
        *,
        next_sequence: int,
    ) -> None:
        self.agentd_client.cancel_turn(claim.turn_id)
        event = self.store.append_event(
            claim.turn_id,
            claim=claim,
            sequence=next_sequence,
            event_type="turn_failed",
            payload={
                "error": {
                    "code": "aborted",
                    "message": "The Turn was aborted.",
                    "retryable": False,
                }
            },
        )
        self._publish_hint(claim.session_id, event.sequence)
        self.store.complete_turn(
            claim.turn_id,
            claim=claim,
            final_checkpoint=checkpoint,
            resource_usage={},
            outcome={"status": "aborted", "error": {"code": "aborted"}},
        )
        self._acknowledge(message)

    def _interrupt_and_retry(
        self,
        message: OutboxMessage,
        claim: AgentTurnLease,
        checkpoint: Mapping[str, object] | None,
        code: str,
    ) -> None:
        self.store.interrupt_turn(
            claim.turn_id,
            claim=claim,
            checkpoint=checkpoint,
            error={"code": code, "message": "Agent Turn was interrupted", "retryable": True},
        )
        self._retry(message, "Agent Turn was interrupted", delay_seconds=0)

    def _capability(self, claim: AgentTurnLease, profile_id: str) -> str:
        now = int(self._clock())
        return self.capability_signer.sign(
            AgentCapabilityClaims(
                owner=claim.owner,
                session_id=claim.session_id,
                turn_id=claim.turn_id,
                state_version=claim.state_version,
                fencing_token=claim.fencing_token,
                profile_id=profile_id,
                tools=_A1_READ_TOOLS,
                max_invocations=32,
                max_bytes=1024 * 1024,
                expires_at=now + min(self.lease_seconds, 120),
            )
        )

    def _publish_hint(self, session_id: str, sequence: int) -> None:
        with suppress(Exception):
            self._publish_event_hint(session_id, sequence)

    def _acknowledge(self, message: OutboxMessage) -> None:
        self.control_repository.acknowledge(
            message_id=message.message_id,
            owner=self.worker_id,
            fencing_token=message.fencing_token,
        )

    def _retry(self, message: OutboxMessage, error: str, *, delay_seconds: int) -> None:
        self.control_repository.retry(
            message_id=message.message_id,
            owner=self.worker_id,
            fencing_token=message.fencing_token,
            error=error,
            delay_seconds=delay_seconds,
            max_attempts=self.max_attempts,
        )

    @staticmethod
    def _turn_id(message: OutboxMessage) -> str:
        value = message.payload.get("turn_id")
        if (
            message.topic != AGENT_TURN_TOPIC
            or not isinstance(value, str)
            or value != message.aggregate_id
        ):
            raise ValueError("Agent Turn outbox message is invalid")
        return value


def _context_refs(source: Mapping[str, Any]) -> tuple[str, ...]:
    refs: list[str] = []
    for key in ("run_id", "workspace_id", "evidence_id"):
        value = source.get(key)
        if isinstance(value, str) and value:
            refs.append(f"{key.removesuffix('_id')}:{value}")
    return tuple(refs)


def _object_or_none(value: object) -> dict[str, object] | None:
    return dict(value) if isinstance(value, Mapping) else None


def _object_or_empty(value: object) -> dict[str, object]:
    return _object_or_none(value) or {}
