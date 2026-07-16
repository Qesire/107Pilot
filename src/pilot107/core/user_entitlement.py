"""Owner-scoped Slurm association and QoS entitlement observations."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from pilot107.core.platform_snapshot import CommandObservation


class EntitlementDataQuality(StrEnum):
    AUTHORITATIVE = "authoritative"
    PARTIAL = "partial"
    PERMISSION_DENIED = "permission_denied"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class UserAssociation:
    account: str
    partition: str | None
    qos: tuple[str, ...]
    default_qos: str | None

    def to_payload(self) -> dict[str, object]:
        return {
            "account": self.account,
            "partition": self.partition,
            "qos": list(self.qos),
            "default_qos": self.default_qos,
        }


@dataclass(frozen=True)
class UserEntitlementSnapshot:
    snapshot_id: str
    captured_at: str
    collector_version: str
    data_quality: EntitlementDataQuality
    default_account: str | None = None
    associations: tuple[UserAssociation, ...] = ()
    command_results: tuple[CommandObservation, ...] = ()
    limitations: tuple[str, ...] = ()
    redaction_report: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, object]:
        return {
            "snapshot_id": self.snapshot_id,
            "captured_at": self.captured_at,
            "collector_version": self.collector_version,
            "data_quality": self.data_quality.value,
            "default_account": self.default_account,
            "associations": [item.to_payload() for item in self.associations],
            "command_results": [item.to_payload() for item in self.command_results],
            "limitations": list(self.limitations),
            "redaction_report": list(self.redaction_report),
        }
