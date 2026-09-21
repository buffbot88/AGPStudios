#!/usr/bin/env python3
"""Unit tests for migrate.py — no database or pymysql required.

Covers the parts that are easy to get subtly wrong and expensive to get wrong
in production: statement splitting (quotes, comments, embedded semicolons),
discovery/ordering rules, the applied/pending/drifted classification, and the
"statements first, tracking row last" failure semantics.

Run: python3 services/python-api/test_migrate.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import migrate  # noqa: E402

failures: list[str] = []
passed = 0


def check(condition: bool, label: str, detail: str = "") -> None:
    global passed
    if condition:
        passed += 1
    else:
        failures.append(f"{label}{f' — {detail}' if detail else ''}")
        print(f"  FAIL: {label}{f' — {detail}' if detail else ''}")


# ---- statement splitting ---------------------------------------------------

def test_split() -> None:
    sql = """
    -- leading comment
    CREATE TABLE t (a INT); -- trailing comment
    INSERT INTO t VALUES ('a;b', "c;d");
    /* block; comment */
    UPDATE t SET a = 1 WHERE a < 2;
    """
    statements = migrate.split_statements(sql)
    check(len(statements) == 3, "split: three statements", repr(statements))
    check(statements[0].startswith("CREATE TABLE t"), "split: first statement", repr(statements[0]))
    check("'a;b'" in statements[1] and '"c;d"' in statements[1],
          "split: semicolons inside quotes are preserved", repr(statements[1]))
    check("comment" not in " ".join(statements), "split: comments removed")

    # trailing statement without a semicolon
    check(migrate.split_statements("SELECT 1") == ["SELECT 1"], "split: no trailing semicolon")
    # escaped and doubled quotes must not end the string early
    check(len(migrate.split_statements(r"INSERT INTO t VALUES ('it\'s; fine');")) == 1,
          "split: backslash-escaped quote")
    check(len(migrate.split_statements("INSERT INTO t VALUES ('it''s; fine');")) == 1,
          "split: doubled-quote escape")
    # MySQL treats # as a comment
    check(len(migrate.split_statements("SELECT 1; # trailing\nSELECT 2;")) == 2, "split: hash comment")
    # empty input / semicolon-only input
    check(migrate.split_statements("") == [], "split: empty input")
    check(migrate.split_statements(";;;") == [], "split: semicolons only")


# ---- checksums -------------------------------------------------------------

def test_checksum() -> None:
    check(migrate.checksum("abc") == migrate.checksum("abc"), "checksum: stable")
    check(migrate.checksum("abc") != migrate.checksum("abcd"), "checksum: content sensitive")
    check(len(migrate.checksum("abc")) == 64, "checksum: sha256 hex length")


# ---- discovery -------------------------------------------------------------

def write(directory: Path, name: str, body: str = "SELECT 1;") -> Path:
    path = directory / name
    path.write_text(body, encoding="utf-8")
    return path


def test_discover() -> None:
    with tempfile.TemporaryDirectory() as raw:
        directory = Path(raw)
        write(directory, "0002_second.sql")
        write(directory, "0001_first.sql")
        write(directory, "0010_tenth.sql")
        write(directory, "README.md", "not sql")

        names = [migration.name for migration in migrate.discover(directory)]
        check(names == ["0001_first.sql", "0002_second.sql", "0010_tenth.sql"],
              "discover: numeric filename order", repr(names))

        # gaps are allowed (0010 after 0002) — only ordering matters
        check([m.prefix for m in migrate.discover(directory)] == [1, 2, 10], "discover: prefixes parsed")

    with tempfile.TemporaryDirectory() as raw:
        directory = Path(raw)
        write(directory, "init.sql")
        try:
            migrate.discover(directory)
            check(False, "discover: rejects names without NNNN_ prefix")
        except ValueError:
            check(True, "discover: rejects names without NNNN_ prefix")

    with tempfile.TemporaryDirectory() as raw:
        directory = Path(raw)
        write(directory, "0001_a.sql")
        write(directory, "0001_b.sql")
        try:
            migrate.discover(directory)
            check(False, "discover: rejects duplicate prefixes")
        except ValueError:
            check(True, "discover: rejects duplicate prefixes")


# ---- planning --------------------------------------------------------------

def test_plan() -> None:
    with tempfile.TemporaryDirectory() as raw:
        directory = Path(raw)
        one = write(directory, "0001_one.sql", "SELECT 1;")
        two = write(directory, "0002_two.sql", "SELECT 2;")
        three = write(directory, "0003_three.sql", "SELECT 3;")
        migrations = migrate.discover(directory)
        by_name = {m.name: m for m in migrations}

        # nothing applied yet -> everything pending, in order
        result = migrate.plan(migrations, {})
        check([m.name for m in result.pending] == ["0001_one.sql", "0002_two.sql", "0003_three.sql"],
              "plan: all pending in order")
        check(not result.drifted and not result.missing, "plan: no drift or missing on a fresh DB")
        check(not result.is_clean, "plan: pending means not clean")

        # 0001 + 0002 applied with matching checksums -> only 0003 pending
        applied = {
            by_name["0001_one.sql"].name: by_name["0001_one.sql"].checksum,
            by_name["0002_two.sql"].name: by_name["0002_two.sql"].checksum,
        }
        result = migrate.plan(migrations, applied)
        check([m.name for m in result.pending] == ["0003_three.sql"], "plan: applied files skipped")
        check([m.name for m in result.applied] == ["0001_one.sql", "0002_two.sql"], "plan: applied list populated")

        # an applied file whose contents changed -> drifted, NOT pending
        one.write_text("SELECT 1, 2;", encoding="utf-8")
        changed = migrate.discover(directory)
        result = migrate.plan(changed, applied)
        check([m.name for m in result.drifted] == ["0001_one.sql"], "plan: drift detected")
        check([m.name for m in result.pending] == ["0003_three.sql"], "plan: drifted files are not pending")
        check(not result.changed, "plan: drift skipped for re-apply by default")

        result = migrate.plan(changed, applied, reapply_drifted=True)
        check([m.name for m in result.changed] == ["0001_one.sql"], "plan: drift re-applied when asked")

        # a recorded file that no longer exists on disk
        result = migrate.plan(migrations, {**applied, "0099_gone.sql": "deadbeef"})
        check(result.missing == ["0099_gone.sql"], "plan: missing recorded file reported", repr(result.missing))

        check(migrate.plan(migrations, {m.name: m.checksum for m in migrations}).is_clean,
              "plan: clean when everything applied at current checksums")
        del two, three


# ---- apply / record semantics ---------------------------------------------

class FakeCursor:
    """Records executed SQL and replays canned tracking-table rows."""

    def __init__(self, log: list[str], rows: list[dict]):
        self.log = log
        self.rows = rows
        self.fail_on: str | None = None

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def execute(self, sql, params=None):
        self.log.append(sql)
        if self.fail_on and self.fail_on in sql:
            raise RuntimeError("boom")

    def fetchall(self):
        return self.rows


class FakeConnection:
    def __init__(self, rows: list[dict] | None = None):
        self.log: list[str] = []
        self.rows = rows or []
        self.fail_on: str | None = None

    def cursor(self):
        cursor = FakeCursor(self.log, self.rows)
        cursor.fail_on = self.fail_on
        return cursor

    def close(self):
        pass


def tracking_inserts(log: list[str]) -> list[str]:
    return [sql for sql in log if f"INSERT INTO {migrate.TRACKING_TABLE}" in sql]


def test_apply() -> None:
    with tempfile.TemporaryDirectory() as raw:
        directory = Path(raw)
        write(directory, "0001_one.sql", "CREATE TABLE a (x INT);\nINSERT INTO a VALUES (1);")
        migration = migrate.discover(directory)[0]

        connection = FakeConnection()
        count = migrate.apply_migration(connection, migration, "tester")
        check(count == 2, "apply: returns statement count", str(count))
        inserts = tracking_inserts(connection.log)
        check(len(inserts) == 1, "apply: records exactly one tracking row", repr(inserts))
        check(connection.log[0].startswith("CREATE TABLE a"), "apply: first statement executed first")
        check(connection.log[-1] is inserts[0], "apply: tracking row written last (after success)")

        # a failing statement must leave the migration unrecorded
        connection = FakeConnection()
        connection.fail_on = "INSERT INTO a"
        try:
            migrate.apply_migration(connection, migration, "tester")
            check(False, "apply: error propagates")
        except RuntimeError:
            check(True, "apply: error propagates")
        check(tracking_inserts(connection.log) == [],
              "apply: failed migration is not recorded (will be retried)")
        check(len(connection.log) == 2, "apply: stopped at the failing statement", repr(connection.log))


def test_tracking_table_io() -> None:
    connection = FakeConnection(rows=[{"filename": "0001_one.sql", "checksum": "abc"}])
    with connection.cursor() as cursor:
        migrate.ensure_tracking_table(cursor)
        applied = migrate.load_applied(cursor)
    check(applied == {"0001_one.sql": "abc"}, "tracking: load_applied maps filename to checksum", repr(applied))
    check(any("CREATE TABLE IF NOT EXISTS schema_migrations" in sql for sql in connection.log),
          "tracking: table creation is IF NOT EXISTS (safe to re-run)")

    connection = FakeConnection()
    with tempfile.TemporaryDirectory() as raw:
        directory = Path(raw)
        write(directory, "0001_one.sql", "SELECT 1;")
        migration = migrate.discover(directory)[0]
    with connection.cursor() as cursor:
        migrate.record(cursor, migration, "tester")
    statement = tracking_inserts(connection.log)[0]
    check("ON DUPLICATE KEY UPDATE" in statement, "tracking: record upserts instead of failing on re-record")


# ---- the real migration set ------------------------------------------------

def test_real_migrations_parse() -> None:
    migrations = migrate.discover()
    check(len(migrations) >= 4, "real: migrations discovered", str(len(migrations)))
    check([m.name for m in migrations] == sorted(m.name for m in migrations),
          "real: files sort into application order")

    expected = {
        "0001_diamonds.sql": 4,  # SET guard, PREPARE, EXECUTE, DEALLOCATE
        "0002_homepage_content.sql": 4,
        "0003_rename_project_horizon.sql": 5,
        "0004_user_moderation_history.sql": 1,
    }
    for migration in migrations:
        statements = migration.statements
        check(len(statements) > 0, f"real: {migration.name} has statements")
        check(all(statement.strip() for statement in statements), f"real: {migration.name} has no empty statements")
        if migration.name in expected:
            check(len(statements) == expected[migration.name],
                  f"real: {migration.name} splits into {expected[migration.name]} statements",
                  f"got {len(statements)}: {statements}")

    # 0001 predates the tracking table, so it must be re-runnable on its own —
    # the MySQL CI job (test_migrations_mysql.py) applies every file twice.
    diamonds = next(m for m in migrations if m.name == "0001_diamonds.sql")
    check(any("information_schema.columns" in statement for statement in diamonds.statements),
          "real: 0001 guards ADD COLUMN against an existing column (re-runnable)")


# ---- CLI orchestration (fake database) -------------------------------------

class FakeDatabase:
    """Minimal stand-in for a pymysql connection: keeps the `schema_migrations`
    rows in memory so multi-command flows behave like a real database."""

    def __init__(self):
        self.log: list[str] = []
        self.rows: list[dict] = []
        self.fail_on: str | None = None

    def cursor(self):
        database = self

        class Cursor:
            def __enter__(self):
                return self

            def __exit__(self, *exc_info):
                return False

            def execute(self, sql, params=None):
                database.log.append(sql)
                if database.fail_on and database.fail_on in sql:
                    raise RuntimeError("boom")
                if f"INSERT INTO {migrate.TRACKING_TABLE}" in sql and params:
                    filename, digest, statements, applied_at, applied_by = params
                    database.rows = [row for row in database.rows if row["filename"] != filename]
                    database.rows.append({"filename": filename, "checksum": digest,
                                          "statements": statements, "applied_at": applied_at,
                                          "applied_by": applied_by})

            def fetchall(self):
                return list(database.rows)

        return Cursor()

    def close(self):
        pass

    def migration_statements(self) -> list[str]:
        """SQL from the migration files only (not tracking-table bookkeeping)."""
        return [sql for sql in self.log if migrate.TRACKING_TABLE not in sql]


def with_fake_database(directory: Path, function) -> FakeDatabase:
    database = FakeDatabase()
    original = migrate.connect
    migrate.connect = lambda _url: database
    try:
        function(database, str(directory))
    finally:
        migrate.connect = original
    return database


def test_cli() -> None:
    with tempfile.TemporaryDirectory() as raw:
        directory = Path(raw)
        write(directory, "0001_one.sql", "CREATE TABLE a (x INT);")
        write(directory, "0002_two.sql", "CREATE TABLE b (y INT);")
        url = "mysql://u:p@127.0.0.1:3306/db"

        def status_then_apply(database: FakeDatabase, path: str) -> None:
            # 1) --status and --check on a fresh database
            check(migrate.main(["--status", "--dir", path, "--database-url", url]) == 0,
                  "cli: --status succeeds")
            check(migrate.main(["--check", "--dir", path, "--database-url", url]) == 1,
                  "cli: --check exits 1 while migrations are pending")
            check(database.migration_statements() == [], "cli: --status and --check execute no DDL")
            check(database.rows == [], "cli: --status records nothing")

            # 2) --dry-run plans but does not apply
            check(migrate.main(["--dry-run", "--dir", path, "--database-url", url]) == 0,
                  "cli: --dry-run succeeds")
            check(database.migration_statements() == [], "cli: --dry-run executes no DDL")

            # 3) apply everything, in order
            check(migrate.main(["--dir", path, "--database-url", url]) == 0, "cli: apply succeeds")
            check([row["filename"] for row in database.rows] == ["0001_one.sql", "0002_two.sql"],
                  "cli: both migrations recorded in order", repr(database.rows))
            statements = database.migration_statements()
            check(statements == ["CREATE TABLE a (x INT)", "CREATE TABLE b (y INT)"],
                  "cli: statements executed in filename order", repr(statements))

            # 4) idempotent re-run: nothing to apply, nothing re-executed
            before = len(database.log)
            check(migrate.main(["--dir", path, "--database-url", url]) == 0, "cli: re-run succeeds")
            check(database.migration_statements() == statements, "cli: re-run executes no migration SQL")
            check(migrate.main(["--check", "--dir", path, "--database-url", url]) == 0,
                  "cli: --check exits 0 once applied")
            check(len(database.log) > before, "cli: re-run still reads the tracking table")

        with_fake_database(directory, status_then_apply)

    # baseline: record without running (the hand-migrated database case)
    with tempfile.TemporaryDirectory() as raw:
        directory = Path(raw)
        write(directory, "0001_diamonds.sql", "ALTER TABLE users ADD COLUMN diamonds BIGINT UNSIGNED NOT NULL DEFAULT 0;")
        write(directory, "0002_two.sql", "CREATE TABLE b (y INT);")

        def baseline(database: FakeDatabase, path: str) -> None:
            code = migrate.main(["--baseline", "--dir", path, "--database-url", "mysql://u:p@h/db"])
            check(code == 0, "cli: --baseline succeeds")
            check(database.migration_statements() == [],
                  "cli: --baseline runs no migration SQL at all")
            check(sorted(row["filename"] for row in database.rows) == ["0001_diamonds.sql", "0002_two.sql"],
                  "cli: --baseline records every file", repr(database.rows))
            check(migrate.main(["--check", "--dir", path, "--database-url", "mysql://u:p@h/db"]) == 0,
                  "cli: --check passes after baseline")

        with_fake_database(directory, baseline)

    # drift: an applied file edited afterwards
    with tempfile.TemporaryDirectory() as raw:
        directory = Path(raw)
        path_written = write(directory, "0001_one.sql", "CREATE TABLE a (x INT);")

        def drift(database: FakeDatabase, path: str) -> None:
            url = "mysql://u:p@h/db"
            check(migrate.main(["--dir", path, "--database-url", url]) == 0, "cli: initial apply")
            path_written.write_text("CREATE TABLE a (x INT, z INT);", encoding="utf-8")
            executed_before = list(database.migration_statements())

            check(migrate.main(["--dir", path, "--database-url", url]) == 0, "cli: drifted run succeeds")
            check(database.migration_statements() == executed_before,
                  "cli: drifted migration is NOT re-run without --reapply-drifted")

            check(migrate.main(["--reapply-drifted", "--dir", path, "--database-url", url]) == 0,
                  "cli: --reapply-drifted succeeds")
            check(database.migration_statements()[-1].startswith("CREATE TABLE a (x INT, z INT)"),
                  "cli: --reapply-drifted re-executes the changed file")
            check(database.rows[0]["checksum"] == migrate.checksum(path_written.read_text(encoding="utf-8")),
                  "cli: --reapply-drifted re-records the new checksum")

        with_fake_database(directory, drift)

    # failure: the file must stay unrecorded so the next run retries it
    with tempfile.TemporaryDirectory() as raw:
        directory = Path(raw)
        write(directory, "0001_one.sql", "CREATE TABLE a (x INT);")

        def failure(database: FakeDatabase, path: str) -> None:
            database.fail_on = "CREATE TABLE a"
            code = migrate.main(["--dir", path, "--database-url", "mysql://u:p@h/db"])
            check(code == 1, "cli: a failing migration exits 1", str(code))
            check(database.rows == [], "cli: a failing migration is not recorded")

        with_fake_database(directory, failure)


def main() -> int:
    test_split()
    test_checksum()
    test_discover()
    test_plan()
    test_apply()
    test_tracking_table_io()
    test_real_migrations_parse()
    test_cli()

    print()
    if failures:
        print(f"migrate unit tests: {len(failures)} FAILED, {passed} passed")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print(f"migrate unit tests: PASS ({passed} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
