from __future__ import annotations

import pytest

from pilot107.observability.slurm_parser import (
    parse_sacct,
    parse_slurm_duration_seconds,
    parse_sstat,
    summarize_sacct_job,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("12", 12.0),
        ("03:04.5", 184.5),
        ("02:03:04.125", 7384.125),
        ("1-02:03:04.5", 93784.5),
    ],
)
def test_parse_slurm_duration_accepts_fractional_and_day_formats(
    raw: str, expected: float
) -> None:
    assert parse_slurm_duration_seconds(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["", "1:60", "00:60:00", "00:00:60", "x:01", "1-2:3:4:5"],
)
def test_parse_slurm_duration_rejects_invalid_values(raw: str) -> None:
    assert parse_slurm_duration_seconds(raw) is None


def test_parse_sacct_preserves_empty_tail_and_joins_allocation_with_steps() -> None:
    parsed = parse_sacct(
        "101|COMPLETED|0:0|13|5|cpu=1,mem=32M|1||00:12.018|13|\n"
        "101.batch|COMPLETED|0:0|13||cpu=1,mem=32M|1|1|00:12.018|13|44000K\n"
        "101.extern|COMPLETED|0:0|13||cpu=1,mem=32M|1|1|00:00.001|13|2M\n"
    )

    assert parsed.warnings == ()
    assert len(parsed.records) == 3
    assert parsed.records[0].max_rss_bytes is None

    usage = summarize_sacct_job(parsed.records, "101")

    assert usage is not None
    assert usage.total_cpu_seconds == 12.018
    assert usage.elapsed_seconds == 13
    assert usage.requested_walltime_seconds == 300
    assert usage.allocated_cpus == 1
    assert usage.task_count == 1
    assert usage.cpu_time_raw_seconds == 13
    assert usage.allocated_memory_bytes == 32 * 1024**2
    assert usage.max_rss_bytes == 44000 * 1024


def test_parse_sstat_groups_step_records_by_base_job_id() -> None:
    parsed = parse_sstat(
        "101.batch|1|cpu=1,mem=32M|00:00:02.5|40M|4096|512\n"
        "102.extern|1|cpu=1,mem=32M|00:00:00|2M|0|0\n"
    )

    assert parsed.warnings == ()
    assert parsed.records[0].job_id == "101"
    assert parsed.records[0].step_id == "101.batch"
    assert parsed.records[0].max_rss_bytes == 40 * 1024**2


def test_parsers_reject_malformed_rows_without_echoing_untrusted_content() -> None:
    parsed = parse_sacct("not|enough|columns\n")

    assert parsed.records == ()
    assert parsed.warnings == ("MALFORMED_SACCT_ROW:1",)
