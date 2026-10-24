#!/usr/bin/env python3
"""Apply SQL migrations in filename order, recording each in `schema_migrations`.

Usage and failure semantics are in the README under "Database migrations".
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import socket
import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence
MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
TRACKING_TABLE = "schema_migrations"
sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import CONFIG
from database import connect as sqlite_connect

CREATE_TRACKING_TABLE = f"""
CREATE TABLE IF NOT EXISTS {TRACKING_TABLE} (
  filename TEXT NOT NULL PRIMARY KEY,
  checksum TEXT NOT NULL,
  statements INTEGER NOT NULL DEFAULT 0,
  applied_at INTEGER NOT NULL,
  applied_by TEXT NOT NULL DEFAULT ''
)
"""


# --------------------------------------------------------------------------
# Pure helpers (unit-tested without a database in test_migrate.py)
# --------------------------------------------------------------------------

def split_statements(sql: str) -> list[str]:
    """Split a migration file into statements, respecting quotes and comments.

    A naive split on ';' breaks on semicolons inside string literals.
    """
    statements: list[str] = []
    buffer: list[str] = []
    quote: str | None = None
    index, length = 0, len(sql)

    while index < length:
        char = sql[index]

        if quote:
            buffer.append(char)
            if char == "\\" and quote in ("'", '"') and index + 1 < length:
                buffer.append(sql[index + 1])
                index += 2
                continue
            if char == quote:
                if index + 1 < length and sql[index + 1] == quote:
                    buffer.append(sql[index + 1])
                    index += 2
                    continue
                quote = None
            index += 1
            continue

        if char in ("'", '"', "`"):
            quote = char
            buffer.append(char)
            index += 1
            continue

        if sql.startswith("--", index) or char == "#":
            newline = sql.find("\n", index)
            index = length if newline == -1 else newline + 1
            continue

        if sql.startswith("/*", index):
            end = sql.find("*/", index + 2)
            index = length if end == -1 else end + 2
            continue

        if char == ";":
            statements.append("".join(buffer).strip())
            buffer = []
            index += 1
            continue

        buffer.append(char)
        index += 1

    tail = "".join(buffer).strip()
    if tail:
        statements.append(tail)
    return [statement for statement in statements if statement]


def checksum(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Migration:
    path: Path
    name: str
    prefix: int
    sql: str

    @property
    def checksum(self) -> str:
        return checksum(self.sql)

    @property
    def statements(self) -> list[str]:
        return split_statements(self.sql)


def discover(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    """Return migrations ordered by numeric filename prefix; a mis-named .sql file is an error."""
    migrations: list[Migration] = []
    seen: dict[int, str] = {}
    for path in sorted(directory.glob("*.sql")):
        stem = path.stem
        prefix_text, separator, _ = stem.partition("_")
        if not separator or not prefix_text.isdigit():
            raise ValueError(f"migration {path.name!r} must be named NNNN_description.sql")
        prefix = int(prefix_text)
        if prefix in seen:
            raise ValueError(f"migrations {seen[prefix]!r} and {path.name!r} share prefix {prefix_text}")
        seen[prefix] = path.name
        migrations.append(Migration(path=path, name=path.name, prefix=prefix, sql=path.read_text(encoding="utf-8")))
    migrations.sort(key=lambda migration: (migration.prefix, migration.name))
    return migrations


@dataclass
class Plan:
    applied: list[Migration]
    pending: list[Migration]
    drifted: list[Migration]
    missing: list[str]
    changed: list[Migration]  # only populated when re-applying drifted files

    @property
    def is_clean(self) -> bool:
        return not self.pending and not self.drifted and not self.missing


def plan(migrations: Sequence[Migration], applied: dict[str, str], reapply_drifted: bool = False) -> Plan:
    """Classify migrations against the recorded `filename -> checksum` map."""
    applied_migrations: list[Migration] = []
    pending: list[Migration] = []
    drifted: list[Migration] = []
    changed: list[Migration] = []

    for migration in migrations:
        recorded = applied.get(migration.name)
        if recorded is None:
            pending.append(migration)
        elif recorded != migration.checksum:
            drifted.append(migration)
            if reapply_drifted:
                changed.append(migration)
        else:
            applied_migrations.append(migration)

    known = {migration.name for migration in migrations}
    missing = sorted(name for name in applied if name not in known)
    return Plan(applied=applied_migrations, pending=pending, drifted=drifted, missing=missing, changed=changed)


# --------------------------------------------------------------------------
# Database access
# --------------------------------------------------------------------------

def connect(database_path: str = ""):
    """Open the local SQLite database without a network driver."""
    return sqlite_connect(database_path or None)


def ensure_tracking_table(cursor) -> None:
    cursor.execute(CREATE_TRACKING_TABLE)


def load_applied(cursor) -> dict[str, str]:
    cursor.execute(f"SELECT filename, checksum FROM {TRACKING_TABLE}")
    return {row["filename"]: row["checksum"] for row in cursor.fetchall()}


def record(cursor, migration: Migration, applied_by: str) -> None:
    cursor.execute(
        f"INSERT INTO {TRACKING_TABLE} (filename, checksum, statements, applied_at, applied_by) "
        "VALUES (?, ?, ?, ?, ?) ON CONFLICT(filename) DO UPDATE SET "
        "checksum=excluded.checksum, statements=excluded.statements, "
        "applied_at=excluded.applied_at, applied_by=excluded.applied_by",
        (migration.name, migration.checksum, len(migration.statements), int(time.time()), applied_by),
    )


def apply_migration(connection, migration: Migration, applied_by: str) -> int:
    """Execute a migration's statements, recording it only after all of them succeed."""
    statements = migration.statements
    connection.execute("BEGIN IMMEDIATE")
    try:
        with connection.cursor() as cursor:
            for statement in statements:
                cursor.execute(statement)
            record(cursor, migration, applied_by)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return len(statements)


