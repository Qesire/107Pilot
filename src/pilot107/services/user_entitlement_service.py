"""Collect and normalize user-scoped Slurm association entitlements."""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime, timedelta

from pilot107.adapters.platform_cli import (
    PlatformObservationCollector,
    user_entitlement_snapshot_specs,
)
from pilot107.core.platform_snapshot import ObservationSourceType
from pilot107.core.user_entitlement import (
    EntitlementDataQuality,
    UserAssociation,
    UserEntitlementSnapshot,
)
from pilot107.core.user_entitlement_store import (
    UserEntitlementRecord,
    UserEntitlementStore,
)
from pilot107.services.platform_snapshot_service import redact_command_results

_VALUE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


class UserEntitlementService:
    def __init__(
        self,
        *,
        collector: PlatformObservationCollector,
        collector_version: str = "pilot107.user_entitlement.v1",
    ) -> None:
        self.collector = collector
        self.collector_version = collector_version

    def collect(
        self,
        *,
        username: str,
        captured_at: str | None = None,
        snapshot_id: str | None = None,
    ) -> UserEntitlementSnapshot:
        timestamp = captured_at or datetime.now(UTC).isoformat()
        results = self.collector.collect(user_entitlement_snapshot_specs(username))
        result = results[0]
        quality, default_account, associations, limitations = _parse_result(
            result.returncode,
            result.stdout,
            result.stderr,
            expected_username=username,
        )
        redacted, report = redact_command_results(
            results,
            username=username,
            home=None,
        )
        digest = hashlib.sha256()
        digest.update(timestamp.encode("utf-8"))
        digest.update(redacted[0].stdout.encode("utf-8"))
        digest.update(redacted[0].stderr.encode("utf-8"))
        return UserEntitlementSnapshot(
            snapshot_id=snapshot_id or f"entitlement-{digest.hexdigest()[:16]}",
            captured_at=timestamp,
            collector_version=self.collector_version,
            data_quality=quality,
            default_account=default_account,
            associations=associations,
            command_results=redacted,
            limitations=limitations,
            redaction_report=report,
        )

    def collect_and_store(
        self,
        *,
        store: UserEntitlementStore,
        owner: str,
        username: str,
        source_type: ObservationSourceType,
        source_name: str,
        ttl_seconds: int = 300,
        captured_at: str | None = None,
        snapshot_id: str | None = None,
    ) -> UserEntitlementRecord:
        if ttl_seconds <= 0 or ttl_seconds > 7 * 24 * 60 * 60:
            raise ValueError("snapshot TTL must be between 1 second and 7 days")
        if owner != username:
            raise ValueError("entitlement owner and queried username must match")
        snapshot = self.collect(
            username=username,
            captured_at=captured_at,
            snapshot_id=snapshot_id,
        )
        captured = datetime.fromisoformat(snapshot.captured_at)
        if captured.tzinfo is None:
            raise ValueError("snapshot captured_at must include a timezone")
        return store.create(
            owner=owner,
            snapshot=snapshot,
            source_type=source_type,
            source_name=source_name,
            expires_at=(captured.astimezone(UTC) + timedelta(seconds=ttl_seconds)).isoformat(),
        )


def _parse_result(
    returncode: int,
    stdout: str,
    stderr: str,
    *,
    expected_username: str,
) -> tuple[
    EntitlementDataQuality,
    str | None,
    tuple[UserAssociation, ...],
    tuple[str, ...],
]:
    if returncode != 0:
        denied = any(
            marker in stderr.lower()
            for marker in ("permission denied", "access denied", "not authorized")
        )
        return (
            EntitlementDataQuality.PERMISSION_DENIED
            if denied
            else EntitlementDataQuality.UNAVAILABLE,
            None,
            (),
            ("user association query unavailable",),
        )
    associations: list[UserAssociation] = []
    observed_default_account: str | None = None
    for line in stdout.splitlines():
        if not line.strip():
            continue
        fields = line.split("|")
        if len(fields) != 6:
            return (
                EntitlementDataQuality.PARTIAL,
                observed_default_account,
                tuple(associations),
                ("user association output contained an invalid row",),
            )
        user, default_account, account, partition, qos_text, default_qos = (
            item.strip() for item in fields
        )
        if user != expected_username:
            return (
                EntitlementDataQuality.PARTIAL,
                observed_default_account,
                tuple(associations),
                ("user association output contained a mismatched user",),
            )
        if not _valid_value(default_account):
            return (
                EntitlementDataQuality.PARTIAL,
                observed_default_account,
                tuple(associations),
                ("user default account was unavailable or invalid",),
            )
        if observed_default_account not in {None, default_account}:
            return (
                EntitlementDataQuality.PARTIAL,
                observed_default_account,
                tuple(associations),
                ("user association output contained inconsistent default accounts",),
            )
        observed_default_account = default_account
        if not _valid_value(account):
            return (
                EntitlementDataQuality.PARTIAL,
                observed_default_account,
                tuple(associations),
                ("user association account was invalid",),
            )
        qos = tuple(item for item in qos_text.split(",") if item)
        if any(not _valid_value(item) for item in qos):
            return (
                EntitlementDataQuality.PARTIAL,
                observed_default_account,
                tuple(associations),
                ("user association QoS value was invalid",),
            )
        if partition and not _valid_value(partition):
            return (
                EntitlementDataQuality.PARTIAL,
                observed_default_account,
                tuple(associations),
                ("user association partition was invalid",),
            )
        if default_qos and not _valid_value(default_qos):
            return (
                EntitlementDataQuality.PARTIAL,
                observed_default_account,
                tuple(associations),
                ("user association default QoS was invalid",),
            )
        associations.append(
            UserAssociation(
                account=account,
                partition=partition or None,
                qos=qos,
                default_qos=default_qos or None,
            )
        )
    limitations = () if associations else ("no user associations were observed",)
    return (
        EntitlementDataQuality.AUTHORITATIVE,
        observed_default_account,
        tuple(associations),
        limitations,
    )


def _valid_value(value: str) -> bool:
    return _VALUE.fullmatch(value) is not None
