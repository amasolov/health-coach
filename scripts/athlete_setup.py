#!/usr/bin/env python3
"""
Interactive CLI for athlete profile setup.

Default mode: runs a full 6-month fitness assessment, presents the
results, auto-populates the athlete profile, then prompts for any
remaining fields.

With --profile-only: just fetches Garmin profile data and prompts for
missing fields (lighter, faster).

Usage:
    python scripts/athlete_setup.py --user alexey
    python scripts/athlete_setup.py --user alexey --profile-only
    python scripts/athlete_setup.py --user alexey --days 90
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
from scripts.fitness_assessment import assess_fitness

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


# ---------------------------------------------------------------------------
# Full assessment mode
# ---------------------------------------------------------------------------

def _print_section(title: str) -> None:
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print(f"{'=' * 60}")


def _print_assessment(result: dict) -> None:
    """Pretty-print the fitness assessment to the terminal."""

    # Training overview
    overview = result.get("training_overview", {})
    _print_section("TRAINING OVERVIEW")
    print(f"  Activities:       {overview.get('total_activities', 0)}")
    print(f"  Total hours:      {overview.get('total_hours', 0)}")
    print(f"  Total distance:   {overview.get('total_distance_km', 0)} km")
    print(f"  Avg weekly hours: {overview.get('avg_weekly_hours', 0)}")
    print(f"  Avg weekly sess:  {overview.get('avg_weekly_sessions', 0)}")
    print(f"  Consistency:      {overview.get('consistency_score', 0)}% of weeks with 3+ sessions")
    print(f"  Longest gap:      {overview.get('longest_gap_days', 0)} days")

    sports = overview.get("sport_distribution", {})
    if sports:
        print("\n  Sport distribution:")
        for sport, data in sports.items():
            print(f"    {sport:25s} {data['sessions']:3d} sessions ({data['pct']}%) -- {data['hours']} hrs")

    # Endurance metrics
    endurance = result.get("endurance_metrics", {})
    if endurance:
        _print_section("ENDURANCE METRICS")
        if endurance.get("vo2max"):
            print(f"  VO2max (Garmin):  {endurance['vo2max']}")
        if endurance.get("estimated_ctl"):
            print(f"  Estimated CTL:    {endurance['estimated_ctl']}")

        run = endurance.get("running", {})
        if run:
            print(f"\n  Running ({run.get('total_sessions', 0)} sessions):")
            if run.get("avg_pace_min_km"):
                print(f"    Avg pace:       {run['avg_pace_min_km']} min/km")
            if run.get("avg_hr"):
                print(f"    Avg HR:         {run['avg_hr']} bpm")
            if run.get("avg_power_w"):
                print(f"    Avg power:      {run['avg_power_w']} W")
            if run.get("max_hr_seen"):
                print(f"    Max HR seen:    {run['max_hr_seen']} bpm")

            fastest = run.get("fastest_efforts", [])
            if fastest:
                print("    Fastest efforts:")
                for f in fastest:
                    print(f"      {f['date']}  {f['distance_km']}km  {f.get('pace_min_km', '?')} min/km  HR {f.get('avg_hr', '?')}")

        bike = endurance.get("cycling", {})
        if bike:
            print(f"\n  Cycling ({bike.get('total_sessions', 0)} sessions):")
            if bike.get("avg_power_w"):
                print(f"    Avg power:      {bike['avg_power_w']} W")
            if bike.get("avg_hr"):
                print(f"    Avg HR:         {bike['avg_hr']} bpm")

    # Intensity analysis
    intensity = result.get("intensity_analysis", {})
    if intensity and not intensity.get("note"):
        _print_section("INTENSITY DISTRIBUTION")
        dist = intensity.get("zone_distribution_pct", {})
        for zone, pct in dist.items():
            bar = "#" * int(pct / 2)
            print(f"    {zone:15s} {pct:5.1f}%  {bar}")
        print(f"\n  Easy:     {intensity.get('easy_pct', 0)}%")
        print(f"  Moderate: {intensity.get('moderate_pct', 0)}%")
        print(f"  Hard:     {intensity.get('hard_pct', 0)}%")
        print(f"  --> {intensity.get('polarization_assessment', '')}")
    elif intensity.get("note"):
        _print_section("INTENSITY DISTRIBUTION")
        print(f"  {intensity['note']}")

    # Body composition
    body = result.get("body_composition", {})
    _print_section("BODY COMPOSITION")
    if body.get("data_points", 0) > 0:
        print(f"  Data points:      {body['data_points']}")
        if body.get("current_weight_kg"):
            print(f"  Current weight:   {body['current_weight_kg']} kg")
        if body.get("weight_trend"):
            print(f"  Weight trend:     {body['weight_trend']}")
        if body.get("current_body_fat_pct"):
            print(f"  Current body fat: {body['current_body_fat_pct']}%")
        if body.get("body_fat_trend"):
            print(f"  Body fat trend:   {body['body_fat_trend']}")
    else:
        print(f"  {body.get('note', 'No data')}")

    # Vitals
    vitals = result.get("vitals", {})
    _print_section("VITALS")
    if vitals.get("current_resting_hr"):
        print(f"  Current resting HR: {vitals['current_resting_hr']} bpm")
        print(f"  RHR trend:          {vitals.get('resting_hr_trend', 'unknown')}")
    else:
        print(f"  {vitals.get('note', 'No data')}")

    # Strength
    strength = result.get("strength_summary", {})
    if strength.get("total_sessions", 0) > 0:
        _print_section("STRENGTH TRAINING (Hevy)")
        print(f"  Total sessions:     {strength['total_sessions']}")
        print(f"  Avg sessions/week:  {strength['avg_sessions_per_week']}")
        print(f"  Volume trend:       {strength.get('volume_trend', 'unknown')}")
        top = strength.get("top_exercises", [])
        if top:
            print("  Top exercises:")
            for ex in top:
                w_str = f" (max {ex['max_weight_kg']}kg)" if ex.get("max_weight_kg") else ""
                print(f"    {ex['exercise']:30s} {ex['sessions']}x{w_str}")

    # Recommendations
    recs = result.get("recommendations", [])
    if recs:
        _print_section("RECOMMENDATIONS")
        for i, rec in enumerate(recs, 1):
            print(f"  {i}. {rec}")

    # Auto-populated profile
    written = result.get("written_to_config", {})
    if written:
        _print_section("AUTO-POPULATED PROFILE")
        for field, val in written.items():
            print(f"    {field}: {val}")


def run_full_assessment(slug: str, client, hevy_key: str | None, days: int) -> list[dict]:
    """Run the full fitness assessment and return the missing fields list."""
    print(f"\nFetching {days} days of data from Garmin Connect", end="")
    if hevy_key:
        print(" and Hevy", end="")
    print("...\n")

    result = assess_fitness(
        slug=slug,
        garmin_client=client,
        hevy_api_key=hevy_key,
        lookback_days=days,
    )

    profile_data = result.get("auto_profile", {})
    if profile_data.get("fetched"):
        written = merge_into_athlete_yaml(
            str(ATHLETE_PATH), slug, profile_data["fetched"]
        )
        result["written_to_config"] = written

    _print_assessment(result)

    return result.get("missing_data", [])


# ---------------------------------------------------------------------------
# Profile-only mode (legacy)
# ---------------------------------------------------------------------------

def run_profile_only(slug: str, client) -> list[dict]:
    """Quick profile fetch -- the original behavior."""
    print("\nFetching profile data from Garmin Connect...")
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

    return missing


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Interactive athlete profile setup")
    parser.add_argument("--user", default=os.environ.get("USER_SLUG", "alexey"))
    parser.add_argument(
        "--profile-only", action="store_true",
        help="Quick mode: only fetch profile fields, skip full assessment",
    )
    parser.add_argument(
        "--days", type=int, default=180,
        help="Lookback period in days for the full assessment (default: 180)",
    )
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
        print("Garmin Connect: authenticated")
        client = try_cached_login(slug)

    # Get Hevy API key from environment
    hevy_key = os.environ.get("HEVY_API_KEY")

    # Run assessment or profile-only
    if client:
        if args.profile_only:
            missing = run_profile_only(slug, client)
        else:
            missing = run_full_assessment(slug, client, hevy_key, args.days)
    else:
        missing = _build_all_missing(slug)

    # Interactive prompts for remaining fields
    important = [m for m in missing if m.get("importance", "optional") != "optional"]
    optional = [m for m in missing if m.get("importance", "optional") == "optional"]

    if important:
        print(f"\n--- {len(important)} important field(s) still need values ---")
        print("For each field, provide a value or press Enter to skip.\n")

        for item in important:
            field = item["field"]
            hint = item["hint"]
            importance = item.get("importance", "recommended")
            display = f"[{importance.upper()}] {field.replace('.', ' > ')}"
            val = _prompt(display, hint)
            if val is not None:
                typed_val = _coerce(val, field)
                update_athlete_field(str(ATHLETE_PATH), slug, field, typed_val)
                print(f"  -> Saved {field} = {typed_val}")

    if optional:
        print(f"\n--- {len(optional)} optional field(s) can be set (or skipped) ---")
        for item in optional:
            field = item["field"]
            hint = item["hint"]
            display = f"[OPTIONAL] {field.replace('.', ' > ')}"
            val = _prompt(display, hint)
            if val is not None:
                typed_val = _coerce(val, field)
                update_athlete_field(str(ATHLETE_PATH), slug, field, typed_val)
                print(f"  -> Saved {field} = {typed_val}")

    if not important and not optional:
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

    importance_map = {
        "thresholds.heart_rate.max_hr": "critical",
        "thresholds.heart_rate.lthr_run": "critical",
        "body.weight_kg": "recommended",
        "profile.date_of_birth": "recommended",
        "profile.sex": "recommended",
        "thresholds.running.vo2max_garmin": "recommended",
        "thresholds.heart_rate.resting_hr": "recommended",
        "thresholds.running.critical_power": "recommended",
        "thresholds.running.threshold_pace": "recommended",
    }

    missing = []
    for section, fields in expected.items():
        parts = section.split(".")
        node = user
        for p in parts:
            node = node.get(p, {}) if isinstance(node, dict) else {}
        for field in fields:
            val = node.get(field) if isinstance(node, dict) else None
            if val is None:
                full_field = f"{section}.{field}"
                hint = FIELD_HINTS.get(field, FIELD_HINTS.get(section, "No specific guidance."))
                missing.append({
                    "field": full_field,
                    "hint": hint,
                    "importance": importance_map.get(full_field, "optional"),
                    "can_skip": full_field not in importance_map,
                })

    return missing


if __name__ == "__main__":
    sys.exit(main())
