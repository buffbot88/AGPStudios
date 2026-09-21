#!/usr/bin/env python3
"""Apply every migration to a real MySQL database twice and assert idempotency.

Skips (exit 0) when ASHAT_DATABASE_URL is unset, so it is safe in a plain
checkout; CI provides a throwaway MySQL 8 service.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import migrate  # noqa: E402

# Minimal stand-in for the production schema that predates this repo's
# migrations: 0001 alters `users`, which no migration creates.
BASE_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id VARCHAR(64) NOT NULL PRIMARY KEY,
  username VARCHAR(80) NOT NULL,
  email VARCHAR(200) NOT NULL,
  password_hash VARCHAR(200) NOT NULL DEFAULT '',
  display_name VARCHAR(80) NOT NULL DEFAULT '',
  role VARCHAR(20) NOT NULL DEFAULT 'Member',
  is_active TINYINT(1) NOT NULL DEFAULT 1,
  email_verified_at BIGINT NULL,
  updated_at TIMESTAMP NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

failures: list[str] = []
passed = 0


def check(condition: bool, label: str, detail: str = "") -> None:
    global passed
    if condition:
        passed += 1
    else:
        failures.append(f"{label}{f' — {detail}' if detail else ''}")
        print(f"  FAIL: {label}{f' — {detail}' if detail else ''}")


def run_statements(cursor, migration) -> None:
    """Execute one migration's statements in order, raising with file context."""
    for index, statement in enumerate(migration.statements, start=1):
        try:
            cursor.execute(statement)
        except Exception as error:  # noqa: BLE001 - report which statement failed
            raise AssertionError(
                f"{migration.name} statement {index}/{len(migration.statements)} failed: {error}\n"
                f"  SQL: {statement[:200]}"
            ) from error


def table_count(cursor, table: str) -> int:
    cursor.execute(f"SELECT COUNT(*) AS n FROM {table}")
    return cursor.fetchone()["n"]


def column_count(cursor, table: str, column: str) -> int:
    cursor.execute(
        "SELECT COUNT(*) AS n FROM information_schema.columns "
        "WHERE table_schema = DATABASE() AND table_name = %s AND column_name = %s",
        (table, column),
    )
    return cursor.fetchone()["n"]


def disposable_database(database_url: str) -> bool:
    """True when the URL's database name looks like a throwaway test database."""
    name = urlparse(database_url).path.lstrip("/").lower()
    return "test" in name or name.endswith("_ci")


def main() -> int:
    database_url = os.environ.get("ASHAT_DATABASE_URL", "").strip()
    if not database_url:
        print("SKIP: ASHAT_DATABASE_URL is not set (this check needs a throwaway MySQL server)")
        return 0
    try:
        import pymysql  # noqa: F401
    except ModuleNotFoundError:
        print("FAILED: ASHAT_DATABASE_URL is set but pymysql is missing (pip install pymysql)")
        return 1
    # This applies every migration (and its seed rows) to whatever the URL
    # points at, so a non-disposable database name is refused unless opted in.
    if not disposable_database(database_url) and not os.environ.get("ASHAT_ALLOW_ANY_DATABASE"):
        print(
            "REFUSING: {!r} does not look like a throwaway database (name must contain "
            "'test'). Set ASHAT_ALLOW_ANY_DATABASE=1 to override.".format(database_url)
        )
        return 1

    migrations = migrate.discover()
    print(f"migration files: {[migration.name for migration in migrations]}")

    try:
        connection = migrate.connect(database_url)
    except Exception as error:  # noqa: BLE001 - bad URL, missing driver, or unreachable server
        print(f"FAILED: cannot connect to MySQL: {error}")
        return 1
    try:
        with connection.cursor() as cursor:
            cursor.execute(BASE_SCHEMA)

            # Pass 1: every file applies cleanly to a database in its prior state.
            for migration in migrations:
                run_statements(cursor, migration)
                check(True, f"pass 1: {migration.name} applied")

            tracked_tables = ["games", "studio_news", "user_moderation_history"]
            before = {table: table_count(cursor, table) for table in tracked_tables}

            # Pass 2: every file again — this is the idempotency assertion.
            for migration in migrations:
                run_statements(cursor, migration)
                check(True, f"pass 2 (re-run): {migration.name} applied again")

            after = {table: table_count(cursor, table) for table in tracked_tables}
            for table in tracked_tables:
                check(before[table] == after[table],
                      f"re-running migrations did not duplicate rows in {table}",
                      f"{before[table]} -> {after[table]}")

            check(column_count(cursor, "users", "diamonds") == 1,
                  "users.diamonds exists exactly once after a re-run")

            cursor.execute("SELECT COUNT(*) AS n FROM games WHERE slug = 'world-of-ra-horizon'")
            check(cursor.fetchone()["n"] == 1, "exactly one world-of-ra-horizon row after a re-run")
            cursor.execute("SELECT COUNT(*) AS n FROM games WHERE slug = 'project-horizon'")
            check(cursor.fetchone()["n"] == 0, "no codename project-horizon row survives (0003 converges)")
            cursor.execute("SELECT COUNT(*) AS n FROM games WHERE slug = 'more-titles'")
            check(cursor.fetchone()["n"] == 1, "exactly one more-titles row after a re-run")
    finally:
        connection.close()

    print()
    if failures:
        print(f"mysql migration idempotency: {len(failures)} FAILED, {passed} passed")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print(f"mysql migration idempotency: PASS ({passed} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
