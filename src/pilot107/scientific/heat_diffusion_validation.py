"""Fail-closed audit for the model-generated heat-diffusion demonstration."""

from __future__ import annotations

import csv
import json
import math
import xml.etree.ElementTree as ET
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

_REQUIRED_FILES = (
    "raw-results.csv",
    "convergence.json",
    "scaling.json",
    "report.md",
    "convergence.svg",
    "scaling.svg",
)
_CSV_COLUMNS = (
    "phase",
    "grid",
    "threads",
    "alpha",
    "t_final",
    "dt",
    "steps",
    "l2_error",
    "elapsed_seconds",
)
_GRIDS = (64, 128, 256)
_THREADS = (1, 2, 4)
_REL_TOLERANCE = 1e-6


@dataclass(frozen=True)
class HeatDiffusionAudit:
    status: Literal["PASS", "FAIL"]
    observed_order: float | None
    grids: tuple[int, ...]
    threads: tuple[int, ...]
    required_files: tuple[str, ...]
    checks: tuple[dict[str, object], ...]


@dataclass(frozen=True)
class _RawRow:
    phase: str
    grid: int
    threads: int
    alpha: float
    t_final: float
    dt: float
    steps: int
    l2_error: float
    elapsed_seconds: float


def audit_heat_diffusion_outputs(root: Path) -> HeatDiffusionAudit:
    """Audit one output directory without trusting its reported summary metrics."""

    checks: list[dict[str, object]] = []
    observed_order: float | None = None
    grids: tuple[int, ...] = ()
    threads: tuple[int, ...] = ()
    try:
        paths = _required_regular_files(root)
        _passed(checks, "required_regular_files")

        rows = _read_csv(paths["raw-results.csv"])
        _passed(checks, "raw_results_shape")

        convergence = _read_json(paths["convergence.json"])
        scaling = _read_json(paths["scaling.json"])
        grids, observed_order = _audit_convergence(rows, convergence)
        _passed(checks, "convergence_consistency")

        threads = _audit_scaling(rows, scaling)
        _passed(checks, "scaling_consistency")

        _audit_report(paths["report.md"])
        _passed(checks, "report_provenance")
        _audit_svg(paths["convergence.svg"], required_terms=("grid", "error"))
        _audit_svg(paths["scaling.svg"], required_terms=("thread", "elapsed"))
        _passed(checks, "accessible_figures")
    except (OSError, UnicodeError, ValueError, TypeError, ET.ParseError) as exc:
        checks.append(
            {
                "name": "artifact_integrity",
                "passed": False,
                "message": str(exc)[:512] or type(exc).__name__,
            }
        )

    return HeatDiffusionAudit(
        status="PASS" if checks and all(check["passed"] is True for check in checks) else "FAIL",
        observed_order=observed_order,
        grids=grids,
        threads=threads,
        required_files=_REQUIRED_FILES,
        checks=tuple(checks),
    )


def _required_regular_files(root: Path) -> dict[str, Path]:
    if not isinstance(root, Path):
        raise TypeError("output root must be a Path")
    if root.is_symlink() or not root.is_dir():
        raise ValueError("output root must be a regular directory")
    paths: dict[str, Path] = {}
    for name in _REQUIRED_FILES:
        path = root / name
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"required output is missing or not a regular file: {name}")
        paths[name] = path
    return paths


def _read_csv(path: Path) -> tuple[_RawRow, ...]:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != _CSV_COLUMNS:
            raise ValueError("raw-results.csv columns are invalid")
        rows = tuple(_raw_row(row) for row in reader)
    if not rows:
        raise ValueError("raw-results.csv contains no data")
    keys = [(row.phase, row.grid, row.threads) for row in rows]
    if len(keys) != len(set(keys)):
        raise ValueError("raw-results.csv contains duplicate phase/grid/thread rows")
    return rows


