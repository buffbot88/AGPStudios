#!/usr/bin/env python3
"""Check the one-time import path with a small MySQL cursor stand-in."""
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import import_mysql
from database import db


class SourceCursor:
    description = []

    def __init__(self):
        self.rows = []

    def __enter__(self): return self
    def __exit__(self, *_): pass

    def execute(self, sql):
        if sql == "SHOW TABLES":
            self.rows = [{"Tables_in_test": "users"}]
        else:
            self.description = [(name,) for name in (
                "id", "username", "email", "password_hash", "display_name", "role", "is_active")]
            self.rows = [{"id": "u1", "username": "member", "email": "m@example.test",
                          "password_hash": "hash", "display_name": "Member", "role": "Member", "is_active": 1}]

    def fetchall(self): return self.rows
    def fetchmany(self, _):
        rows, self.rows = self.rows, []
        return rows


class Source:
    def cursor(self): return SourceCursor()
    def close(self): pass


def main():
    with tempfile.TemporaryDirectory() as raw:
        target = Path(raw) / "import.sqlite3"
        with patch.object(import_mysql, "mysql_connect", return_value=Source()):
            import_mysql.import_database("unused", target)
        with db(target) as conn:
            row = conn.execute("SELECT username,email FROM users WHERE id='u1'").fetchone()
            assert row == {"username": "member", "email": "m@example.test"}
    print("MySQL-to-SQLite import path: PASS")


if __name__ == "__main__":
    main()
