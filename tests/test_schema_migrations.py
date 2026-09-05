import sqlite3
import tempfile
import unittest
from pathlib import Path

from pilot107.core.schema_migrations import (
    SchemaMigration,
    SchemaMigrationError,
    apply_schema_migrations,
)


class SchemaMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "migration.db"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_applies_once_and_records_checksum(self) -> None:
        migration = SchemaMigration(
            migration_id="001.example",
            statements=("CREATE TABLE example (id TEXT PRIMARY KEY)",),
        )
        with sqlite3.connect(self.db_path) as conn:
            first = apply_schema_migrations(conn, (migration,))
            second = apply_schema_migrations(conn, (migration,))
            row = conn.execute(
                "SELECT checksum FROM schema_migrations WHERE migration_id = ?",
                (migration.migration_id,),
            ).fetchone()

        self.assertEqual(first, (migration.migration_id,))
        self.assertEqual(second, ())
        self.assertEqual(row[0], migration.checksum)

    def test_rejects_checksum_drift(self) -> None:
        original = SchemaMigration("001.example", ("CREATE TABLE example (id TEXT)",))
        changed = SchemaMigration("001.example", ("CREATE TABLE example (id INTEGER)",))
        with sqlite3.connect(self.db_path) as conn:
            apply_schema_migrations(conn, (original,))
            with self.assertRaisesRegex(SchemaMigrationError, "checksum changed"):
                apply_schema_migrations(conn, (changed,))

    def test_failed_migration_rolls_back_schema_and_history(self) -> None:
        migration = SchemaMigration(
            "001.broken",
            (
                "CREATE TABLE should_rollback (id TEXT)",
                "CREATE TABLE should_rollback (id TEXT)",
            ),
        )
        with sqlite3.connect(self.db_path) as conn:
            with self.assertRaises(sqlite3.OperationalError):
                apply_schema_migrations(conn, (migration,))
            table = conn.execute(
                "SELECT name FROM sqlite_master WHERE name = 'should_rollback'"
            ).fetchone()
            history = conn.execute(
                "SELECT migration_id FROM schema_migrations WHERE migration_id = ?",
                (migration.migration_id,),
            ).fetchone()

        self.assertIsNone(table)
        self.assertIsNone(history)


if __name__ == "__main__":
    unittest.main()

