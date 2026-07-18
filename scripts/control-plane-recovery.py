#!/usr/bin/env python3
"""Create, verify, or restore an integrity-bound 107pilot local backup."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from pilot107.core.recovery import (
    RecoveryError,
    create_control_plane_backup,
    restore_control_plane_backup,
    verify_control_plane_backup,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser("create", help="create a new backup directory")
    create.add_argument("--destination", type=Path, required=True)
    create.add_argument("--sqlite-db", type=Path, required=True)
    create.add_argument("--evidence-root", type=Path)
    create.add_argument("--capsule-root", type=Path)
    create.add_argument("--postgres-dsn")
    create.add_argument(
        "--quiesced",
        action="store_true",
        required=True,
        help="confirm API/Worker writers are stopped",
    )

    verify = subparsers.add_parser("verify", help="verify hashes and SQLite integrity")
    verify.add_argument("--backup-root", type=Path, required=True)

    restore = subparsers.add_parser("restore", help="restore into a new or empty root")
    restore.add_argument("--backup-root", type=Path, required=True)
    restore.add_argument("--destination", type=Path, required=True)
    restore.add_argument("--postgres-dsn")
    restore.add_argument(
        "--quiesced",
        action="store_true",
        required=True,
        help="confirm API/Worker writers are stopped",
    )
    restore.add_argument(
        "--postgres-allow-reset",
        action="store_true",
        help="explicitly allow pg_restore --clean against the supplied DSN",
    )

    args = parser.parse_args()
    try:
        if args.command == "create":
            result = create_control_plane_backup(
                destination=args.destination,
                sqlite_db=args.sqlite_db,
                evidence_root=args.evidence_root,
                capsule_root=args.capsule_root,
                postgres_dsn=args.postgres_dsn,
                quiesced=args.quiesced,
            )
            payload = {
                "backup_id": result.backup_id,
                "backup_root": str(result.backup_root),
                "file_count": result.file_count,
                "manifest_sha256": result.manifest_sha256,
                "total_size_bytes": result.total_size_bytes,
            }
        elif args.command == "verify":
            manifest = verify_control_plane_backup(args.backup_root)
            payload = {
                "backup_id": manifest["backup_id"],
                "backup_root": str(args.backup_root.resolve()),
                "file_count": len(manifest["files"]),
                "verified": True,
            }
        else:
            if args.postgres_dsn and not args.postgres_allow_reset:
                parser.error("--postgres-dsn restore requires --postgres-allow-reset")
            result = restore_control_plane_backup(
                backup_root=args.backup_root,
                destination=args.destination,
                postgres_dsn=args.postgres_dsn,
                postgres_allow_reset=args.postgres_allow_reset,
                quiesced=args.quiesced,
            )
            payload = {
                "backup_id": result.backup_id,
                "file_count": result.file_count,
                "postgres_restored": result.postgres_restored,
                "restore_root": str(result.restore_root),
            }
    except RecoveryError as exc:
        parser.exit(2, f"recovery error: {exc}\n")
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
