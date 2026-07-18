#!/usr/bin/env python3
"""Find missing array outputs using artifact + marker (+ optional metadata) truth."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--expected-tasks", type=int, required=True)
    parser.add_argument("--artifact-pattern", default="shards/task_{task}.bin")
    parser.add_argument("--marker-pattern", default="complete/task_{task}.COMPLETE")
    parser.add_argument("--metadata-pattern")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--require-complete", action="store_true")
    args = parser.parse_args()

    if not 1 <= args.expected_tasks <= 100_000:
        parser.error("--expected-tasks must be between 1 and 100000")
    patterns = [args.artifact_pattern, args.marker_pattern]
    if args.metadata_pattern:
        patterns.append(args.metadata_pattern)
    for pattern in patterns:
        _validate_pattern(pattern)

    root = args.root.resolve(strict=True)
    missing: list[dict[str, object]] = []
    for task in range(args.expected_tasks):
        absent = [
            pattern
            for pattern in patterns
            if not _nonempty_file_under(root, pattern.format(task=task))
        ]
        if absent:
            missing.append({"task": task, "missing": absent})

    payload = {
        "schema": "pilot107.array_artifact_scan.v1",
        "root": str(root),
        "expected_tasks": args.expected_tasks,
        "complete_tasks": args.expected_tasks - len(missing),
        "missing_tasks": [item["task"] for item in missing],
        "missing_array_spec": _compress_ranges([int(item["task"]) for item in missing]),
        "details": missing,
    }
    if args.output is not None:
        _atomic_json(args.output, payload)
    print(payload["missing_array_spec"])
    return 1 if args.require_complete and missing else 0


def _validate_pattern(pattern: str) -> None:
    path = Path(pattern)
    if pattern.count("{task}") != 1:
        raise SystemExit(f"pattern must contain exactly one {{task}}: {pattern}")
    if path.is_absolute() or ".." in path.parts or "\x00" in pattern:
        raise SystemExit(f"pattern must stay below --root: {pattern}")


def _nonempty_file_under(root: Path, relative: str) -> bool:
    candidate = root / relative
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
        return resolved.is_file() and resolved.stat().st_size > 0
    except (FileNotFoundError, OSError, ValueError):
        return False


def _compress_ranges(values: list[int]) -> str:
    if not values:
        return ""
    ranges: list[str] = []
    start = previous = values[0]
    for value in values[1:]:
        if value == previous + 1:
            previous = value
            continue
        ranges.append(str(start) if start == previous else f"{start}-{previous}")
        start = previous = value
    ranges.append(str(start) if start == previous else f"{start}-{previous}")
    return ",".join(ranges)


def _atomic_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    os.replace(temporary, path)


if __name__ == "__main__":
    raise SystemExit(main())
