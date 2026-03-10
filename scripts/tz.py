"""
Timezone utilities.

All timestamps in the database are stored as UTC (TIMESTAMPTZ).
User-facing date boundaries ("today", "this week", default ranges)
must respect the athlete's configured timezone.

Usage:
    from scripts.tz import user_today, user_now, utc_now, load_user_tz, DEFAULT_TZ

    tz = load_user_tz("alexey")          # ZoneInfo("Australia/Sydney")
    today = user_today(tz)               # date in athlete's local time
    now = user_now(tz)                   # aware datetime in athlete's tz
    sql_cast = tz_date_cast(tz)          # "(time AT TIME ZONE 'Australia/Sydney')::date"
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

DEFAULT_TZ_NAME = "Australia/Sydney"
DEFAULT_TZ = ZoneInfo(DEFAULT_TZ_NAME)


def load_user_tz(slug: str) -> ZoneInfo:
    """Load the timezone for a user, falling back to default."""
    try:
        from scripts.athlete_store import load as _load_athlete
        cfg = _load_athlete(slug) or {}
        tz_name = cfg.get("profile", {}).get("timezone", DEFAULT_TZ_NAME)
        return ZoneInfo(tz_name)
    except Exception:
        return DEFAULT_TZ


def user_now(tz: ZoneInfo | None = None) -> datetime:
    """Current aware datetime in the user's timezone."""
    return datetime.now(tz or DEFAULT_TZ)


def user_today(tz: ZoneInfo | None = None) -> date:
    """Today's date in the user's timezone."""
    return user_now(tz).date()


def utc_now() -> datetime:
    """Current aware datetime in UTC."""
    return datetime.now(timezone.utc)


def ts_to_utc(ts_ms: int) -> datetime:
    """Convert epoch milliseconds to an aware UTC datetime."""
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)


def to_utc(dt: datetime) -> datetime:
    """Convert an aware datetime to UTC. Raises if naive."""
    if dt.tzinfo is None:
        raise ValueError("Cannot convert naive datetime to UTC")
    return dt.astimezone(timezone.utc)


def tz_date_cast(tz: ZoneInfo | None = None) -> str:
    """
    Return a SQL expression that casts the `time` column to a date
    in the user's timezone.

    Example: "(time AT TIME ZONE 'Australia/Sydney')::date"
    """
    tz_name = str(tz) if tz else DEFAULT_TZ_NAME
    return f"(time AT TIME ZONE '{tz_name}')::date"
