#!/usr/bin/env python3
"""
Interactive CLI for athlete profile setup.

Authenticates with Garmin Connect, auto-fetches available data,
then prompts the user for any missing fields with hints and
smart defaults.

Usage:
    python scripts/athlete_setup.py --user alexey
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.garmin_auth import try_cached_login, get_auth_status
from scripts.garmin_fetch import (
    FIELD_HINTS,
    fetch_garmin_profile,
    merge_into_athlete_yaml,
    update_athlete_field,
)

ATHLETE_PATH = ROOT / "config" / "athlete.yaml"


def _prompt(label: str, hint: str, default: str | None = None) -> str | None:
    """Prompt user for a value, showing the hint and optional default."""
    print(f"\n  {label}")
    print(f"  Hint: {hint}")
    if default:
        raw = input(f"  Value [{default}]: ").strip()
        return raw if raw else default
    raw = input("  Value (or Enter to skip): ").strip()
    return raw if raw else None


def _coerce(value: str, field: str) -> int | float | str:
    """Best-effort type coercion for common numeric fields."""
    numeric_fields = {
        "max_hr", "resting_hr", "lthr_run", "lthr_bike",
        "critical_power", "ftp",
    }
    float_fields = {
        "weight_kg", "body_fat_pct", "muscle_mass_kg", "bone_mass_kg",
        "bmi", "vo2max_garmin", "vo2max_lab", "ftp_wkg",
        "threshold_pace", "lt1_hr", "lt1_pace", "lt2_hr", "lt2_pace",
        "weekly_volume_hrs", "longest_run_km", "longest_ride_km",
        "height_cm",
    }
    int_fields = {"strength_sessions_per_week"}

    key = field.split(".")[-1]
    try:
        if key in numeric_fields:
            return int(float(value))
        if key in float_fields:
            return float(value)
        if key in int_fields:
            return int(value)
    except ValueError:
        pass
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description="Interactive athlete profile setup")
    parser.add_argument("--user", default=os.environ.get("USER_SLUG", "alexey"))
    args = parser.parse_args()
    slug = args.user

    print(f"=== Athlete Profile Setup for '{slug}' ===\n")

    # Check Garmin auth
    status = get_auth_status(slug)
    if not status["authenticated"]:
        print("Garmin Connect: NOT authenticated.")
        print("Run 'task garmin:auth' first to authenticate, then re-run this setup.")
        print("Continuing without Garmin data...\n")
        client = None
    else:
        print("Garmin Connect: authenticated ✓")
        client = try_cached_login(slug)

    # Fetch from Garmin if possible
    if client:
        print("\nFetching data from Garmin Connect...")
        result = fetch_garmin_profile(slug, client)
        fetched = result["fetched"]
        sources = result["sources"]
        missing = result["missing"]

        written = merge_into_athlete_yaml(str(ATHLETE_PATH), slug, fetched)

        if written:
            print("\nAuto-populated from Garmin:")
            for field, val in written.items():
                src = sources.get(field.split(".")[-1], "garmin")
                print(f"  {field}: {val}  (source: {src})")
        else:
            print("\nNo new values fetched (all fields already populated).")
    else:
        missing = _build_all_missing(slug)

    # Interactive prompts for missing fields
    if missing:
        print(f"\n--- {len(missing)} field(s) still need values ---")
        print("For each field, provide a value or press Enter to skip.\n")

        for item in missing:
            field = item["field"]
            hint = item["hint"]
            display = field.replace(".", " > ")
            val = _prompt(display, hint)
            if val is not None:
                typed_val = _coerce(val, field)
                update_athlete_field(str(ATHLETE_PATH), slug, field, typed_val)
                print(f"  -> Saved {field} = {typed_val}")
    else:
        print("\nAll fields populated!")

    print("\nProfile setup complete. Review config/athlete.yaml for details.")
    print("Run 'task zones:calculate' to compute training zones from your thresholds.")
    return 0


def _build_all_missing(slug: str) -> list[dict]:
    """Build missing field list without Garmin data."""
    expected = {
        "profile": ["date_of_birth", "sex", "height_cm"],
        "thresholds.heart_rate": ["max_hr", "resting_hr", "lthr_run", "lthr_bike"],
        "thresholds.running": ["critical_power", "threshold_pace", "vo2max_garmin"],
        "thresholds.cycling": ["ftp"],
        "body": ["weight_kg", "body_fat_pct"],
        "training_status": [
            "weekly_volume_hrs", "longest_run_km", "longest_ride_km",
            "strength_sessions_per_week", "current_phase",
        ],
    }

    import yaml
    with open(ATHLETE_PATH) as f:
        data = yaml.safe_load(f) or {}
    user = data.get("users", {}).get(slug, {})

    missing = []
    for section, fields in expected.items():
        parts = section.split(".")
        node = user
        for p in parts:
            node = node.get(p, {}) if isinstance(node, dict) else {}
        for field in fields:
            val = node.get(field) if isinstance(node, dict) else None
            if val is None:
                hint = FIELD_HINTS.get(field, FIELD_HINTS.get(section, "No specific guidance."))
                missing.append({"field": f"{section}.{field}", "hint": hint})

    return missing


if __name__ == "__main__":
    sys.exit(main())
