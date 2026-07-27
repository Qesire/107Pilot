#!/usr/bin/env python3
"""Safely migrate 107Pilot's business-domain data from SQLite to PostgreSQL."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from pilot107.core.postgres_domain_migration import (
    DomainDataMigrationError,
    migrate_sqlite_domain_to_postgres,
    verify_sqlite_domain_matches_postgres,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Migrate a quiesced 107Pilot SQLite business database into PostgreSQL."
    )
    parser.add_argument("--sqlite-db", required=True, type=Path)
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="compare SQLite and PostgreSQL without writing either database",
    )
    parser.add_argument(
        "--source-quiesced",
        action="store_true",
        help="confirm API and Worker writers have been stopped before copying",
    )
    args = parser.parse_args()
    dsn = os.environ.get("PILOT107_POSTGRES_DSN")
    if not dsn:
        parser.error("PILOT107_POSTGRES_DSN must be supplied through the environment")
    try:
        if args.verify_only:
            report = verify_sqlite_domain_matches_postgres(
                sqlite_path=args.sqlite_db,
                postgres_dsn=dsn,
            )
        else:
            report = migrate_sqlite_domain_to_postgres(
                sqlite_path=args.sqlite_db,
                postgres_dsn=dsn,
                source_quiesced=args.source_quiesced,
            )
    except (DomainDataMigrationError, ValueError) as exc:
        print(f"migration failed: {type(exc).__name__}: {exc}")
        return 1
    print(
        json.dumps(
            {
                "source_tables": report.source_tables,
                "target_tables": report.target_tables,
                "source_digest": report.source_digest,
                "target_digest": report.target_digest,
                "transferred": report.transferred,
                "already_complete": report.already_complete,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
