#!/usr/bin/env python3
"""Exercise the SQLite schema and migration runner against a disposable file."""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import migrate
from database import db


def main():
    with tempfile.TemporaryDirectory() as raw:
        database = Path(raw) / "ashat.sqlite3"
        migrations = migrate.discover()
        with db(database) as conn:
            with conn.cursor() as cur:
                migrate.ensure_tracking_table(cur)
            for migration in migrations:
                migrate.apply_migration(conn, migration, "test")
            applied = migrate.load_applied(conn.cursor())
            assert len(applied) == len(migrations) and not migrate.plan(migrations, applied).pending
            assert conn.execute("SELECT COUNT(*) AS n FROM games").fetchone()["n"] == 2
            assert conn.execute("SELECT COUNT(*) AS n FROM studio_news").fetchone()["n"] == 1
            conn.execute("INSERT INTO users(id,username,email,password_hash) VALUES(?,?,?,?)",
                         ("u1", "member", "member@example.test", "hash"))
            conn.execute("INSERT INTO world_of_ra_entitlements(user_id,granted_at,granted_by) VALUES(?,?,?)",
                         ("u1", 1, "admin"))
            assert conn.execute("SELECT user_id FROM world_of_ra_entitlements").fetchone()["user_id"] == "u1"
            conn.execute("INSERT INTO everenfell_s1_entitlements(user_id,granted_at,granted_by) VALUES(?,?,?)",
                         ("u1", 1, "admin"))
            assert conn.execute("SELECT user_id FROM everenfell_s1_entitlements").fetchone()["user_id"] == "u1"
            assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        with db(database) as conn:
            assert conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"] == 1
    print("SQLite schema, persistence, and foreign keys: PASS")


if __name__ == "__main__":
    main()
