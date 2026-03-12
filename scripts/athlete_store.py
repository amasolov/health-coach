"""Single source of truth for per-user athlete configuration.

Reads/writes the ``athlete_config`` table (JSONB column).

The ``config`` column stores a nested dict::

    {
      "profile": {...},
      "thresholds": {"heart_rate": {...}, "running": {...}, ...},
      "body": {...},
      "goals": {...},
      ...
    }

Threshold history
~~~~~~~~~~~~~~~~~
The ``threshold_history`` table keeps dated snapshots of the flat
threshold values (FTP, LTHR, resting HR, …).  When TSS is calculated
for an activity, ``get_thresholds_for_date()`` returns the snapshot
whose ``effective_date`` is closest to (but not after) the activity
date.  This ensures that a change in LTHR today does not retroactively
distort TSS for older workouts.
"""

from __future__ import annotations

import json as _json
import logging
from datetime import date as _date
from typing import Any

log = logging.getLogger(__name__)


# ------------------------------------------------------------------
# DB connection
# ------------------------------------------------------------------

def _get_conn():
    from scripts.db_pool import get_conn
    return get_conn()


def _try_conn():
    """Return a connection or None if the DB is unavailable."""
    try:
        return _get_conn()
    except Exception:
        return None


# ------------------------------------------------------------------
# Public API
# ------------------------------------------------------------------

def load(slug: str) -> dict | None:
    """Load an athlete's config dict, or *None* if unknown."""
    conn = _try_conn()
    if not conn:
        return None
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT config FROM athlete_config WHERE slug = %s",
            (slug,),
        )
        row = cur.fetchone()
        if row:
            cfg = row[0]
            return cfg if isinstance(cfg, dict) else _json.loads(cfg)
        return None
    except Exception:
        log.debug("athlete_store.load failed for '%s'", slug, exc_info=True)
        return None
    finally:
        conn.close()


def save(slug: str, config: dict) -> None:
    """Persist an athlete's config dict."""
    _save_to_db(slug, config)


def delete(slug: str) -> None:
    """Remove an athlete's config."""
    conn = _try_conn()
    if not conn:
        return
    try:
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("DELETE FROM athlete_config WHERE slug = %s", (slug,))
        cur.close()
    except Exception:
        log.debug("athlete_store.delete failed for '%s'", slug, exc_info=True)
    finally:
        conn.close()


def update_field(slug: str, field_path: str, value: Any) -> None:
    """Update a single nested field, e.g. ``thresholds.heart_rate.lthr_run``.

    Loads the current config, applies the change, and saves.
    """
    config = load(slug) or {}
    parts = field_path.split(".")
    target = config
    for part in parts[:-1]:
        target = target.setdefault(part, {})
    target[parts[-1]] = value
    save(slug, config)


def load_thresholds_flat(slug: str) -> dict:
    """Return a flat dict of the key threshold values used for TSS estimation.

    Keys: ftp, rftp, lthr_run, lthr_bike, resting_hr, max_hr, weight_kg.
    """
    config = load(slug)
    if not config:
        return {}
    thresholds = config.get("thresholds", {})
    hr = thresholds.get("heart_rate", {})
    running = thresholds.get("running", {})
    body = config.get("body", {})
    return {
        "ftp": thresholds.get("cycling", {}).get("ftp"),
        "rftp": running.get("rftp_garmin") or running.get("critical_power"),
        "lthr_run": hr.get("lthr_run"),
        "lthr_bike": hr.get("lthr_bike"),
        "resting_hr": hr.get("resting_hr"),
        "max_hr": hr.get("max_hr"),
        "weight_kg": body.get("weight_kg"),
    }


# ------------------------------------------------------------------
# Threshold history
# ------------------------------------------------------------------

def record_threshold_snapshot(
    slug: str,
    source: str = "garmin",
    effective: _date | None = None,
) -> bool:
    """Upsert a snapshot of the current flat thresholds into ``threshold_history``.

    Called after ``refresh_thresholds()`` updates athlete config so that
    future TSS calculations can look up the thresholds that were active
    on any given date.

    Returns *True* if a row was written.
    """
    flat = load_thresholds_flat(slug)
    if not flat or all(v is None for v in flat.values()):
        return False

    conn = _try_conn()
    if not conn:
        return False
    try:
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("SELECT id FROM users WHERE slug = %s", (slug,))
        row = cur.fetchone()
        if not row:
            return False
        user_id = row[0]
        if not effective:
            from scripts.tz import load_user_tz, user_today
            effective = user_today(load_user_tz(slug))
        eff = effective

        cur.execute(
            """INSERT INTO threshold_history
                   (user_id, effective_date, ftp, rftp,
                    lthr_run, lthr_bike, resting_hr, max_hr,
                    weight_kg, source)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (user_id, effective_date) DO UPDATE SET
                   ftp        = EXCLUDED.ftp,
                   rftp       = EXCLUDED.rftp,
                   lthr_run   = EXCLUDED.lthr_run,
                   lthr_bike  = EXCLUDED.lthr_bike,
                   resting_hr = EXCLUDED.resting_hr,
                   max_hr     = EXCLUDED.max_hr,
                   weight_kg  = EXCLUDED.weight_kg,
                   source     = EXCLUDED.source,
                   created_at = NOW()""",
            (
                user_id, eff,
                flat.get("ftp"), flat.get("rftp"),
                flat.get("lthr_run"), flat.get("lthr_bike"),
                flat.get("resting_hr"), flat.get("max_hr"),
                flat.get("weight_kg"), source,
            ),
        )
        cur.close()
        return True
    except Exception:
        log.warning("record_threshold_snapshot failed for '%s'", slug, exc_info=True)
        return False
    finally:
        conn.close()