def _raw_row(row: Mapping[str, str | None]) -> _RawRow:
    phase = row.get("phase")
    if phase not in {"convergence", "scaling"}:
        raise ValueError("raw result phase is invalid")
    result = _RawRow(
        phase=phase,
        grid=_csv_int(row, "grid"),
        threads=_csv_int(row, "threads"),
        alpha=_csv_float(row, "alpha"),
        t_final=_csv_float(row, "t_final"),
        dt=_csv_float(row, "dt"),
        steps=_csv_int(row, "steps"),
        l2_error=_csv_float(row, "l2_error"),
        elapsed_seconds=_csv_float(row, "elapsed_seconds"),
    )
    if min(
        result.grid,
        result.threads,
        result.alpha,
        result.t_final,
        result.dt,
        result.steps,
        result.l2_error,
        result.elapsed_seconds,
    ) <= 0:
        raise ValueError("raw result numeric values must be positive")
    return result


def _read_json(path: Path) -> Mapping[str, object]:
    def reject_constant(value: str) -> object:
        raise ValueError(f"non-finite JSON number is forbidden: {value}")

    value = json.loads(path.read_text(encoding="utf-8"), parse_constant=reject_constant)
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain an object")
    return value


def _audit_convergence(
    rows: Sequence[_RawRow], payload: Mapping[str, object]
) -> tuple[tuple[int, ...], float]:
    expected_fields = {
        "scheme",
        "grids",
        "errors",
        "observed_orders",
        "order_min",
        "order_max",
    }
    if set(payload) != expected_fields or payload.get("scheme") != "explicit-five-point":
        raise ValueError("convergence.json shape or scheme is invalid")
    grids = _integer_sequence(payload.get("grids"), "convergence grids")
    errors = _number_sequence(payload.get("errors"), "convergence errors")
    reported_orders = _number_sequence(
        payload.get("observed_orders"), "observed orders"
    )
    if grids != _GRIDS or len(errors) != 3 or len(reported_orders) != 2:
        raise ValueError("convergence grid or metric count is invalid")
    if any(error <= 0 for error in errors) or not all(
        coarse > fine for coarse, fine in zip(errors[:-1], errors[1:], strict=True)
    ):
        raise ValueError("convergence errors must be positive and strictly decreasing")

    raw = sorted((row for row in rows if row.phase == "convergence"), key=lambda row: row.grid)
    if tuple(row.grid for row in raw) != _GRIDS or any(row.threads != 1 for row in raw):
        raise ValueError("raw convergence rows do not use the required grids and one thread")
    _require_close_sequence(tuple(row.l2_error for row in raw), errors, "convergence errors")

    orders = tuple(
        math.log(coarse / fine) / math.log(float(fine_grid) / float(coarse_grid))
        for coarse, fine, coarse_grid, fine_grid in zip(
            errors[:-1], errors[1:], grids[:-1], grids[1:], strict=True
        )
    )
    _require_close_sequence(reported_orders, orders, "observed orders")
    order_min = _number(payload.get("order_min"), "order_min")
    order_max = _number(payload.get("order_max"), "order_max")
    _require_close(order_min, min(orders), "order_min")
    _require_close(order_max, max(orders), "order_max")
    if not all(1.8 <= order <= 2.2 for order in orders):
        raise ValueError("observed spatial order is outside [1.8, 2.2]")
    return grids, min(orders)


