"""Single source of truth for per-user athlete configuration.

Reads/writes the ``athlete_config`` table (JSONB).  Falls back to
config/athlete.yaml when the DB is unreachable so standalone local-dev
scripts still work (e.g. garmin_pull_profile.py without a DB).

The JSONB ``config`` column stores the same nested dict that previously
lived under ``users.<slug>`` in athlete.yaml::

    {
      "profile": {...},
      "thresholds": {"heart_rate": {...}, "running": {...}, ...},
      "body": {...},
      "goals": {...},
      ...
    }

During the transition the ``save()`` function dual-writes to both the DB
and the YAML file so that nothing breaks.
"""

from __future__ import annotations

import json as _json
import logging
import os
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent
_YAML_PATH = _ROOT / "config" / "athlete.yaml"


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
    """Load an athlete's config dict.

    Returns the config dict (same shape as the old ``users.<slug>`` YAML
    block), or *None* if the slug is unknown.
    """
    conn = _try_conn()
    if conn:
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
        except Exception:
            log.debug("athlete_store.load: DB read failed, falling back to YAML", exc_info=True)
        finally:
            conn.close()

    return _load_from_yaml(slug)


def save(slug: str, config: dict) -> None:
    """Persist an athlete's config dict.

    Writes to the DB (primary) and YAML (backup/transition).
    """
    _save_to_db(slug, config)
    _save_to_yaml(slug, config)


def delete(slug: str) -> None:
    """Remove an athlete's config from both DB and YAML."""
    conn = _try_conn()
    if conn:
        try:
            conn.autocommit = True
            cur = conn.cursor()
            cur.execute("DELETE FROM athlete_config WHERE slug = %s", (slug,))
            cur.close()
        except Exception:
            log.debug("athlete_store.delete: DB delete failed for '%s'", slug, exc_info=True)
        finally:
            conn.close()

    try:
        path = _YAML_PATH
        if path.exists():
            with open(path) as f:
                data = yaml.safe_load(f) or {}
            users = data.get("users", {})
            if slug in users:
                del users[slug]
                with open(path, "w") as f:
                    yaml.dump(data, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
    except Exception:
        log.debug("athlete_store.delete: YAML cleanup failed for '%s'", slug, exc_info=True)


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

    This is the hot-path helper consumed by sync_garmin and run_sync.
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
# Seeding
# ------------------------------------------------------------------

def seed_from_yaml(yaml_path: str | Path | None = None) -> dict[str, bool]:
    """Import all users from a YAML file into the DB.

    Only inserts rows that don't already exist (idempotent).
    Returns ``{slug: True}`` for each slug that was seeded.
    """
    path = Path(yaml_path) if yaml_path else _YAML_PATH
    if not path.exists():
        return {}
    with open(path) as f:
        data = yaml.safe_load(f) or {}

    seeded = {}
    for slug, config in data.get("users", {}).items():
        if _db_has_slug(slug):
            continue
        _save_to_db(slug, config)
        seeded[slug] = True
        log.info("Seeded athlete_config for '%s' from YAML", slug)
    return seeded


def ensure_seeded(slug: str, yaml_path: str | Path | None = None) -> None:
    """Ensure a single user is present in the DB, seeding from YAML if needed."""
    if _db_has_slug(slug):
        return
    path = Path(yaml_path) if yaml_path else _YAML_PATH
    config = _load_from_yaml(slug, path)
    if config:
        _save_to_db(slug, config)
        log.info("Seeded athlete_config for '%s' from YAML", slug)


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------

def _db_has_slug(slug: str) -> bool:
    conn = _try_conn()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM athlete_config WHERE slug = %s", (slug,))
        return cur.fetchone() is not None
    except Exception:
        return False
    finally:
        conn.close()


def _save_to_db(slug: str, config: dict) -> None:
    conn = _try_conn()
    if not conn:
        log.debug("athlete_store: DB unavailable, skipping DB write for '%s'", slug)
        return
    try:
        conn.autocommit = True
        cur = conn.cursor()
        # Try to link to the users table if possible
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


def _load_from_yaml(slug: str, yaml_path: Path | None = None) -> dict | None:
    path = yaml_path or _YAML_PATH
    if not path.exists():
        return None
    try:
        with open(path) as f:
            data = yaml.safe_load(f) or {}
        return data.get("users", {}).get(slug)
    except Exception:
        return None


def _save_to_yaml(slug: str, config: dict) -> None:
    """Dual-write to YAML for backward compatibility during transition."""
    path = _YAML_PATH
    try:
        data = {}
        if path.exists():
            with open(path) as f:
                data = yaml.safe_load(f) or {}
        data.setdefault("users", {})[slug] = deepcopy(config)
        with open(path, "w") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
    except Exception:
        log.debug("athlete_store: YAML write failed", exc_info=True)
