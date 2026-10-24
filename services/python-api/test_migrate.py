#!/usr/bin/env python3
"""Focused checks for SQLite migration parsing, planning, and rollback."""
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import migrate
from database import db


def main():
    assert migrate.split_statements("SELECT ';'; -- ignored\nSELECT 2;") == ["SELECT ';'", "SELECT 2"]
    assert migrate.checksum("schema") == migrate.checksum("schema")
    with tempfile.TemporaryDirectory() as raw:
        folder = Path(raw)
        one = folder / "0001_one.sql"
        one.write_text("CREATE TABLE sample (id INTEGER PRIMARY KEY); INSERT INTO sample VALUES (1);")
        migrations = migrate.discover(folder)
        assert [item.name for item in migrations] == ["0001_one.sql"]
        assert len(migrate.plan(migrations, {}).pending) == 1
        path = Path(raw) / "test.sqlite3"
        with db(path) as conn:
            with conn.cursor() as cur:
                migrate.ensure_tracking_table(cur)
            migration = migrations[0]
            assert migrate.apply_migration(conn, migration, "test") == 2
            assert migrate.plan(migrations, migrate.load_applied(conn.cursor())).is_clean
            assert conn.execute("SELECT COUNT(*) AS n FROM sample").fetchone()["n"] == 1

        # A failed migration rolls back both DDL and its tracking record.
        failed = folder / "0002_fail.sql"
        failed.write_text("CREATE TABLE rolled_back (id INTEGER); INSERT INTO missing VALUES (1);")
        with db(path) as conn:
            migration = migrate.discover(folder)[1]
            try:
                migrate.apply_migration(conn, migration, "test")
                failed = False
            except sqlite3.OperationalError:
                failed = True
            assert failed
            assert conn.execute("SELECT name FROM sqlite_master WHERE name='rolled_back'").fetchone() is None
            assert "0002_fail.sql" not in migrate.load_applied(conn.cursor())
    print("SQLite migration planning and rollback: PASS")


if __name__ == "__main__":
    main()