def applied_by_label() -> str:
    return f"{getpass.getuser()}@{socket.gethostname()}"


def render(plan_result: Plan) -> str:
    lines: list[str] = []
    if plan_result.applied:
        lines.append(f"applied  ({len(plan_result.applied)}): " + ", ".join(m.name for m in plan_result.applied))
    if plan_result.pending:
        lines.append(f"pending  ({len(plan_result.pending)}): " + ", ".join(m.name for m in plan_result.pending))
    if plan_result.drifted:
        lines.append(f"drifted  ({len(plan_result.drifted)}): " + ", ".join(m.name for m in plan_result.drifted))
    if plan_result.missing:
        lines.append(f"recorded but no longer on disk ({len(plan_result.missing)}): " + ", ".join(plan_result.missing))
    if not lines:
        lines.append("no migrations found")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Apply AGP Studios Python API SQLite migrations in order.")
    parser.add_argument("--status", action="store_true", help="report status without changing anything")
    parser.add_argument("--dry-run", action="store_true", help="show the plan without executing it")
    parser.add_argument("--check", action="store_true", help="exit 1 when migrations are pending (deploy gate)")
    parser.add_argument("--baseline", action="store_true",
                        help="record the current files as applied WITHOUT running them "
                             "(for a database migrated by hand before this runner existed)")
    parser.add_argument("--reapply-drifted", action="store_true",
                        help="re-run migrations whose file contents changed since they were applied")
    parser.add_argument("--dir", default=str(MIGRATIONS_DIR), help="migrations directory")
    parser.add_argument("--database-path", default=CONFIG["database_path"],
                        help="SQLite file path (defaults to config.json)")
    return parser.parse_args(list(argv) if argv is not None else None)


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):  # keep emoji-bearing migration names safe on Windows consoles
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    try:
        migrations = discover(Path(args.dir))
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    if not migrations:
        print("no migrations found")
        return 0

    try:
        connection = connect(args.database_path)
    except (sqlite3.Error, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    try:
        with connection.cursor() as cursor:
            ensure_tracking_table(cursor)
            applied = load_applied(cursor)
        result = plan(migrations, applied, reapply_drifted=args.reapply_drifted)
        print(render(result))

        # Baseline first: it records state, so it takes precedence over the
        # reporting modes when a caller passes both.
        if args.baseline:
            label = applied_by_label()
            baseline = result.pending + result.drifted
            with connection.cursor() as cursor:
                for migration in baseline:
                    record(cursor, migration, label)
            print(f"baseline: recorded {len(baseline)} migration(s) as applied without running them")
            return 0

        # --status/--dry-run/--check are all side-effect free: --check is a
        # deploy gate, so it must never apply the migrations it is gating on.
        if args.status or args.dry_run or args.check:
            if args.check:
                return 1 if result.pending else 0
            return 0

        if result.missing:
            print(f"warning: {len(result.missing)} recorded migration(s) are gone from disk: "
                  + ", ".join(result.missing), file=sys.stderr)
        if result.drifted and not args.reapply_drifted:
            print(f"warning: {len(result.drifted)} applied migration(s) changed on disk; not re-running "
                  "(pass --reapply-drifted to converge): "
                  + ", ".join(m.name for m in result.drifted), file=sys.stderr)

        todo = result.pending + result.changed
        if not todo:
            print("nothing to apply")
            if args.check:
                return 0
            return 0

        label = applied_by_label()
        for migration in todo:
            try:
                count = apply_migration(connection, migration, label)
            except Exception as error:  # noqa: BLE001 - surface the statement and stop
                print(f"FAILED {migration.name}: {error}", file=sys.stderr)
                print("The file is not recorded; fix it and re-run (migrations are convergent).", file=sys.stderr)
                return 1
            print(f"applied {migration.name} ({count} statement(s))")

        if args.reapply_drifted:
            with connection.cursor() as cursor:
                for migration in result.drifted:
                    record(cursor, migration, label)
            if result.drifted:
                print("re-recorded drifted checksums: " + ", ".join(m.name for m in result.drifted))
        return 0
    finally:
        connection.close()


if __name__ == "__main__":
    sys.exit(main())
