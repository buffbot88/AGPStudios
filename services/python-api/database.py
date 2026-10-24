"""Small SQLite connection helper shared by the API and migration runner."""
from contextlib import contextmanager
from hashlib import sha256 as _sha256
from pathlib import Path
import os
import sqlite3

from config import CONFIG, path as config_path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PATH = config_path(CONFIG["database_path"])


class DictCursor(sqlite3.Cursor):
    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class Connection(sqlite3.Connection):
    def cursor(self, factory=DictCursor):
        return super().cursor(factory)


def row_dict(cursor, row):
    return {column[0]: row[index] for index, column in enumerate(cursor.description)}


def connect(path=None):
    path = config_path(path or CONFIG["database_path"])
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None, factory=Connection)
    os.chmod(path, 0o600)
    conn.row_factory = row_dict
    conn.create_function("sha256", 1, lambda value: _sha256(str(value).encode()).hexdigest())
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


@contextmanager
def db(path=None):
    conn = connect(path)
    try:
        yield conn
    finally:
        conn.close()
