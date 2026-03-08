#!/usr/bin/env python3
"""
Sync orchestrator.
In the HA addon, iterates over users from USERS_JSON env var.
Locally, uses USER_SLUG from .env.

Data sources:
  - Garmin Connect: activities, vitals, body composition
  - Hevy: strength training (sets, reps, weight)

Actual sync implementations (sync_garmin.py, sync_hevy.py) will be
fleshed out once MCP authentication is established and data formats
are confirmed.
"""

from __future__ import annotations

import json
import os
import sys


def get_users() -> list[dict]:
    """Get user list from USERS_JSON (addon) or USER_SLUG (local dev)."""
    users_json = os.environ.get("USERS_JSON")
    if users_json:
        return json.loads(users_json)

    slug = os.environ.get("USER_SLUG", "alexey")
    return [{"slug": slug, "name": slug}]


def main() -> int:
    users = get_users()

    for user in users:
        slug = user["slug"]
        name = user.get("name", slug)
        print(f"\n--- Syncing data for {name} ({slug}) ---")

        # TODO: Call sync_garmin.py with user credentials
        print(f"  [SKIP] Garmin sync not yet implemented for {slug}")

        # TODO: Call sync_hevy.py with user's Hevy API key
        hevy_key = user.get("hevy_api_key")
        if hevy_key:
            print(f"  [SKIP] Hevy sync not yet implemented for {slug}")
        else:
            print(f"  [SKIP] No Hevy API key for {slug}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
