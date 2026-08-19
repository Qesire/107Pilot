"""Deterministic, side-effect-free alerts over persisted Runtime log bytes."""

from __future__ import annotations

import re
from dataclasses import dataclass

from pilot107.runtime_watch.model import RuntimeAlert, RuntimeAlertSeverity, RuntimeLogSegment


@dataclass(frozen=True)
class _Rule:
    code: str
    severity: RuntimeAlertSeverity
    summary: str
    pattern: re.Pattern[bytes]


_RULES = (
    _Rule(
        "PYTHON.MISSING_IMPORT",
        "critical",
        "Python dependency import failed",
        re.compile(rb"(?:ModuleNotFoundError|ImportError)(?::|\b)", re.I),
    ),
    _Rule(
        "COMMAND.NOT_FOUND",
        "critical",
        "Command was not found",
        re.compile(rb"command not found", re.I),
    ),
    _Rule(
        "PATH.MISSING",
        "critical",
        "A required path was not found",
        re.compile(rb"(?:No such file or directory|cannot access .+No such file)", re.I),
    ),
    _Rule(
        "CUDA.OOM",
        "critical",
        "CUDA reported out of memory",
        re.compile(rb"(?:CUDA out of memory|CUDA error: out of memory)", re.I),
    ),
    _Rule(
        "NCCL.ERROR",
        "critical",
        "NCCL reported a distributed runtime error",
        re.compile(rb"NCCL.{0,80}(?:error|failed|failure)", re.I | re.S),
    ),
    _Rule(
        "NUMERIC.NON_FINITE",
        "warning",
        "Runtime output contains NaN or Inf",
        re.compile(rb"(?<![A-Za-z])(?:nan|[+-]?inf(?:inity)?)(?![A-Za-z])", re.I),
    ),
    _Rule(
        "SLURM.INVALID_QOS_OR_ACCOUNT",
        "critical",
        "Slurm rejected the QoS or account",
        re.compile(rb"Invalid(?:QOS| qos| account)|invalid account", re.I),
    ),
    _Rule(
        "SLURM.IMPOSSIBLE_DEPENDENCY",
        "critical",
        "Slurm dependency can never be satisfied",
        re.compile(rb"dependency (?:can never be satisfied|is impossible|failed)", re.I),
    ),
    _Rule(
        "RESOURCE.PRESSURE",
        "warning",
        "Runtime output references resource pressure",
        re.compile(rb"(?:oom-kill|out of memory|memory pressure|disk quota exceeded)", re.I),
    ),
)


class RuntimeAlertEvaluator:
    def __init__(self, *, boundary_bytes: int = 512) -> None:
        if not 64 <= boundary_bytes <= 4096:
            raise ValueError("boundary_bytes must be between 64 and 4096")
        self.boundary_bytes = boundary_bytes

    def evaluate_segment(
        self,
        segment: RuntimeLogSegment,
        *,
        content: bytes,
        previous_tail: bytes,
        created_at: str,
    ) -> list[RuntimeAlert]:
        if len(content) != segment.content_size:
            raise ValueError("Runtime alert content does not match its segment")
        prefix = previous_tail[-self.boundary_bytes :]
        combined = prefix + content
        alerts: list[RuntimeAlert] = []
        seen: set[str] = set()
        for rule in _RULES:
            if rule.code == "RESOURCE.PRESSURE" and "CUDA.OOM" in seen:
                continue
            for match in rule.pattern.finditer(combined):
                if match.end() <= len(prefix) or rule.code in seen:
                    continue
                offset = max(0, segment.start_offset - len(prefix) + match.start())
                alerts.append(
                    RuntimeAlert.create(
                        watch_id=segment.watch_id,
                        run_id=segment.run_id,
                        owner=segment.owner,
                        code=rule.code,
                        severity=rule.severity,
                        summary=rule.summary,
                        segment_id=segment.segment_id,
                        generation=segment.generation,
                        offset=offset,
                        created_at=created_at,
                    )
                )
                seen.add(rule.code)
        return alerts
