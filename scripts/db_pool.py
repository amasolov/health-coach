"""Shared DB connection pool for the health database.

Provides a drop-in replacement for ``psycopg2.connect()`` that reuses
connections from a ``ThreadedConnectionPool``.  The pool is created lazily
on first use and sized for the typical add-on workload (2-4 concurrent
Python processes, each doing 1-3 queries in parallel via thread pool).

Usage — identical to the old pattern::

    conn = get_conn()          # borrows from pool
    try:
        cur = conn.cursor()
        cur.execute(...)
    finally:
        conn.close()           # returns to pool (not a real close)

The returned object proxies all psycopg2 connection attributes/methods
so ``conn.autocommit = True``, ``conn.cursor()``, etc. all work
transparently.  ``close()`` rolls back any uncommitted work and returns
the underlying connection to the pool.

A separate ``get_conn_chat()`` is provided for the Chainlit chat DB.
"""

from __future__ import annotations

import os
import threading

import psycopg2
import psycopg2.pool


_pool_lock = threading.Lock()
_health_pool: psycopg2.pool.ThreadedConnectionPool | None = None
_chat_pool: psycopg2.pool.ThreadedConnectionPool | None = None


class _PooledConn:
    """Thin proxy that returns the connection to the pool on close()."""

    __slots__ = ("_raw", "_pool")

    def __init__(self, raw_conn, pool):
        object.__setattr__(self, "_raw", raw_conn)
        object.__setattr__(self, "_pool", pool)

    # Proxy attribute access to the underlying connection
    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_raw"), name)

    def __setattr__(self, name, value):
        setattr(object.__getattribute__(self, "_raw"), name, value)

    def close(self):
        raw = object.__getattribute__(self, "_raw")
        pool = object.__getattribute__(self, "_pool")
        try:
            if not raw.closed:
                raw.rollback()
        except Exception:
            pass
        try:
            pool.putconn(raw)
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def dsn_kwargs(dbname: str | None = None) -> dict:
    """Build psycopg2 connection kwargs from environment variables.

    Reads DB_HOST / DB_PORT / DB_NAME / DB_USER / DB_PASSWORD lazily so
    that callers (including the test-container fixture) can override env
    vars at any point before the first call.
    """
    return dict(
        host=os.environ.get("DB_HOST", "localhost"),
        port=os.environ.get("DB_PORT", "5432"),
        dbname=dbname or os.environ.get("DB_NAME", "health"),
        user=os.environ.get("DB_USER", "postgres"),
        password=os.environ.get("DB_PASSWORD", ""),
        connect_timeout=5,
    )


def _ensure_health_pool() -> psycopg2.pool.ThreadedConnectionPool:
    global _health_pool
    if _health_pool is None:
        with _pool_lock:
            if _health_pool is None:
                _health_pool = psycopg2.pool.ThreadedConnectionPool(
                    minconn=1, maxconn=8, **dsn_kwargs(),
                )
    return _health_pool


def _ensure_chat_pool() -> psycopg2.pool.ThreadedConnectionPool:
    global _chat_pool
    if _chat_pool is None:
        with _pool_lock:
            if _chat_pool is None:
                _chat_pool = psycopg2.pool.ThreadedConnectionPool(
                    minconn=1, maxconn=4, **dsn_kwargs("healthcoach_chat"),
                )
    return _chat_pool


def get_conn() -> _PooledConn:
    """Borrow a connection from the health DB pool."""
    pool = _ensure_health_pool()
    return _PooledConn(pool.getconn(), pool)


def get_conn_chat() -> _PooledConn:
    """Borrow a connection from the Chainlit chat DB pool."""
    pool = _ensure_chat_pool()
    return _PooledConn(pool.getconn(), pool)
