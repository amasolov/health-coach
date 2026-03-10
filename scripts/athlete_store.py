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
"""

from __future__ import annotations

import json as _json
import logging
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
