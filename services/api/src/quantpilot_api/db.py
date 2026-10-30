"""PostgreSQL pool (psycopg3) + row helpers."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Iterator
from uuid import UUID

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from . import config

_pool: ConnectionPool | None = None


def init_pool() -> None:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            config.pg_dsn(),
            min_size=1,
            max_size=8,
            open=True,
            # dev runs PG through an SSH tunnel; verify liveness at checkout
            # so a dropped tunnel connection never reaches a request handler
            check=ConnectionPool.check_connection,
        )


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


@contextmanager
def conn() -> Iterator[Any]:
    if _pool is None:
        init_pool()
    assert _pool is not None
    with _pool.connection() as connection:
        yield connection


def query(sql: str, params: tuple | dict | None = None) -> list[dict[str, Any]]:
    with conn() as connection:
        cur = connection.cursor(row_factory=dict_row)
        cur.execute(sql, params)
        return list(cur.fetchall())


def query_one(sql: str, params: tuple | dict | None = None) -> dict[str, Any] | None:
    rows = query(sql, params)
    return rows[0] if rows else None


def execute(sql: str, params: tuple | dict | None = None) -> None:
    with conn() as connection:
        connection.execute(sql, params)


def jsonable(value: Any) -> Any:
    """Make a DB row JSON-safe: Decimal -> str (money as decimal string, PRD
    13.1), datetime -> ISO, UUID -> str, nested dicts/lists recursively."""
    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    return value
