from __future__ import annotations

import csv
import json
import math
from pathlib import Path

import pytest

from pilot107.scientific.heat_diffusion_validation import (
    audit_heat_diffusion_outputs,
)

REQUIRED_COLUMNS = (
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


def _write_valid_outputs(root: Path) -> None:
    grids = [64, 128, 256]
    errors = [(1.0 / grid) ** 2.01 for grid in grids]
    convergence_times = [0.08, 0.31, 1.25]
    scaling_times = [4.0, 2.4, 1.6]
    rows = [
        {
            "phase": "convergence",
            "grid": grid,
            "threads": 1,
            "alpha": 0.1,
            "t_final": 0.01,
            "dt": 0.00001,
            "steps": 1000,
            "l2_error": error,
            "elapsed_seconds": elapsed,
        }
        for grid, error, elapsed in zip(grids, errors, convergence_times, strict=True)
    ]
    rows.extend(
        {
            "phase": "scaling",
            "grid": 256,
            "threads": threads,
            "alpha": 0.1,
            "t_final": 0.01,
            "dt": 0.00001,
            "steps": 1000,
            "l2_error": errors[-1],
            "elapsed_seconds": elapsed,
        }
        for threads, elapsed in zip([1, 2, 4], scaling_times, strict=True)
    )
    with (root / "raw-results.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=REQUIRED_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)

    orders = [
        math.log(errors[index] / errors[index + 1]) / math.log(grids[index + 1] / grids[index])
        for index in range(2)
    ]
    (root / "convergence.json").write_text(
        json.dumps(
            {
                "scheme": "explicit-five-point",
                "grids": grids,
                "errors": errors,
                "observed_orders": orders,
                "order_min": min(orders),
                "order_max": max(orders),
            }
        ),
        encoding="utf-8",
    )
    speedup = [scaling_times[0] / value for value in scaling_times]
    (root / "scaling.json").write_text(
        json.dumps(
            {
                "threads": [1, 2, 4],
                "elapsed_seconds": scaling_times,
                "speedup": speedup,
                "efficiency": [
                    value / threads for value, threads in zip(speedup, [1, 2, 4], strict=True)
                ],
            }
        ),
        encoding="utf-8",
    )
    (root / "report.md").write_text(
        "# Heat diffusion report\n\n"
        "Analytic solution: `u(x,y,t) = sin(pi*x) * sin(pi*y) * "
        "exp(-2*pi*pi*alpha*t)`.\n\n"
        "platform_snapshot_id: platform-demo\n\nrun_id: run-demo\n",
        encoding="utf-8",
    )
    for filename, title, x_axis, y_axis in (
        ("convergence.svg", "Convergence", "Grid resolution", "L2 error"),
        ("scaling.svg", "Scaling", "Threads", "Elapsed seconds"),
    ):
        (root / filename).write_text(
            f'<svg xmlns="http://www.w3.org/2000/svg"><title>{title}</title>'
            f"<text>{x_axis}</text><text>{y_axis}</text></svg>",
            encoding="utf-8",
        )


def _audit(root: Path):
    _write_valid_outputs(root)
    return audit_heat_diffusion_outputs(root)


def test_accepts_consistent_second_order_outputs(tmp_path: Path) -> None:
    audit = _audit(tmp_path)

    assert audit.status == "PASS"
    assert audit.observed_order == pytest.approx(2.01)
    assert audit.grids == (64, 128, 256)
    assert audit.threads == (1, 2, 4)
    assert all(check["passed"] is True for check in audit.checks)


@pytest.mark.parametrize("missing", ["report.md", "convergence.svg", "scaling.svg"])
def test_fails_when_required_presentation_output_is_missing(tmp_path: Path, missing: str) -> None:
    _write_valid_outputs(tmp_path)
    (tmp_path / missing).unlink()

    assert audit_heat_diffusion_outputs(tmp_path).status == "FAIL"


def test_rejects_non_finite_json_number(tmp_path: Path) -> None:
    _write_valid_outputs(tmp_path)
    payload = json.loads((tmp_path / "convergence.json").read_text(encoding="utf-8"))
    payload["errors"][0] = float("nan")
    (tmp_path / "convergence.json").write_text(json.dumps(payload), encoding="utf-8")

    assert audit_heat_diffusion_outputs(tmp_path).status == "FAIL"


@pytest.mark.parametrize(
    ("filename", "field", "value"),
    [
        ("convergence.json", "grids", [32, 64, 128]),
        ("scaling.json", "threads", [1, 2, 8]),
    ],
)
def test_rejects_wrong_grid_or_thread_set(
    tmp_path: Path, filename: str, field: str, value: list[int]
) -> None:
    _write_valid_outputs(tmp_path)
    payload = json.loads((tmp_path / filename).read_text(encoding="utf-8"))
    payload[field] = value
    (tmp_path / filename).write_text(json.dumps(payload), encoding="utf-8")

    assert audit_heat_diffusion_outputs(tmp_path).status == "FAIL"


def test_recomputes_and_rejects_inconsistent_speedup(tmp_path: Path) -> None:
    _write_valid_outputs(tmp_path)
    payload = json.loads((tmp_path / "scaling.json").read_text(encoding="utf-8"))
    payload["speedup"][1] = 99.0
    (tmp_path / "scaling.json").write_text(json.dumps(payload), encoding="utf-8")

    assert audit_heat_diffusion_outputs(tmp_path).status == "FAIL"


def test_recomputes_and_rejects_order_outside_acceptance_band(tmp_path: Path) -> None:
    _write_valid_outputs(tmp_path)
    payload = json.loads((tmp_path / "convergence.json").read_text(encoding="utf-8"))
    payload["errors"] = [0.1, 0.05, 0.025]
    payload["observed_orders"] = [1.0, 1.0]
    payload["order_min"] = 1.0
    payload["order_max"] = 1.0
    (tmp_path / "convergence.json").write_text(json.dumps(payload), encoding="utf-8")

    assert audit_heat_diffusion_outputs(tmp_path).status == "FAIL"


def test_rejects_svg_without_title_or_axis_labels(tmp_path: Path) -> None:
    _write_valid_outputs(tmp_path)
    (tmp_path / "convergence.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg"><path d="M0 0"/></svg>',
        encoding="utf-8",
    )

    assert audit_heat_diffusion_outputs(tmp_path).status == "FAIL"


def test_rejects_report_without_solution_or_provenance(tmp_path: Path) -> None:
    _write_valid_outputs(tmp_path)
    (tmp_path / "report.md").write_text("# Results\nLooks good.\n", encoding="utf-8")

    assert audit_heat_diffusion_outputs(tmp_path).status == "FAIL"


def test_rejects_symlinked_output(tmp_path: Path) -> None:
    _write_valid_outputs(tmp_path)
    target = tmp_path / "outside-report.md"
    target.write_text("not trusted", encoding="utf-8")
    (tmp_path / "report.md").unlink()
    (tmp_path / "report.md").symlink_to(target)

    assert audit_heat_diffusion_outputs(tmp_path).status == "FAIL"
