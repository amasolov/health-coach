"""Lightweight helper that inserts structured events into the ops_log table.

Usage:
    from scripts.ops_emit import emit
    emit("sync", "garmin_sync", user_id=2, duration_ms=3400,
         new_activities=3, new_vitals=1)

Gracefully no-ops when the DB connection is unavailable or the table
does not yet exist (pre-migration).
"""

from __future__ import annotations

import json as _json
import os
import time as _time
from contextlib import contextmanager
from typing import Any

import psycopg2


def _get_conn():
    return psycopg2.connect(
        host=os.environ.get("DB_HOST", "localhost"),
        port=os.environ.get("DB_PORT", "5432"),
        dbname=os.environ.get("DB_NAME", "health"),
        user=os.environ.get("DB_USER", "postgres"),
        password=os.environ.get("DB_PASSWORD", ""),
    )


def emit(
    category: str,
    event: str,
    *,
    user_id: int | None = None,
    status: str = "ok",
    duration_ms: int | None = None,
    **detail: Any,
) -> None:
    """Insert a single event row into ops_log."""
    try:
        conn = _get_conn()
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO ops_log (category, event, user_id, status, duration_ms, detail)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            (
                category,
                event,
                user_id,
                status,
                duration_ms,
                _json.dumps(detail) if detail else "{}",
            ),
        )
        cur.close()
        conn.close()
    except Exception:
        pass


@contextmanager
def timed(category: str, event: str, *, user_id: int | None = None, **extra: Any):
    """Context manager that emits a timed event on exit.

    Usage:
        with ops_emit.timed("sync", "garmin_sync", user_id=2) as ctx:
            result = do_sync()
            ctx["new_activities"] = result["count"]
    """
    ctx: dict[str, Any] = {**extra}
    t0 = _time.monotonic()
    try:
        yield ctx
        ms = int((_time.monotonic() - t0) * 1000)
        emit(category, event, user_id=user_id, status=ctx.pop("_status", "ok"),
             duration_ms=ms, **ctx)
    except Exception as exc:
        ms = int((_time.monotonic() - t0) * 1000)
        ctx["error"] = str(exc)[:500]
        emit(category, event, user_id=user_id, status="error",
             duration_ms=ms, **ctx)
        raise
