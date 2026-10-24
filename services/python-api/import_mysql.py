#!/usr/bin/env python3
"""Copy the AshatHub API tables from MySQL into a new SQLite file."""
import argparse
from datetime import date, datetime, time as datetime_time
from decimal import Decimal
import os
from pathlib import Path
import sys
from urllib.parse import unquote, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import migrate
from config import CONFIG
from database import DEFAULT_PATH, db

# These belong to legacy or separately deployed services, not the current portal API.
NON_PORTAL_TABLES = {
    "_sqlx_migrations", "agent_skills", "api_configs", "brainstem_config", "docs_articles",
    "files", "google_accounts", "icarus_devices", "oauth_authorization_codes", "oauth_clients",
    "page_visits", "password_resets", "skills", "vesper_model_seeds", "vesper_models",
    "vesper_releases", "vesper_sessions",
}


def mysql_connect(url):
    try:
        import pymysql
        from pymysql.cursors import SSDictCursor
    except ModuleNotFoundError as exc:
        raise RuntimeError("Install PyMySQL temporarily to import the existing database") from exc
    parsed = urlparse(url)
    if parsed.scheme not in ("mysql", "mysql+pymysql") or not parsed.hostname or not parsed.path:
        raise ValueError("source URL must be a MySQL connection URL")
    return pymysql.connect(host=parsed.hostname, port=parsed.port or 3306,
                           user=unquote(parsed.username or ""), password=unquote(parsed.password or ""),
                           database=unquote(parsed.path.lstrip("/")), cursorclass=SSDictCursor,
                           charset="utf8mb4", autocommit=False)


def sqlite_value(value):
    if isinstance(value, (datetime, date, datetime_time)):
        return value.isoformat(sep=" ") if isinstance(value, datetime) else value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, memoryview):
        return value.tobytes()
    return value


def quote(name):
    return '"' + name.replace('"', '""') + '"'


def import_database(source_url, destination):
    destination = Path(destination).expanduser().resolve()
    temporary = destination.with_name(destination.name + ".importing")
    if destination.exists() or temporary.exists():
        raise FileExistsError("destination or import staging file already exists; refusing to overwrite data")
    destination.parent.mkdir(parents=True, exist_ok=True)

    source = mysql_connect(source_url)
    try:
        with source.cursor() as cur:
            cur.execute("START TRANSACTION WITH CONSISTENT SNAPSHOT")
            cur.execute("SHOW TABLES")
            source_tables = {next(iter(row.values())) for row in cur.fetchall()}

        with db(temporary) as target:
            with target.cursor() as cur:
                migrate.ensure_tracking_table(cur)
            for migration in migrate.discover():
                migrate.apply_migration(target, migration, "mysql-import")
            target.execute("PRAGMA foreign_keys = OFF")
            target.execute("BEGIN IMMEDIATE")
            counts = {}
            target_tables = {row["name"] for row in target.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()} - {migrate.TRACKING_TABLE}
            ignored_tables = source_tables & NON_PORTAL_TABLES
            unmapped_tables = source_tables - target_tables - NON_PORTAL_TABLES - {migrate.TRACKING_TABLE}
            if unmapped_tables:
                raise ValueError(f"unmapped MySQL tables: {', '.join(sorted(unmapped_tables))}")
            for table in sorted(target_tables & source_tables):
                with source.cursor() as cur:
                    cur.execute(f"SELECT * FROM `{table.replace('`', '``')}`")
                    columns = [item[0] for item in cur.description]
                    target_columns = [row["name"] for row in target.execute(
                        f"PRAGMA table_info({quote(table)})").fetchall()]
                    extra = set(columns) - set(target_columns)
                    if extra:
                        raise ValueError(f"{table} has unmapped columns: {', '.join(sorted(extra))}")
                    if not columns:
                        continue
                    target.execute(f"DELETE FROM {quote(table)}")
                    names = ",".join(quote(column) for column in columns)
                    marks = ",".join("?" for _ in columns)
                    insert = f"INSERT INTO {quote(table)} ({names}) VALUES ({marks})"
                    count = 0
                    while rows := cur.fetchmany(500):
                        target.executemany(insert, [tuple(sqlite_value(row[column]) for column in columns)
                                                     for row in rows])
                        count += len(rows)
                    counts[table] = count
            target.commit()
            target.execute("PRAGMA foreign_keys = ON")
            violations = target.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise ValueError(f"SQLite foreign key check failed: {violations[:5]}")
    except Exception:
        for suffix in ("", "-wal", "-shm", "-journal"):
            Path(str(temporary) + suffix).unlink(missing_ok=True)
        raise
    finally:
        source.close()

    os.replace(temporary, destination)
    for table, count in sorted(counts.items()):
        print(f"{table}: {count} rows")
    if ignored_tables:
        print("left in MySQL (outside current API schema): " + ", ".join(sorted(ignored_tables)))
    print(f"SQLite database created at {destination}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sqlite-path", default=str(DEFAULT_PATH), help="destination file (defaults to config.json)")
    args = parser.parse_args()
    if not CONFIG["mysql_source_url"]:
        parser.error("set mysql_source_url in config.json")
    import_database(CONFIG["mysql_source_url"], args.sqlite_path)


if __name__ == "__main__":
    main()
