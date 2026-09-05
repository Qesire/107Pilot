"""Build platform snapshots from allowlisted read-only observations."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from pilot107.adapters.platform_cli import (
    PlatformCommand,
    PlatformObservationCollector,
    default_login_snapshot_specs,
)
from pilot107.adapters.platform_parsers import (
    parse_scontrol_show_nodes,
    parse_scontrol_show_part,
    parse_sinfo_pipe,
    parse_squeue_pipe,
)
from pilot107.core.platform_snapshot import (
    CommandObservation,
    NodeSnapshot,
    ObservationSourceType,
    ObservedAvailability,
    PartitionSnapshot,
    PlatformDefault,
    PlatformSnapshot,
    PlatformSnapshotScope,
    RuntimeLimitation,
    RuntimeLimitationName,
    SqueueJobSnapshot,
)
from pilot107.core.platform_snapshot_store import (
    PlatformSnapshotRecord,
    PlatformSnapshotStore,
)


class PlatformSnapshotService:
    def __init__(
        self,
        *,
        collector: PlatformObservationCollector | None = None,
        collector_version: str = "pilot107.platform_snapshot.v1",
    ) -> None:
        if collector is None:
            from pilot107.adapters.platform_cli import PlatformCliCollector

            collector = PlatformCliCollector()
        self.collector = collector
        self.collector_version = collector_version

    def collect_login_snapshot(
        self,
        *,
        username: str,
        home: str | None = None,
        captured_at: str | None = None,
        snapshot_id: str | None = None,
    ) -> PlatformSnapshot:
        timestamp = captured_at or datetime.now(UTC).isoformat()
        command_results = self.collector.collect(
            default_login_snapshot_specs(username=username, home=home)
        )
        redacted_results, redaction_report = redact_command_results(
            command_results,
            username=username,
            home=home,
        )
        return build_login_snapshot_from_observations(
            command_results=redacted_results,
            captured_at=timestamp,
            collector_version=self.collector_version,
            snapshot_id=snapshot_id
            or _snapshot_id(timestamp=timestamp, command_results=redacted_results),
            redaction_report=redaction_report,
        )

    def collect_and_store_login_snapshot(
        self,
        *,
        store: PlatformSnapshotStore,
        owner: str,
        username: str,
        source_type: ObservationSourceType,
        source_name: str,
        home: str | None = None,
        ttl_seconds: int = 300,
        captured_at: str | None = None,
        snapshot_id: str | None = None,
    ) -> PlatformSnapshotRecord:
        if ttl_seconds <= 0 or ttl_seconds > 7 * 24 * 60 * 60:
            raise ValueError("snapshot TTL must be between 1 second and 7 days")
        snapshot = self.collect_login_snapshot(
            username=username,
            home=home,
            captured_at=captured_at,
            snapshot_id=snapshot_id,
        )
        captured = datetime.fromisoformat(snapshot.captured_at)
        if captured.tzinfo is None:
            raise ValueError("snapshot captured_at must include a timezone")
        expires_at = (captured.astimezone(UTC) + timedelta(seconds=ttl_seconds)).isoformat()
        return store.create(
            owner=owner,
            snapshot=snapshot,
            source_type=source_type,
            source_name=source_name,
            expires_at=expires_at,
        )


def build_login_snapshot_from_observations(
    *,
    command_results: tuple[CommandObservation, ...],
    captured_at: str,
    collector_version: str = "pilot107.platform_snapshot.v1",
    snapshot_id: str | None = None,
    redaction_report: tuple[str, ...] = (),
    defaults: tuple[PlatformDefault, ...] = (),
    runtime_limitations: tuple[RuntimeLimitation, ...] = (),
) -> PlatformSnapshot:
    by_name = {item.name: item for item in command_results}
    partitions: tuple[PartitionSnapshot, ...] = ()
    nodes: tuple[NodeSnapshot, ...] = ()
    squeue_jobs: tuple[SqueueJobSnapshot, ...] = ()
    limitations: list[str] = []

    part_result = by_name.get(PlatformCommand.SCONTROL_SHOW_PART.value)
    if part_result is not None and part_result.returncode == 0:
        partitions = parse_scontrol_show_part(
            part_result.stdout,
            raw_artifact="raw/scontrol-show-part.txt",
            captured_at=captured_at,
        )
    elif part_result is not None:
        limitations.append("scontrol show part unavailable")

    node_result = by_name.get(PlatformCommand.SCONTROL_SHOW_NODES.value)
    if node_result is not None and node_result.returncode == 0:
        nodes = parse_scontrol_show_nodes(
            node_result.stdout,
            raw_artifact="raw/scontrol-show-nodes.txt",
            captured_at=captured_at,
        )
    else:
        sinfo_result = by_name.get(PlatformCommand.SINFO_PIPE.value)
        if sinfo_result is not None and sinfo_result.returncode == 0:
            nodes = parse_sinfo_pipe(sinfo_result.stdout, captured_at=captured_at)
        if node_result is not None and node_result.returncode != 0:
            limitations.append("scontrol show nodes unavailable")

    squeue_result = by_name.get(PlatformCommand.SQUEUE_USER_PIPE.value)
    if squeue_result is not None and squeue_result.returncode == 0:
        squeue_jobs = parse_squeue_pipe(
            squeue_result.stdout,
            raw_artifact="raw/squeue.txt",
            captured_at=captured_at,
        )
    elif squeue_result is not None:
        limitations.append("squeue unavailable")

    for result in command_results:
        if result.returncode != 0:
            limitations.append(f"{result.name} returned {result.returncode}")
        if result.timed_out:
            limitations.append(f"{result.name} timed out")
        if result.truncated:
            limitations.append(f"{result.name} output truncated")

    structured_runtime_limitations = runtime_limitations or (
        RuntimeLimitation(
            name=RuntimeLimitationName.GPU_RUNTIME,
            availability=ObservedAvailability.UNSUPPORTED,
            source_type=ObservationSourceType.CLI,
            source_name="login-node platform snapshot",
            captured_at=captured_at,
            warning=(
                "Login-node GPU runtime is not evidence that GPU partitions are unavailable; "
                "GPU runtime must be checked inside an allocated GPU job."
            ),
        ),
    )

    return PlatformSnapshot(
        snapshot_id=snapshot_id
        or _snapshot_id(timestamp=captured_at, command_results=command_results),
        scope=PlatformSnapshotScope.LOGIN_NODE,
        captured_at=captured_at,
        collector_version=collector_version,
        command_results=command_results,
        partitions=partitions,
        nodes=nodes,
        squeue_jobs=squeue_jobs,
        defaults=defaults,
        runtime_limitations=structured_runtime_limitations,
        limitations=tuple(dict.fromkeys(limitations)),
        redaction_report=redaction_report,
    )


def redact_command_results(
    command_results: tuple[CommandObservation, ...],
    *,
    username: str,
    home: str | None,
) -> tuple[tuple[CommandObservation, ...], tuple[str, ...]]:
    redacted: list[CommandObservation] = []
    reports: list[str] = []
    for result in command_results:
        argv: list[str] = []
        argv_changed = False
        for argument in result.argv:
            redacted_argument, changed = _redact_text(
                argument,
                username=username,
                home=home,
            )
            argv.append(redacted_argument)
            argv_changed = argv_changed or changed
        stdout, stdout_changed = _redact_text(result.stdout, username=username, home=home)
        stderr, stderr_changed = _redact_text(result.stderr, username=username, home=home)
        if argv_changed or stdout_changed or stderr_changed:
            reports.append(f"{result.name}: username/home redacted")
        redacted.append(replace(result, argv=tuple(argv), stdout=stdout, stderr=stderr))
    return tuple(redacted), tuple(reports)


def _redact_text(
    value: str,
    *,
    username: str,
    home: str | None,
) -> tuple[str, bool]:
    changed = False
    redacted = value
    if home and home in redacted:
        redacted = redacted.replace(home, "<home>")
        changed = True
    if username and username in redacted:
        redacted = redacted.replace(username, "<user>")
        changed = True
    return redacted, changed


def _snapshot_id(
    *,
    timestamp: str,
    command_results: tuple[CommandObservation, ...],
) -> str:
    digest = hashlib.sha256()
    digest.update(timestamp.encode("utf-8"))
    for result in command_results:
        digest.update(result.name.encode("utf-8"))
        digest.update(result.stdout.encode("utf-8"))
        digest.update(result.stderr.encode("utf-8"))
    return f"platform-{digest.hexdigest()[:16]}"
