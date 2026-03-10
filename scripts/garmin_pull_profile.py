#!/usr/bin/env python3
"""
Pull Garmin profile data and merge into the athlete config (DB).

Requires a valid cached token (run `make garmin-login` first).

Usage:
    python scripts/garmin_pull_profile.py
    python scripts/garmin_pull_profile.py --dry-run   # show what would change
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

def _load_dotenv(path: Path) -> None:
    """Best-effort .env loading (Taskfile handles this, but support standalone too)."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip())

if not os.environ.get("USER_SLUG"):
    _load_dotenv(Path(_PROJECT_ROOT) / ".env")

from scripts.garmin_auth import try_cached_login
from scripts.garmin_fetch import fetch_garmin_profile, refresh_thresholds


def main() -> int:
    parser = argparse.ArgumentParser(description="Pull Garmin profile into athlete config")
    parser.add_argument("--slug", default=os.environ.get("USER_SLUG", "alexeym"))
    parser.add_argument("--dry-run", action="store_true", help="Show data without writing")
    args = parser.parse_args()

    client = try_cached_login(args.slug)
    if not client:
        print("Not authenticated. Run: task garmin:login")
        return 1

    print(f"Fetching Garmin profile for '{args.slug}'...")
    result = fetch_garmin_profile(args.slug, client)

    print("\n=== Fetched Data ===")
    print(json.dumps(result["fetched"], indent=2, default=str))

    print("\n=== Data Sources ===")
    for field, source in sorted(result["sources"].items()):
        print(f"  {field}: {source}")

    if result["missing"]:
        print("\n=== Still Missing ===")
        for m in result["missing"]:
            print(f"  {m['field']}: {m['hint']}")

    if args.dry_run:
        print("\n[DRY RUN] No changes written.")
        return 0

    refresh = refresh_thresholds(
        args.slug, result["fetched"],
        fetched_sources=result.get("sources"),
    )

    if refresh["updated"]:
        print(f"\n=== Updated in athlete config ===")
        for field, value in refresh["updated"].items():
            print(f"  {field}: {value}")
    else:
        print("\nAll thresholds up to date (no changes).")

    if refresh.get("advisories"):
        print(f"\n=== Advisories ===")
        for adv in refresh["advisories"]:
            print(f"  {adv['message']}")

    if refresh.get("garmin_latest"):
        print(f"\n=== Garmin Latest Values ===")
        for field, value in refresh["garmin_latest"].items():
            print(f"  {field}: {value}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
