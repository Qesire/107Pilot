from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from pilot107.core.novice_acceptance import evaluate_novice_acceptance


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify the Phase 3D facilitated novice usability-study gate."
    )
    parser.add_argument("input", type=Path, help="Anonymized study JSON")
    parser.add_argument("--out", type=Path, help="Optional machine-readable report path")
    args = parser.parse_args(argv)

    try:
        payload: Any = json.loads(args.input.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"unable to read novice acceptance input: {exc}", file=sys.stderr)
        return 1
    if not isinstance(payload, dict):
        print("novice acceptance input must be a JSON object", file=sys.stderr)
        return 1

    report = evaluate_novice_acceptance(payload).to_payload()
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    if report["status"] == "passed":
        return 0
    if report["status"] == "pending":
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
