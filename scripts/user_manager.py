"""
User registration and management.

Handles creating new users in:
  - PostgreSQL (users table)
  - /config/healthcoach/users.json (persistent user store)
  - /config/healthcoach/athlete.yaml (athlete profile stub)

Used by the onboarding flow in chat_app.py.
"""

from __future__ import annotations

import json
import os
import re
import secrets
from pathlib import Path
from typing import Any

import yaml

HA_CFG_DIR = Path("/config/healthcoach")
USERS_FILE = HA_CFG_DIR / "users.json"


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _get_conn():
    from scripts.db_pool import get_conn
    return get_conn()


def slug_available(slug: str) -> bool:
    """Return True if the slug is not yet taken in the database."""
    try:
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM users WHERE slug = %s", (slug,))
        exists = cur.fetchone() is not None
        conn.close()
        return not exists
    except Exception:
        return False


def create_db_user(slug: str, display_name: str) -> int | None:
    """Insert a new user row. Returns the new user_id or None on failure."""
    try:
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO users (slug, display_name) VALUES (%s, %s) RETURNING id",
            (slug, display_name),
        )
        user_id = cur.fetchone()[0]
        conn.commit()
        conn.close()
        return user_id
    except Exception as e:
        print(f"ERROR: create_db_user failed: {e}")
        return None


# ---------------------------------------------------------------------------
# users.json helpers
# ---------------------------------------------------------------------------

def add_user_to_users_file(user_entry: dict) -> bool:
    """
    Append a user entry to /config/healthcoach/users.json.
    Creates the file if it doesn't exist.
    """
    HA_CFG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        users = json.loads(USERS_FILE.read_text()) if USERS_FILE.exists() else []
        # Idempotent: remove any existing entry with the same slug
        users = [u for u in users if u.get("slug") != user_entry["slug"]]
        users.append(user_entry)
        USERS_FILE.write_text(json.dumps(users, indent=2))
        return True
    except Exception as e:
        print(f"ERROR: add_user_to_users_file failed: {e}")
        return False


# ---------------------------------------------------------------------------
# Athlete config helpers
# ---------------------------------------------------------------------------

def _empty_athlete_entry(
    first_name: str,
    last_name: str,
    timezone: str,
) -> dict[str, Any]:
    return {
        "profile": {
            "name": f"{first_name} {last_name}".strip(),
            "date_of_birth": None,
            "sex": None,
            "height_cm": None,
            "timezone": timezone,
        },
        "thresholds": {
            "last_tested": None,
            "heart_rate": {
                "max_hr": None, "resting_hr": None,
                "lthr_run": None, "lthr_bike": None,
            },
            "running": {
                "critical_power": None, "threshold_pace": None,
                "vo2max_garmin": None, "vo2max_lab": None,
            },
            "cycling": {"ftp": None, "ftp_wkg": None},
            "lactate": {
                "lt1_hr": None, "lt1_pace": None,
                "lt2_hr": None, "lt2_pace": None,
                "test_protocol": None, "test_date": None,
            },
        },
        "body": {
            "weight_kg": None, "body_fat_pct": None,
            "muscle_mass_kg": None, "bone_mass_kg": None,
            "bmi": None, "measured_date": None,
            "source": "garmin_scale",
        },
        "goals": {
            "primary_goal": None,
            "target_event": None,
            "target_date": None,
            "secondary_goals": [],
            "available_hours_per_week": None,
            "preferred_sports": [],
            "constraints": [],
            "experience_level": None,
            "training_preferences": {"likes": None, "dislikes": None},
        },
        "training_status": {
            "weekly_volume_hrs": None, "longest_run_km": None,
            "longest_ride_km": None, "strength_sessions_per_week": None,
            "current_phase": None,
        },
        "action_items": [],
        "ifit": {
            "favourite_trainers": [], "available_equipment": [],
            "preferred_duration_min": [20, 45], "min_rating": 4.0,
            "software_number": None,
        },
        "treadmill": {"zone_speed_map": {}, "hill_map": {}},
    }


def create_athlete_config(
    slug: str,
    first_name: str,
    last_name: str,
    timezone: str = "UTC",
) -> None:
    """Create a new athlete config entry in the DB.

    Won't overwrite an existing entry.
    """
    from scripts import athlete_store

    existing = athlete_store.load(slug)
    if existing:
        return

    entry = _empty_athlete_entry(first_name, last_name, timezone)
    athlete_store.save(slug, entry)


# ---------------------------------------------------------------------------
# Slug helpers
# ---------------------------------------------------------------------------

def make_slug(first_name: str) -> str:
    """Generate a URL-safe lowercase slug from a first name."""
    base = re.sub(r"[^a-z0-9]", "", first_name.lower())
    return base or "user"


def find_available_slug(base: str) -> str:
    """Return base if available, otherwise base2, base3, …"""
    if slug_available(base):
        return base
    i = 2
    while not slug_available(f"{base}{i}"):
        i += 1
    return f"{base}{i}"


# ---------------------------------------------------------------------------
# Top-level registration
# ---------------------------------------------------------------------------

def register_user(
    email: str,
    first_name: str,
    last_name: str,
    slug: str,
    timezone: str = "UTC",
    garmin_email: str = "",
    garmin_password: str = "",
    hevy_api_key: str = "",
    mcp_api_key: str = "",
) -> dict:
    """
    Full user registration pipeline:
      1. Create DB row
      2. Append to options.json (HA addon context)
      3. Create athlete.yaml stub

    Returns {"success": True, "user_id": int, "user_entry": dict}
         or {"error": str}.
    """
    if not slug_available(slug):
        return {"error": f"Username '{slug}' is already taken. Please choose another."}

    display_name = f"{first_name} {last_name}".strip()
    user_id = create_db_user(slug, display_name)
    if user_id is None:
        return {"error": "Failed to create user record in database."}

    if not mcp_api_key:
        mcp_api_key = secrets.token_urlsafe(32)

    user_entry = {
        "first_name": first_name,
        "last_name": last_name,
        "slug": slug,
        "email": email,
        "mcp_api_key": mcp_api_key,
        "garmin_email": garmin_email,
        "garmin_password": garmin_password,
        "hevy_api_key": hevy_api_key,
    }

    add_user_to_users_file(user_entry)
    create_athlete_config(slug, first_name, last_name, timezone)

    return {
        "success": True,
        "user_id": user_id,
        "mcp_api_key": mcp_api_key,
        "user_entry": user_entry,
    }
