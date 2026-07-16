#!/usr/bin/env python3
# ruff: noqa: E402,I001
"""Read-only CLI PlatformSnapshot probe for real 107 or simulator login nodes.

This script intentionally executes only the allowlisted commands defined by
``pilot107.adapters.platform_cli``. It does not accept arbitrary command input.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


def _ensure_import_path() -> None:
    script_dir = Path(__file__).resolve().parent
    candidates = (
        script_dir,
        script_dir / "src",
        script_dir.parents[1] / "src",
    )
    for candidate in candidates:
        if (candidate / "pilot107").is_dir():
            sys.path.insert(0, str(candidate))
            return


_ensure_import_path()

from pilot107.core.platform_snapshot import PlatformSnapshot  # noqa: E402
from pilot107.services.platform_snapshot_service import PlatformSnapshotService  # noqa: E402


RAW_FILENAMES = {
    "hostname": "hostname.txt",
    "pwd": "pwd.txt",
    "whoami": "whoami.txt",
    "date_iso": "date.txt",
    "python_version": "python-version.txt",
    "which_python": "which-python.txt",
    "scontrol_show_part": "scontrol-show-part.txt",
    "scontrol_show_nodes": "scontrol-show-nodes.txt",
    "sinfo_pipe": "sinfo.txt",
    "squeue_user_pipe": "squeue.txt",
    "df_public_home": "df-public-home.txt",
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Collect a read-only CLI PlatformSnapshot with fixed allowlisted commands."
    )
    parser.add_argument(
        "--out-dir",
        default=os.environ.get("PILOT107_REAL107_CLI_OUT_DIR", "real107-cli-snapshot"),
    )
    parser.add_argument(
        "--username",
        default=os.environ.get("PILOT107_REAL107_USERNAME") or os.environ.get("USER", "unknown"),
    )
    parser.add_argument(
        "--home",
        default=os.environ.get("PILOT107_REAL107_HOME") or os.environ.get("HOME"),
    )
    parser.add_argument(
        "--source-name",
        default=os.environ.get("PILOT107_REAL107_CLI_SOURCE", "real107-cli-test-only"),
    )
    parser.add_argument("--expires-hours", type=int, default=24)
    parser.add_argument("--max-output-chars", type=int, default=200_000)
    args = parser.parse_args(argv)

    captured_at = datetime.now(UTC)
    service = PlatformSnapshotService()
    service.collector.max_output_chars = args.max_output_chars
    snapshot = service.collect_login_snapshot(
        username=args.username,
        home=args.home,
        captured_at=captured_at.isoformat(),
    )
    expires_at = captured_at + timedelta(hours=args.expires_hours)
    write_snapshot_artifacts(
        snapshot=snapshot,
        out_dir=Path(args.out_dir),
        source_name=args.source_name,
        expires_at=expires_at.isoformat(),
    )
    print("platform_snapshot=" + str(Path(args.out_dir) / "platform_snapshot.json"))
    print("summary_status=" + ("ok" if not snapshot.limitations else "partial"))
    return 0


def write_snapshot_artifacts(
    *,
    snapshot: PlatformSnapshot,
    out_dir: Path,
    source_name: str,
    expires_at: str,
) -> None:
    raw_dir = out_dir / "raw"
    parsed_dir = out_dir / "parsed"
    raw_dir.mkdir(parents=True, exist_ok=True)
    parsed_dir.mkdir(parents=True, exist_ok=True)

    _write_json(out_dir / "platform_snapshot.json", snapshot.to_payload())
    _write_json(
        out_dir / "manifest.json",
        {
            "schema": "pilot107.platform_snapshot_artifacts.v1",
            "snapshot_id": snapshot.snapshot_id,
            "scope": snapshot.scope.value,
            "collector_version": snapshot.collector_version,
            "captured_at": snapshot.captured_at,
            "expires_at": expires_at,
            "source_type": "cli",
            "source_name": source_name,
            "warning": (
                "CLI/SSH access is treated as test-only unless an administrator "
                "explicitly approves it as a production capability."
            ),
            "files": {
                "snapshot": "platform_snapshot.json",
                "partitions": "parsed/partitions.json",
                "nodes": "parsed/nodes.json",
                "squeue": "parsed/squeue.json",
                "runtime": "parsed/runtime.json",
                "warnings": "warnings.json",
                "redaction_report": "redaction-report.json",
            },
        },
    )
    _write_json(parsed_dir / "partitions.json", [item.to_payload() for item in snapshot.partitions])
    _write_json(parsed_dir / "nodes.json", [item.to_payload() for item in snapshot.nodes])
    _write_json(parsed_dir / "squeue.json", [item.to_payload() for item in snapshot.squeue_jobs])
    _write_json(parsed_dir / "runtime.json", _runtime_payload(snapshot))
    _write_json(out_dir / "warnings.json", {"warnings": list(snapshot.limitations)})
    _write_json(out_dir / "redaction-report.json", {"redactions": list(snapshot.redaction_report)})

    for command in snapshot.command_results:
        filename = RAW_FILENAMES.get(command.name, f"{command.name}.txt")
        content = command.stdout
        if command.stderr:
            content += "\n--- stderr ---\n" + command.stderr
        (raw_dir / filename).write_text(content, encoding="utf-8")


def _runtime_payload(snapshot: PlatformSnapshot) -> dict[str, Any]:
    runtime_names = {
        "hostname",
        "pwd",
        "whoami",
        "date_iso",
        "python_version",
        "which_python",
        "df_public_home",
    }
    return {
        command.name: {
            "argv": list(command.argv),
            "returncode": command.returncode,
            "stdout": command.stdout,
            "stderr": command.stderr,
            "timed_out": command.timed_out,
            "truncated": command.truncated,
        }
        for command in snapshot.command_results
        if command.name in runtime_names
    }


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    raise SystemExit(main())
