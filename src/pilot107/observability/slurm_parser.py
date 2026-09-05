"""Strict parsers for bounded, pipe-delimited Slurm accounting output."""

from __future__ import annotations

import re
from dataclasses import dataclass

_JOB_ID = re.compile(r"^[A-Za-z0-9_.:+-]{1,128}$")
_SECONDS = re.compile(r"^[0-9]+(?:\.[0-9]+)?$")
_MEMORY = re.compile(r"^(?P<value>[0-9]+(?:\.[0-9]+)?)(?P<unit>[KMGTPE]?)(?:i?B)?$", re.I)


@dataclass(frozen=True)
class ParsedRows[T]:
    records: tuple[T, ...]
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class SstatStepRecord:
    job_id: str
    step_id: str
    tasks: int | None
    allocated_tres: tuple[tuple[str, str], ...]
    average_cpu_seconds: float | None
    max_rss_bytes: int | None
    tres_usage_in_total: str
    tres_usage_out_total: str


@dataclass(frozen=True)
class SacctRecord:
    job_id_raw: str
    state: str
    exit_code: str
    elapsed_seconds: int | None
    time_limit_minutes: int | None
    allocated_tres: tuple[tuple[str, str], ...]
    allocated_cpus: int | None
    task_count: int | None
    total_cpu_seconds: float | None
    cpu_time_raw_seconds: int | None
    max_rss_bytes: int | None


@dataclass(frozen=True)
class SacctJobUsage:
    job_id: str
    state: str
    exit_code: str
    elapsed_seconds: int | None
    requested_walltime_seconds: int | None
    allocated_cpus: int | None
    task_count: int | None
    allocated_memory_bytes: int | None
    total_cpu_seconds: float | None
    cpu_time_raw_seconds: int | None
    max_rss_bytes: int | None


def parse_slurm_duration_seconds(value: str) -> float | None:
    """Parse Slurm's ``[days-][hours:]minutes:seconds[.fraction]`` values."""

    if not value:
        return None
    days = 0
    body = value.strip()
    if "-" in body:
        day_text, separator, body = body.partition("-")
        if not separator or not day_text.isdigit():
            return None
        days = int(day_text)

    parts = body.split(":")
    if not 1 <= len(parts) <= 3 or (days and len(parts) != 3):
        return None
    if not _SECONDS.fullmatch(parts[-1]):
        return None
    seconds = float(parts[-1])
    if len(parts) > 1 and seconds >= 60:
        return None

    if len(parts) == 3:
        if not parts[0].isdigit() or not parts[1].isdigit():
            return None
        hours = int(parts[0])
        minutes = int(parts[1])
        if minutes >= 60:
            return None
    elif len(parts) == 2:
        if not parts[0].isdigit():
            return None
        hours = 0
        minutes = int(parts[0])
    else:
        hours = 0
        minutes = 0
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def parse_memory_bytes(value: str) -> int | None:
    match = _MEMORY.fullmatch(value.strip())
    if match is None:
        return None
    exponent = " KMGTPE".index(match.group("unit").upper())
    return int(float(match.group("value")) * (1024**exponent))


def parse_sstat(stdout: str) -> ParsedRows[SstatStepRecord]:
    records: list[SstatStepRecord] = []
    warnings: list[str] = []
    for line_number, columns in _pipe_rows(stdout, expected=7, operation="SSTAT"):
        step_id = columns[0]
        if _JOB_ID.fullmatch(step_id) is None:
            warnings.append(f"MALFORMED_SSTAT_ROW:{line_number}")
            continue
        records.append(
            SstatStepRecord(
                job_id=step_id.split(".", 1)[0],
                step_id=step_id,
                tasks=_integer(columns[1]),
                allocated_tres=_parse_tres(columns[2]),
                average_cpu_seconds=parse_slurm_duration_seconds(columns[3]),
                max_rss_bytes=parse_memory_bytes(columns[4]),
                tres_usage_in_total=columns[5],
                tres_usage_out_total=columns[6],
            )
        )
    warnings.extend(_row_warnings(stdout, expected=7, operation="SSTAT"))
    return ParsedRows(tuple(records), tuple(sorted(warnings, key=_warning_line)))


