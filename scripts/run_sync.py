#!/usr/bin/env python3
"""
Sync orchestrator.

In the HA addon, iterates over users from USERS_JSON env var.
Locally, uses USER_SLUG from .env.

Data sources:
  - Garmin Connect: activities, vitals, body composition
  - Hevy: strength training (sets, reps, weight)
"""

from __future__ import annotations

import json
import os
import sys
import traceback

from dotenv import load_dotenv
load_dotenv()

from scripts.sync_garmin import sync_user as sync_garmin_user
from scripts.sync_hevy import sync_user as sync_hevy_user


def get_users() -> list[dict]:
    """Get user list from USERS_JSON (addon) or USER_SLUG (local dev)."""
    users_json = os.environ.get("USERS_JSON")
    if users_json:
        return json.loads(users_json)

    slug = os.environ.get("USER_SLUG", "alexey")
    hevy_key = os.environ.get("HEVY_API_KEY", "")
    return [{"slug": slug, "name": slug, "hevy_api_key": hevy_key}]


def _resolve_user_id(slug: str) -> int | None:
    import psycopg2
    conn = psycopg2.connect(
        host=os.environ.get("DB_HOST", "localhost"),
        port=os.environ.get("DB_PORT", "5432"),
        dbname=os.environ.get("DB_NAME", "health"),
        user=os.environ.get("DB_USER", "postgres"),
        password=os.environ.get("DB_PASSWORD", ""),
    )
    try:
        cur = conn.cursor()
        cur.execute("SELECT id FROM users WHERE slug = %s", (slug,))
        row = cur.fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def main() -> int:
    users = get_users()
    errors = 0

    for user in users:
        slug = user["slug"]
        name = user.get("name", slug)
        print(f"\n{'='*60}")
        print(f"Syncing data for {name} ({slug})")
        print(f"{'='*60}")

        user_id = _resolve_user_id(slug)
        if not user_id:
            print(f"  ERROR: User '{slug}' not found in database. Run migrations first.")
            errors += 1
            continue

        # --- Garmin Connect ---
        print(f"\n  [Garmin Connect]")
        try:
            result = sync_garmin_user(slug, user_id)
            if "error" in result:
                print(f"    SKIP: {result['error']}")
            else:
                print(f"    Activities: {result['activities_inserted']} new")
                print(f"    Body comp:  {result['body_comp_inserted']} new")
                print(f"    Vitals:     {result['vitals_inserted']} new")
        except Exception as e:
            print(f"    ERROR: Garmin sync failed: {e}")
            traceback.print_exc()
            errors += 1

        # --- Hevy ---
        hevy_key = user.get("hevy_api_key") or os.environ.get("HEVY_API_KEY", "")
        print(f"\n  [Hevy]")
        if hevy_key:
            try:
                result = sync_hevy_user(slug, user_id, hevy_key)
                if "error" in result:
                    print(f"    SKIP: {result['error']}")
                else:
                    print(f"    Workouts: {result['workouts_inserted']} new ({result['sets_inserted']} sets)")
            except Exception as e:
                print(f"    ERROR: Hevy sync failed: {e}")
                traceback.print_exc()
                errors += 1
        else:
            print(f"    SKIP: No Hevy API key configured")

    if errors:
        print(f"\nSync completed with {errors} error(s)")
        return 1

    print(f"\nSync completed successfully")
    return 0


if __name__ == "__main__":
    sys.exit(main())