def _audit_scaling(
    rows: Sequence[_RawRow], payload: Mapping[str, object]
) -> tuple[int, ...]:
    expected_fields = {"threads", "elapsed_seconds", "speedup", "efficiency"}
    if set(payload) != expected_fields:
        raise ValueError("scaling.json shape is invalid")
    threads = _integer_sequence(payload.get("threads"), "scaling threads")
    elapsed = _number_sequence(payload.get("elapsed_seconds"), "scaling elapsed times")
    reported_speedup = _number_sequence(payload.get("speedup"), "scaling speedup")
    reported_efficiency = _number_sequence(payload.get("efficiency"), "scaling efficiency")
    if threads != _THREADS or not all(
        len(values) == 3 for values in (elapsed, reported_speedup, reported_efficiency)
    ):
        raise ValueError("scaling thread or metric count is invalid")
    if any(value <= 0 for value in elapsed):
        raise ValueError("scaling elapsed times must be positive")

    raw = sorted((row for row in rows if row.phase == "scaling"), key=lambda row: row.threads)
    if tuple(row.threads for row in raw) != _THREADS or any(row.grid != 256 for row in raw):
        raise ValueError("raw scaling rows do not use the required threads and grid")
    _require_close_sequence(tuple(row.elapsed_seconds for row in raw), elapsed, "elapsed times")

    speedup = tuple(elapsed[0] / value for value in elapsed)
    efficiency = tuple(value / thread for value, thread in zip(speedup, threads, strict=True))
    _require_close_sequence(reported_speedup, speedup, "speedup")
    _require_close_sequence(reported_efficiency, efficiency, "efficiency")
    return threads


def _audit_report(path: Path) -> None:
    report = path.read_text(encoding="utf-8").lower().replace(" ", "")
    required = (
        "sin(pi*x)",
        "sin(pi*y)",
        "exp(-2*pi*pi*alpha*t)",
        "platform_snapshot_id:",
        "run_id:",
    )
    if not all(fragment in report for fragment in required):
        raise ValueError("report.md lacks the analytic solution or provenance fields")


def _audit_svg(path: Path, *, required_terms: tuple[str, str]) -> None:
    root = ET.fromstring(path.read_text(encoding="utf-8"))
    title = next(
        (
            element
            for element in root.iter()
            if element.tag.rsplit("}", 1)[-1].lower() == "title"
            and (element.text or "").strip()
        ),
        None,
    )
    text = " ".join(part.strip().lower() for part in root.itertext() if part.strip())
    if title is None or not all(term in text for term in required_terms):
        raise ValueError(f"{path.name} lacks a title or semantic axis labels")


def _integer_sequence(value: object, label: str) -> tuple[int, ...]:
    if not isinstance(value, list) or any(
        isinstance(item, bool) or not isinstance(item, int) for item in value
    ):
        raise ValueError(f"{label} must be an integer array")
    return tuple(value)


def _number_sequence(value: object, label: str) -> tuple[float, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a numeric array")
    return tuple(_number(item, label) for item in value)


def _number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{label} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{label} must be finite")
    return result


def _csv_int(row: Mapping[str, str | None], field: str) -> int:
    value = row.get(field)
    if value is None:
        raise ValueError(f"raw result {field} is missing")
    try:
        return int(value)
    except ValueError:
        raise ValueError(f"raw result {field} must be an integer") from None


def _csv_float(row: Mapping[str, str | None], field: str) -> float:
    value = row.get(field)
    if value is None:
        raise ValueError(f"raw result {field} is missing")
    try:
        result = float(value)
    except ValueError:
        raise ValueError(f"raw result {field} must be numeric") from None
    if not math.isfinite(result):
        raise ValueError(f"raw result {field} must be finite")
    return result


def _require_close(actual: float, expected: float, label: str) -> None:
    if not math.isclose(actual, expected, rel_tol=_REL_TOLERANCE, abs_tol=1e-12):
        raise ValueError(f"reported {label} is inconsistent with raw data")


def _require_close_sequence(
    actual: Sequence[float], expected: Sequence[float], label: str
) -> None:
    if len(actual) != len(expected):
        raise ValueError(f"reported {label} count is inconsistent with raw data")
    for actual_value, expected_value in zip(actual, expected, strict=True):
        _require_close(actual_value, expected_value, label)


def _passed(checks: list[dict[str, object]], name: str) -> None:
    checks.append({"name": name, "passed": True, "message": "verified"})
