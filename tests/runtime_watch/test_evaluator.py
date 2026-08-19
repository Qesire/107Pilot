from __future__ import annotations

from pilot107.runtime_watch.evaluator import RuntimeAlertEvaluator
from pilot107.runtime_watch.model import RuntimeLogSegment


def _segment() -> RuntimeLogSegment:
    return RuntimeLogSegment(
        segment_id="segment-one",
        watch_id="watch-one",
        run_id="run1",
        owner="alice",
        stream="stderr",
        generation=0,
        start_offset=15,
        end_offset=19,
        content_sha256="a" * 64,
        content_size=4,
        content_ref=f"sha256:{'a' * 64}",
        created_at="2026-08-19T00:00:00Z",
    )


def test_cuda_oom_is_detected_across_segment_boundary() -> None:
    alerts = RuntimeAlertEvaluator().evaluate_segment(
        _segment(),
        content=b"ory\n",
        previous_tail=b"CUDA out of mem",
        created_at="2026-08-19T00:00:01Z",
    )

    assert [(item.code, item.severity) for item in alerts] == [
        ("CUDA.OOM", "critical")
    ]
    assert alerts[0].offset == 0


def test_known_runtime_failures_are_side_effect_free_alerts() -> None:
    content = (
        b"ModuleNotFoundError: No module named x\n"
        b"bash: train: command not found\n"
        b"NCCL error: unhandled system error\n"
        b"loss=NaN InvalidQOS dependency can never be satisfied\n"
    )
    segment = RuntimeLogSegment(
        **{
            **_segment().__dict__,
            "start_offset": 0,
            "end_offset": len(content),
            "content_size": len(content),
        }
    )

    alerts = RuntimeAlertEvaluator().evaluate_segment(
        segment,
        content=content,
        previous_tail=b"",
        created_at="2026-08-19T00:00:01Z",
    )

    assert {item.code for item in alerts} >= {
        "PYTHON.MISSING_IMPORT",
        "COMMAND.NOT_FOUND",
        "NCCL.ERROR",
        "NUMERIC.NON_FINITE",
        "SLURM.INVALID_QOS_OR_ACCOUNT",
        "SLURM.IMPOSSIBLE_DEPENDENCY",
    }
