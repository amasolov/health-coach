#!/usr/bin/env python3
"""
Calculate training zones from threshold values in config/athlete.yaml
and write computed absolute values into config/zones.yaml.

Supports multi-user: iterates over all user slugs in athlete.yaml.
When USER_SLUG env var is set, only processes that user.

Usage:
    python scripts/calc_zones.py
    # or via task:
    task zones:calculate
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import yaml

from scripts.tz import load_user_tz, user_today
from scripts import athlete_store

ROOT = Path(__file__).resolve().parent.parent
ZONES_PATH = ROOT / "config" / "zones.yaml"


def load_yaml(path: Path) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def save_yaml(path: Path, data: dict) -> None:
    with open(path, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False, allow_unicode=True)


def compute_hr_zones(zones_section: dict, lthr: int) -> None:
    zones_section["anchor_value"] = lthr
    for z in zones_section["zones"]:
        lo_pct = z.get("lower_pct")
        hi_pct = z.get("upper_pct")
        z["lower"] = round(lthr * lo_pct) if lo_pct is not None else None
        z["upper"] = round(lthr * hi_pct) if hi_pct is not None else None


def compute_power_zones(zones_section: dict, anchor: int) -> None:
    zones_section["anchor_value"] = anchor
    for z in zones_section["zones"]:
        lo_pct = z.get("lower_pct")
        hi_pct = z.get("upper_pct")
        z["lower"] = round(anchor * lo_pct) if lo_pct is not None else None
        z["upper"] = round(anchor * hi_pct) if hi_pct is not None else None


def compute_pace_zones(zones_section: dict, threshold_pace: float) -> None:
    zones_section["anchor_value"] = threshold_pace
    for z in zones_section["zones"]:
        lo_pct = z.get("lower_pct")
        hi_pct = z.get("upper_pct")
        if lo_pct is not None:
            pace = threshold_pace * lo_pct
            minutes = int(pace)
            seconds = round((pace - minutes) * 60)
            z["lower"] = f"{minutes}:{seconds:02d}"
        else:
            z["lower"] = None
        if hi_pct is not None:
            pace = threshold_pace * hi_pct
            minutes = int(pace)
            seconds = round((pace - minutes) * 60)
            z["upper"] = f"{minutes}:{seconds:02d}"
        else:
            z["upper"] = None


def process_user(slug: str, athlete_data: dict, zones_data: dict) -> bool:
    """Process zones for a single user. Returns True if any zones were updated."""
    thresholds = athlete_data.get("thresholds", {})
    hr = thresholds.get("heart_rate", {})
    running = thresholds.get("running", {})
    cycling = thresholds.get("cycling", {})

    updated = False

    lthr = hr.get("lthr_run")
    if lthr:
        print(f"  Computing HR zones from LTHR = {lthr} bpm")
        compute_hr_zones(zones_data["heart_rate"], int(lthr))
        zones_data["heart_rate"]["max_hr"] = hr.get("max_hr")
        zones_data["heart_rate"]["resting_hr"] = hr.get("resting_hr")
        updated = True
    else:
        print(f"  WARN: No LTHR -- skipping HR zones")

    cp = running.get("critical_power")
    if cp:
        print(f"  Computing running power zones from CP = {cp} W")
        compute_power_zones(zones_data["running_power"], int(cp))
        updated = True
    else:
        print(f"  WARN: No Critical Power -- skipping running power zones")

    ftp = cycling.get("ftp")
    if ftp:
        print(f"  Computing cycling power zones from FTP = {ftp} W")
        compute_power_zones(zones_data["cycling_power"], int(ftp))
        updated = True
    else:
        print(f"  WARN: No FTP -- skipping cycling power zones")

    tp = running.get("threshold_pace")
    if tp:
        print(f"  Computing pace zones from threshold pace = {tp} min/km")
        compute_pace_zones(zones_data["running_pace"], float(tp))
        updated = True
    else:
        print(f"  WARN: No threshold pace -- skipping pace zones")

    if updated:
        zones_data["effective_date"] = user_today(load_user_tz(slug)).isoformat()

    return updated


def _list_slugs() -> list[str]:
    """List all athlete slugs from DB, falling back to YAML."""
    try:
        conn = athlete_store._try_conn()
        if conn:
            cur = conn.cursor()
            cur.execute("SELECT slug FROM athlete_config ORDER BY slug")
            slugs = [r[0] for r in cur.fetchall()]
            conn.close()
            if slugs:
                return slugs
    except Exception:
        pass
    # Fallback to YAML
    yaml_path = ROOT / "config" / "athlete.yaml"
    if yaml_path.exists():
        with open(yaml_path) as f:
            data = yaml.safe_load(f) or {}
        return list(data.get("users", {}).keys())
    return []


def main() -> int:
    zones = load_yaml(ZONES_PATH)

    target_slug = os.environ.get("USER_SLUG")
    slugs = _list_slugs()
    users_zones = zones.get("users", {})

    if not slugs:
        print("No users found. Ensure athlete configs are seeded in the DB.")
        return 1

    any_updated = False

    for slug in slugs:
        if target_slug and slug != target_slug:
            continue

        adata = athlete_store.load(slug)
        if not adata:
            continue

        print(f"\n--- Zones for {slug} ---")

        if slug not in users_zones:
            print(f"  WARN: No zone config for {slug} in zones.yaml -- skipping")
            continue

        if process_user(slug, adata, users_zones[slug]):
            any_updated = True

    if any_updated:
        save_yaml(ZONES_PATH, zones)
        print(f"\nZones updated in {ZONES_PATH}")
    else:
        print("\nNo threshold data available.")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
