"""Freshness-aware resource findings derived from platform snapshots."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pilot107.core.platform_snapshot_store import PlatformSnapshotRecord, SnapshotFreshness
from pilot107.core.resources import PreflightFinding, PreflightSeverity, ResourcePlan
from pilot107.core.user_entitlement import EntitlementDataQuality
from pilot107.core.user_entitlement_store import UserEntitlementRecord


def validate_platform_snapshot_resource_plan(
    plan: ResourcePlan,
    snapshot: PlatformSnapshotRecord | None,
    *,
    at: datetime | None = None,
) -> list[PreflightFinding]:
    if snapshot is None:
        return [
            PreflightFinding(
                severity=PreflightSeverity.UNKNOWN,
                code="PLATFORM.SNAPSHOT_UNAVAILABLE",
                message="no owner-scoped platform snapshot is available",
                source_authority="platform_snapshot:none",
            )
        ]
    authority = f"platform_snapshot:{snapshot.snapshot_id}"
    freshness = snapshot.freshness(at=at)
    if freshness != SnapshotFreshness.FRESH:
        return [
            PreflightFinding(
                severity=PreflightSeverity.UNKNOWN,
                code=(
                    "PLATFORM.SNAPSHOT_STALE"
                    if freshness == SnapshotFreshness.STALE
                    else "PLATFORM.SNAPSHOT_FRESHNESS_UNKNOWN"
                ),
                message=(
                    f"platform snapshot {snapshot.snapshot_id} is {freshness.value}; "
                    "its dynamic facts were not used to authorize this resource request"
                ),
                source_authority=authority,
            )
        ]

    partitions = snapshot.payload.get("partitions")
    if not isinstance(partitions, list):
        return [_invalid_partitions_finding(authority)]
    observed = next(
        (
            item
            for item in partitions
            if isinstance(item, dict) and item.get("name") == plan.partition
        ),
        None,
    )
    if observed is None:
        return [
            PreflightFinding(
                severity=PreflightSeverity.WARN,
                code="PLATFORM.PARTITION_NOT_OBSERVED",
                message=(
                    f"partition {plan.partition} was not present in fresh snapshot "
                    f"{snapshot.snapshot_id}; static policy remains authoritative"
                ),
                source_authority=authority,
            )
        ]

    findings: list[PreflightFinding] = []
    state = observed.get("state_raw")
    if isinstance(state, str) and state.upper() != "UP":
        findings.append(
            PreflightFinding(
                severity=PreflightSeverity.WARN,
                code="PLATFORM.PARTITION_NOT_UP",
                message=f"partition {plan.partition} was observed in state {state}",
                source_authority=authority,
            )
        )
    findings.extend(_observed_qos_findings(plan, observed, authority))
    return findings


def validate_user_entitlement_resource_plan(
    plan: ResourcePlan,
    entitlement: UserEntitlementRecord | None,
    *,
    at: datetime | None = None,
) -> list[PreflightFinding]:
    if entitlement is None:
        return [
            PreflightFinding(
                severity=PreflightSeverity.UNKNOWN,
                code="ENTITLEMENT.SNAPSHOT_UNAVAILABLE",
                message="no owner-scoped user entitlement snapshot is available",
                source_authority="user_entitlement:none",
            )
        ]
    authority = f"user_entitlement:{entitlement.snapshot_id}"
    freshness = entitlement.freshness(at=at)
    if freshness != SnapshotFreshness.FRESH:
        return [
            PreflightFinding(
                severity=PreflightSeverity.UNKNOWN,
                code=(
                    "ENTITLEMENT.SNAPSHOT_STALE"
                    if freshness == SnapshotFreshness.STALE
                    else "ENTITLEMENT.SNAPSHOT_FRESHNESS_UNKNOWN"
                ),
                message=(
                    f"user entitlement snapshot {entitlement.snapshot_id} is "
                    f"{freshness.value}; it was not used to authorize this request"
                ),
                source_authority=authority,
            )
        ]
    if entitlement.data_quality != EntitlementDataQuality.AUTHORITATIVE:
        return [
            PreflightFinding(
                severity=PreflightSeverity.UNKNOWN,
                code="ENTITLEMENT.DATA_QUALITY_INSUFFICIENT",
                message=(
                    f"user entitlement data quality is {entitlement.data_quality.value}; "
                    "the request cannot be authorized from this observation"
                ),
                source_authority=authority,
            )
        ]
    raw_associations = entitlement.payload.get("associations")
    if not isinstance(raw_associations, list):
        return [
            PreflightFinding(
                severity=PreflightSeverity.UNKNOWN,
                code="ENTITLEMENT.ASSOCIATIONS_INVALID",
                message="user entitlement associations are missing or invalid",
                source_authority=authority,
            )
        ]
    default_account = entitlement.payload.get("default_account")
    if not isinstance(default_account, str) or not default_account:
        return [
            PreflightFinding(
                severity=PreflightSeverity.UNKNOWN,
                code="ENTITLEMENT.DEFAULT_ACCOUNT_UNAVAILABLE",
                message=(
                    "the user's default Slurm account is unavailable; associations "
                    "cannot authorize a submission without an explicit account"
                ),
                source_authority=authority,
            )
        ]
    candidates = [
        item
        for item in raw_associations
        if (
            isinstance(item, dict)
            and item.get("account") == default_account
            and item.get("partition") in {None, plan.partition}
        )
    ]
    if not candidates:
        return [
            PreflightFinding(
                severity=PreflightSeverity.BLOCK,
                code="ENTITLEMENT.PARTITION_NOT_ALLOWED",
                message=(
                    f"fresh authoritative associations for default account {default_account} "
                    f"do not allow partition {plan.partition}"
                ),
                source_authority=authority,
            )
        ]
    allowed_qos = {
        qos
        for association in candidates
        for qos in _association_qos(association)
    }
    if plan.qos is not None and plan.qos not in allowed_qos:
        return [
            PreflightFinding(
                severity=PreflightSeverity.BLOCK,
                code="ENTITLEMENT.QOS_NOT_ALLOWED",
                message=(
                    f"QoS {plan.qos} is not present in the owner's fresh authoritative "
                    f"Slurm associations for default account {default_account}"
                ),
                source_authority=authority,
            )
        ]
    return [
        PreflightFinding(
            severity=PreflightSeverity.INFO,
            code="ENTITLEMENT.QOS_CONFIRMED",
            message=(
                f"partition {plan.partition} and QoS {plan.qos} are present in a fresh "
                f"authoritative Slurm association for default account {default_account}"
            ),
            source_authority=authority,
        )
    ]


def _observed_qos_findings(
    plan: ResourcePlan,
    observed: dict[str, Any],
    authority: str,
) -> list[PreflightFinding]:
    allowed = observed.get("allow_qos")
    if not isinstance(allowed, list) or not allowed or "ALL" in allowed or plan.qos is None:
        return []
    if plan.qos in allowed:
        return []
    return [
        PreflightFinding(
            severity=PreflightSeverity.WARN,
            code="PLATFORM.QOS_NOT_OBSERVED",
            message=(
                f"QoS {plan.qos} was not in the partition AllowQos observation; "
                "user entitlement is not yet proven, so this is not a hard authorization decision"
            ),
            source_authority=authority,
        )
    ]


def _invalid_partitions_finding(authority: str) -> PreflightFinding:
    return PreflightFinding(
        severity=PreflightSeverity.UNKNOWN,
        code="PLATFORM.PARTITION_FACTS_INVALID",
        message="platform snapshot partition facts are missing or invalid",
        source_authority=authority,
    )


def _association_qos(association: dict[str, Any]) -> tuple[str, ...]:
    value = association.get("qos")
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str))
