"""Persistence-only read service for resource observation facts."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from pilot107.observability.evaluator import ResourceEvaluation, ResourceEvaluator
from pilot107.observability.model import (
    AccountPulse,
    PlatformPulse,
    ResourceMeasureSet,
    RunResourceSample,
    RunResourceSummary,
)
from pilot107.observability.store import ObservabilityStore


class ObservabilityService:
    def __init__(
        self,
        *,
        store: ObservabilityStore,
        evaluator: ResourceEvaluator | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store
        self.evaluator = evaluator or ResourceEvaluator()
        self._clock = clock or (lambda: datetime.now(UTC))

    def latest_capability(self, connection_id: str) -> dict[str, object]:
        pulse = self.store.get_latest_platform_pulse(connection_id, lane="capability")
        return _pulse_payload(
            pulse,
            kind="cluster_capability",
            freshness=_freshness(pulse.captured_at, now=self._now(), fresh=600, stale=1800),
        )

    def latest_platform(self, connection_id: str) -> dict[str, object]:
        pulse = self.store.get_latest_platform_pulse(
            connection_id, lane="platform_account"
        )
        return _pulse_payload(
            pulse,
            kind="platform_pulse",
            freshness=_freshness(pulse.captured_at, now=self._now(), fresh=45, stale=300),
        )

    def latest_account(self, connection_id: str, *, owner: str) -> dict[str, object]:
        pulse = self.store.get_latest_account_pulse(connection_id, owner=owner)
        payload = _pulse_payload(
            pulse,
            kind="account_pulse",
            freshness=_freshness(pulse.captured_at, now=self._now(), fresh=45, stale=300),
        )
        trend = self.evaluator.evaluate_queue_trend(
            tuple(self.store.list_account_pulses(connection_id, owner=owner, limit=3))
        )
        payload["evaluations"] = [] if trend is None else [_evaluation_payload(trend)]
        return payload

    def run_resources(self, run_id: str, *, owner: str) -> dict[str, object]:
        try:
            summary = self.store.get_summary(run_id, owner=owner)
        except KeyError:
            samples = self.store.list_run_samples(run_id, owner=owner)
            if not samples:
                raise
            latest = samples[-1]
            return _sample_payload(
                latest,
                freshness=_freshness(
                    latest.captured_at, now=self._now(), fresh=75, stale=300
                ),
            )
        payload = _summary_payload(summary)
        payload["evaluations"] = [
            _evaluation_payload(item) for item in self.evaluator.evaluate(summary)
        ]
        return payload

    def run_series(
        self,
        run_id: str,
        *,
        owner: str,
        step: str,
        limit: int,
    ) -> dict[str, object]:
        values = (
            self.store.list_run_samples(run_id, owner=owner)
            if step == "raw"
            else self.store.list_minute_aggregates(run_id, owner=owner)
        )
        if not values:
            raise KeyError(run_id)
        selected = values[-limit:]
        return {
            "run_id": run_id,
            "step": step,
            "items": [
                _sample_payload(
                    item,
                    freshness=_freshness(
                        item.captured_at, now=self._now(), fresh=75, stale=300
                    ),
                )
                for item in selected
            ],
            "limit": limit,
            "truncated": len(values) > len(selected),
            "next_cursor": None,
        }

    def run_evaluations(self, run_id: str, *, owner: str) -> dict[str, object]:
        summary = self.store.get_summary(run_id, owner=owner)
        return {
            "run_id": run_id,
            "summary_id": summary.observation_id,
            "items": [
                _evaluation_payload(item) for item in self.evaluator.evaluate(summary)
            ],
        }

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            raise ValueError("observability clock must be timezone-aware")
        return value.astimezone(UTC)


def _pulse_payload(
    value: PlatformPulse | AccountPulse, *, kind: str, freshness: str
) -> dict[str, object]:
    return {
        "observation_id": value.observation_id,
        "kind": kind,
        "connection_id": value.connection_id,
        "owner": value.owner,
        "cycle_id": value.cycle_id,
        "captured_at": value.captured_at,
        "freshness": freshness,
        "partial": value.partial,
        "warnings": list(value.warnings),
        "measures": _measure_payload(value.measures),
    }


def _sample_payload(value: RunResourceSample, *, freshness: str) -> dict[str, object]:
    return {
        "observation_id": value.observation_id,
        "kind": "run_resource_sample",
        "connection_id": value.connection_id,
        "owner": value.owner,
        "run_id": value.run_id,
        "attempt": value.attempt,
        "cycle_id": value.cycle_id,
        "captured_at": value.captured_at,
        "freshness": freshness,
        "partial": value.partial,
        "warnings": list(value.warnings),
        "measures": _measure_payload(value.measures),
        "evaluations": [],
    }


def _summary_payload(value: RunResourceSummary) -> dict[str, object]:
    return {
        "observation_id": value.observation_id,
        "kind": "run_resource_summary",
        "connection_id": value.connection_id,
        "owner": value.owner,
        "run_id": value.run_id,
        "attempt": value.attempt,
        "cycle_id": value.cycle_id,
        "captured_at": value.captured_at,
        "freshness": "terminal",
        "partial": value.partial,
        "warnings": list(value.warnings),
        "used": _measure_payload(value.used),
        "allocated": _measure_payload(value.allocated),
    }


def _measure_payload(value: ResourceMeasureSet) -> dict[str, object]:
    return {name: measure.__dict__ for name, measure in value.as_dict().items()}


def _evaluation_payload(value: ResourceEvaluation) -> dict[str, object]:
    return {
        "evaluation_id": value.evaluation_id,
        "run_id": value.run_id,
        "summary_id": value.summary_id,
        "rule_id": value.rule_id,
        "rule_version": value.rule_version,
        "severity": value.severity,
        "confidence": value.confidence,
        "summary": value.summary,
        "measured_values": value.measured_values,
        "thresholds": value.thresholds,
        "evidence_refs": list(value.evidence_refs),
        "suggested_contract_patch": value.suggested_contract_patch,
    }


def _freshness(
    captured_at: str, *, now: datetime, fresh: int, stale: int
) -> str:
    captured = datetime.fromisoformat(captured_at.replace("Z", "+00:00")).astimezone(UTC)
    age = max(0.0, (now - captured).total_seconds())
    if age <= fresh:
        return "fresh"
    if age <= stale:
        return "stale"
    return "expired"
