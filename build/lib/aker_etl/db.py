"""Connection helper and the migration runner."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import psycopg

from .config import REPO_ROOT, Settings

log = logging.getLogger(__name__)

SQL_DIR = REPO_ROOT / "sql"
DROP_DIR = SQL_DIR / "drop"

# hashtext('aker_etl_load') would need a round trip; a fixed key is equivalent and
# self-documenting. One loader at a time, so two concurrent runs cannot interleave.
LOAD_LOCK_KEY = 0x1AE12026


@contextmanager
def connect(settings: Settings, *, autocommit: bool = False) -> Iterator[psycopg.Connection]:
    conn = psycopg.connect(settings.dsn, autocommit=autocommit)
    try:
        yield conn
    finally:
        conn.close()


def scalar(cur: psycopg.Cursor) -> Any:
    """First column of the next row, for queries that must return exactly one.

    `fetchone()` is typed `tuple | None`, so every `RETURNING`/`count(*)` call
    site would otherwise need its own narrowing. A missing row here means the
    statement did not do what it claimed, which is a bug, not a data condition.
    """
    row = cur.fetchone()
    if row is None:
        raise RuntimeError("expected exactly one row, got none")
    return row[0]


def migration_files() -> list[Path]:
    """sql/*.sql in filename order. The numeric prefixes are the dependency order."""
    return sorted(SQL_DIR.glob("*.sql"))


def drop_files() -> list[Path]:
    """sql/drop/*.sql, in filename order.

    A sub-directory on purpose: `migration_files()` globs `sql/*.sql`, which is
    not recursive, so these never run during an ordinary `init-db`.
    """
    return sorted(DROP_DIR.glob("*.sql"))


def init_db(settings: Settings, *, drop: bool = False) -> tuple[list[str], list[str]]:
    """Run every migration. Returns (dropped file names, applied file names).

    Each migration is idempotent, so this is safe to repeat.

    With `drop=True`, `sql/drop/*.sql` runs first and the schemas are rebuilt from
    nothing. That is the *only* way to pick up a change to a CREATE TYPE or a
    CREATE TABLE, because `sql/` deliberately contains no ALTER statements: the 50
    source workbooks are the record, a full reload takes about a second, and a
    schema with no migration history cannot drift from the file that declares it.
    """
    if not SQL_DIR.is_dir():
        raise RuntimeError(
            f"migration directory not found: {SQL_DIR}. aker-etl runs from a checkout of "
            f"the repository; an installed copy carries no sql/ directory."
        )
    dropped: list[str] = []
    applied: list[str] = []
    with connect(settings, autocommit=True) as conn:
        if drop:
            for path in drop_files():
                log.warning("dropping schemas via %s", path.name)
                with conn.cursor() as cur:
                    cur.execute(path.read_text(encoding="utf-8"))  # type: ignore[arg-type]
                dropped.append(path.name)
        for path in migration_files():
            sql = path.read_text(encoding="utf-8")
            log.info("applying %s", path.name)
            with conn.cursor() as cur:
                cur.execute(sql)  # type: ignore[arg-type]
            applied.append(path.name)
    return dropped, applied


def refresh_marts(conn: psycopg.Connection) -> list[str]:
    """Refresh every materialized view in `mart`.

    Must run OUTSIDE a transaction block: REFRESH ... CONCURRENTLY is rejected
    inside one. A view that has never been populated cannot be refreshed
    concurrently either, so the first refresh of each is plain.
    """
    refreshed: list[str] = []
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.relname, c.relispopulated
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'mart' AND c.relkind = 'm'
            ORDER BY c.relname
            """
        )
        views = cur.fetchall()
    for name, populated in views:
        mode = "CONCURRENTLY " if populated else ""
        with conn.cursor() as cur:
            cur.execute(f"REFRESH MATERIALIZED VIEW {mode}mart.{name}")  # noqa: S608 - name from pg_class
        refreshed.append(name)
        log.info("refreshed mart.%s%s", name, " (concurrently)" if populated else "")
    return refreshed
