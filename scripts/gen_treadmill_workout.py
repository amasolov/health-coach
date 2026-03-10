#!/usr/bin/env python3
"""
Generate a treadmill workout from a template and athlete zone-speed mapping.

Outputs a step-by-step table with speed, incline, duration, and distance
for manual entry into iFit's web Workout Creator.

Usage:
    python scripts/gen_treadmill_workout.py --template threshold_4x5 --user alexey
    # or via task:
    task treadmill:generate TEMPLATE=threshold_4x5
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES_PATH = ROOT / "config" / "treadmill_templates.yaml"
ATHLETE_PATH = ROOT / "config" / "athlete.yaml"
ZONES_PATH = ROOT / "config" / "zones.yaml"


def load_yaml(path: Path) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def get_user_data(athlete: dict, user_slug: str) -> dict:
    users = athlete.get("users", {})
    if user_slug in users:
        return users[user_slug]
    return athlete


def get_zone_settings(user_data: dict, zone_key: str) -> dict:
    """Look up speed/incline for a zone key in the treadmill mapping."""
    treadmill = user_data.get("treadmill", {})
    zone_map = treadmill.get("zone_speed_map", {})
    hill_map = treadmill.get("hill_map", {})

    if zone_key in zone_map:
        return zone_map[zone_key]
    if zone_key in hill_map:
        return hill_map[zone_key]

    return {"speed_kph": 0.0, "incline_pct": 0.0}


def get_zone_hr_range(user_data: dict, zones_data: dict, zone_key: str) -> str:
    """Try to find the HR range for a given zone key."""
    hr_section = None
    if "users" in zones_data:
        for slug, uzones in zones_data["users"].items():
            if isinstance(uzones, dict) and "heart_rate" in uzones:
                hr_section = uzones["heart_rate"]
                break
    if not hr_section and "heart_rate" in zones_data:
        hr_section = zones_data["heart_rate"]
    if not hr_section:
        return ""

    zone_name_map = {
        "z1_recovery": "Zone 1",
        "z2_aerobic": "Zone 2",
        "z3_tempo": "Zone 3",
        "z4_threshold": "Zone 4",
        "z5_vo2max": "Zone 5a",
    }
    target = zone_name_map.get(zone_key.replace("_hills", ""), "")
    for z in hr_section.get("zones", []):
        if target and target in z.get("name", ""):
            lo = z.get("lower", "?")
            hi = z.get("upper", "?")
            return f"{lo}-{hi} bpm"
    return ""


def generate_workout(template: dict, user_data: dict, zones_data: dict) -> None:
    steps = template["steps"]
    name = template["name"]
    description = template.get("description", "")

    total_time = sum(s["duration_min"] for s in steps)
    total_distance = 0.0

    rows = []
    for i, step in enumerate(steps, 1):
        zone_key = step["zone"]
        settings = get_zone_settings(user_data, zone_key)
        speed = settings["speed_kph"]
        incline = settings["incline_pct"]
        duration = step["duration_min"]
        distance = speed * (duration / 60)
        total_distance += distance

        zone_label = zone_key.replace("_", " ").replace("z", "Z", 1).title()

        rows.append({
            "step": i,
            "phase": step["phase"].capitalize(),
            "zone": zone_label,
            "speed": speed,
            "incline": incline,
            "duration": duration,
            "distance": distance,
        })

    print(f"\n{'=' * 65}")
    print(f"  {name}")
    if description:
        print(f"  {description}")
    print(f"  Total: ~{total_distance:.1f} km | {total_time} min")
    print(f"{'=' * 65}")
    print()

    header = f"{'Step':>4}  {'Phase':<10} {'Zone':<18} {'Speed':>6} {'Incline':>8} {'Time':>6} {'Dist':>8}"
    print(header)
    print("─" * len(header))

    for r in rows:
        mins = int(r["duration"])
        print(
            f"{r['step']:4d}  {r['phase']:<10} {r['zone']:<18} "
            f"{r['speed']:5.1f}  {r['incline']:6.1f}%  "
            f"{mins:3d}:00  {r['distance']:6.2f} km"
        )

    print()

    work_zones = [s["zone"] for s in steps if s["phase"] in ("work", "main") and "recovery" not in s["zone"]]
    if work_zones:
        work_zone = work_zones[0]
        hr_range = get_zone_hr_range(user_data, zones_data, work_zone)
        if hr_range:
            print(f"  Target HR (work): {hr_range}")

    recovery_hr = get_zone_hr_range(user_data, zones_data, "z1_recovery")
    if recovery_hr:
        print(f"  Target HR (recovery): {recovery_hr}")

    print()
    print("  Enter these steps in iFit: ifit.com → Create → Distance Based Workout")
    print()


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate treadmill workout")
    parser.add_argument("--template", "-t", required=True, help="Template key from treadmill_templates.yaml")
    parser.add_argument("--user", "-u", default="alexey", help="User slug")
    args = parser.parse_args()

    templates = load_yaml(TEMPLATES_PATH)
    all_templates = templates.get("templates", {})
    if args.template not in all_templates:
        print(f"ERROR: Template '{args.template}' not found.")
        print(f"Available: {', '.join(all_templates.keys())}")
        return 1

    from scripts.athlete_store import load as _load_athlete
    zones = load_yaml(ZONES_PATH)
    user_data = _load_athlete(args.user) or {}
    template = all_templates[args.template]

    missing_zones = set()
    treadmill = user_data.get("treadmill", {})
    zone_map = {**treadmill.get("zone_speed_map", {}), **treadmill.get("hill_map", {})}
    for step in template["steps"]:
        if step["zone"] not in zone_map:
            missing_zones.add(step["zone"])

    if missing_zones:
        print(f"WARN: No treadmill speed mapping for: {', '.join(sorted(missing_zones))}")
        print("      Using 0.0 km/h. Update treadmill section in config/athlete.yaml.")
        print()

    generate_workout(template, user_data, zones)
    return 0


if __name__ == "__main__":
    sys.exit(main())