def get_thresholds_for_date(user_id: int, activity_date: _date | str) -> dict:
    """Return the threshold snapshot effective on *activity_date*.

    Looks for the most recent ``threshold_history`` row where
    ``effective_date <= activity_date``.  Returns an empty dict when
    no history exists (callers should fall back to current config).
    """
    conn = _try_conn()
    if not conn:
        return {}
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT ftp, rftp, lthr_run, lthr_bike,
                      resting_hr, max_hr, weight_kg
               FROM threshold_history
               WHERE user_id = %s AND effective_date <= %s
               ORDER BY effective_date DESC
               LIMIT 1""",
            (user_id, activity_date),
        )
        row = cur.fetchone()
        if not row:
            return {}
        return {
            "ftp": row[0],
            "rftp": row[1],
            "lthr_run": row[2],
            "lthr_bike": row[3],
            "resting_hr": row[4],
            "max_hr": row[5],
            "weight_kg": row[6],
        }
    except Exception:
        log.debug("get_thresholds_for_date failed", exc_info=True)
        return {}
    finally:
        conn.close()


def load_threshold_timeline(user_id: int) -> list[tuple[_date, dict]]:
    """Load all threshold history rows for a user, sorted ascending.

    Returns a list of ``(effective_date, flat_dict)`` tuples suitable
    for :func:`pick_thresholds`.  Loads everything in a single query so
    the caller can do fast in-memory lookups per activity.
    """
    conn = _try_conn()
    if not conn:
        return []
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT effective_date, ftp, rftp, lthr_run, lthr_bike,
                      resting_hr, max_hr, weight_kg
               FROM threshold_history
               WHERE user_id = %s
               ORDER BY effective_date ASC""",
            (user_id,),
        )
        return [
            (
                row[0],
                {
                    "ftp": row[1], "rftp": row[2],
                    "lthr_run": row[3], "lthr_bike": row[4],
                    "resting_hr": row[5], "max_hr": row[6],
                    "weight_kg": row[7],
                },
            )
            for row in cur.fetchall()
        ]
    except Exception:
        log.debug("load_threshold_timeline failed", exc_info=True)
        return []
    finally:
        conn.close()


def pick_thresholds(
    timeline: list[tuple[_date, dict]],
    activity_date: _date | str,
) -> dict:
    """Pick the threshold snapshot effective at *activity_date* from *timeline*.

    *timeline* must be sorted ascending by effective_date (as returned by
    :func:`load_threshold_timeline`).  Returns an empty dict when no
    entry precedes *activity_date*.
    """
    if not timeline:
        return {}
    if isinstance(activity_date, str):
        activity_date = _date.fromisoformat(activity_date)
    result: dict = {}
    for eff, thresholds in timeline:
        if eff <= activity_date:
            result = thresholds
        else:
            break
    return result


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------

def _save_to_db(slug: str, config: dict) -> None:
    conn = _try_conn()
    if not conn:
        log.debug("athlete_store: DB unavailable, skipping write for '%s'", slug)
        return
    try:
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("SELECT id FROM users WHERE slug = %s", (slug,))
        row = cur.fetchone()
        user_id = row[0] if row else None

        from psycopg2.extras import Json
        cur.execute(
            """INSERT INTO athlete_config (slug, user_id, config, updated_at)
               VALUES (%s, %s, %s, NOW())
               ON CONFLICT (slug) DO UPDATE
               SET config = EXCLUDED.config,
                   user_id = COALESCE(EXCLUDED.user_id, athlete_config.user_id),
                   updated_at = NOW()""",
            (slug, user_id, Json(config)),
        )
        cur.close()
    except Exception:
        log.warning("athlete_store: DB write failed for '%s'", slug, exc_info=True)
    finally:
        conn.close()
