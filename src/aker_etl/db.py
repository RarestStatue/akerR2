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


def init_db(settings: Settings) -> list[str]:
    """Run every migration. Each file is idempotent, so this is safe to repeat."""
    applied: list[str] = []
    with connect(settings, autocommit=True) as conn:
        for path in migration_files():
            sql = path.read_text(encoding="utf-8")
            log.info("applying %s", path.name)
            with conn.cursor() as cur:
                cur.execute(sql)  # type: ignore[arg-type]
            applied.append(path.name)
    return applied


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