def parse_sacct(stdout: str) -> ParsedRows[SacctRecord]:
    records: list[SacctRecord] = []
    warnings: list[str] = []
    for line_number, columns in _pipe_rows(stdout, expected=11, operation="SACCT"):
        job_id = columns[0]
        if _JOB_ID.fullmatch(job_id) is None:
            warnings.append(f"MALFORMED_SACCT_ROW:{line_number}")
            continue
        records.append(
            SacctRecord(
                job_id_raw=job_id,
                state=columns[1],
                exit_code=columns[2],
                elapsed_seconds=_integer(columns[3]),
                time_limit_minutes=_integer(columns[4]),
                allocated_tres=_parse_tres(columns[5]),
                allocated_cpus=_integer(columns[6]),
                task_count=_integer(columns[7]),
                total_cpu_seconds=parse_slurm_duration_seconds(columns[8]),
                cpu_time_raw_seconds=_integer(columns[9]),
                max_rss_bytes=parse_memory_bytes(columns[10]),
            )
        )
    warnings.extend(_row_warnings(stdout, expected=11, operation="SACCT"))
    return ParsedRows(tuple(records), tuple(sorted(warnings, key=_warning_line)))


def summarize_sacct_job(records: tuple[SacctRecord, ...], job_id: str) -> SacctJobUsage | None:
    allocation = next((record for record in records if record.job_id_raw == job_id), None)
    if allocation is None:
        return None
    step_prefix = f"{job_id}."
    step_rss = [
        record.max_rss_bytes
        for record in records
        if record.job_id_raw.startswith(step_prefix) and record.max_rss_bytes is not None
    ]
    step_tasks = [
        record.task_count
        for record in records
        if record.job_id_raw.startswith(step_prefix)
        and not record.job_id_raw.endswith(".extern")
        and record.task_count is not None
    ]
    tres = dict(allocation.allocated_tres)
    return SacctJobUsage(
        job_id=job_id,
        state=allocation.state,
        exit_code=allocation.exit_code,
        elapsed_seconds=allocation.elapsed_seconds,
        requested_walltime_seconds=(
            None if allocation.time_limit_minutes is None else allocation.time_limit_minutes * 60
        ),
        allocated_cpus=allocation.allocated_cpus,
        task_count=(
            allocation.task_count
            if allocation.task_count is not None
            else max(step_tasks)
            if step_tasks
            else None
        ),
        allocated_memory_bytes=parse_memory_bytes(tres.get("mem", "")),
        total_cpu_seconds=allocation.total_cpu_seconds,
        cpu_time_raw_seconds=allocation.cpu_time_raw_seconds,
        max_rss_bytes=max(step_rss) if step_rss else None,
    )


def _pipe_rows(stdout: str, *, expected: int, operation: str) -> tuple[tuple[int, list[str]], ...]:
    del operation
    return tuple(
        (line_number, [column.strip() for column in line.split("|")])
        for line_number, line in enumerate(stdout.splitlines(), start=1)
        if line and len(line.split("|")) == expected
    )


def _row_warnings(stdout: str, *, expected: int, operation: str) -> tuple[str, ...]:
    return tuple(
        f"MALFORMED_{operation}_ROW:{line_number}"
        for line_number, line in enumerate(stdout.splitlines(), start=1)
        if line and len(line.split("|")) != expected
    )


def _warning_line(warning: str) -> int:
    return int(warning.rsplit(":", 1)[1])


def _integer(value: str) -> int | None:
    return int(value) if value.isdigit() else None


def _parse_tres(value: str) -> tuple[tuple[str, str], ...]:
    result: list[tuple[str, str]] = []
    for item in value.split(","):
        key, separator, raw = item.partition("=")
        if separator and key and raw:
            result.append((key, raw))
    return tuple(result)
