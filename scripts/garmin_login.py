#!/usr/bin/env python3
"""
Interactive Garmin Connect login for local development.

Authenticates with email/password, handles MFA if required, and
caches OAuth tokens in .garmin_tokens/{slug}/ for subsequent use
by garmin_fetch.py, sync_garmin.py, etc.

Usage:
    python scripts/garmin_login.py                  # uses .env defaults
    python scripts/garmin_login.py --slug alexeym   # explicit slug
"""
from __future__ import annotations

import argparse
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

if not os.environ.get("GARMIN_EMAIL"):
    _load_dotenv(Path(_PROJECT_ROOT) / ".env")

from scripts.garmin_auth import try_cached_login, start_login, finish_mfa_login, get_auth_status


def main() -> int:
    parser = argparse.ArgumentParser(description="Garmin Connect login for local dev")
    parser.add_argument("--slug", default=os.environ.get("USER_SLUG", "alexeym"))
    parser.add_argument("--email", default=os.environ.get("GARMIN_EMAIL", ""))
    parser.add_argument("--password", default=os.environ.get("GARMIN_PASSWORD", ""))
    parser.add_argument("--status", action="store_true", help="Check auth status only")
    args = parser.parse_args()

    if args.status:
        status = get_auth_status(args.slug)
        print(f"Auth status for '{args.slug}': {status}")
        return 0 if status.get("authenticated") else 1

    # Try cached tokens first
    print(f"Checking cached tokens for '{args.slug}'...")
    client = try_cached_login(args.slug)
    if client:
        name = getattr(client.garth, "profile", {})
        display = ""
        if hasattr(name, "get"):
            display = name.get("displayName", "")
        print(f"Already authenticated{f' as {display}' if display else ''}. Tokens are valid.")
        return 0

    # Need fresh login
    if not args.email or not args.password:
        print("No cached tokens. Set GARMIN_EMAIL and GARMIN_PASSWORD in .env")
        return 1

    print(f"Logging in as {args.email}...")
    result, client = start_login(args.slug, args.email, args.password)

    if result == "ok":
        print("Login successful! Tokens cached.")
        return 0

    if result == "needs_mfa":
        print("MFA required. Check your email/authenticator for the code.")
        mfa_code = input("Enter MFA code: ").strip()
        if not mfa_code:
            print("No code entered. Aborting.")
            return 1

        result, client = finish_mfa_login(args.slug, mfa_code)
        if result == "ok":
            print("MFA verified! Tokens cached.")
            return 0
        print(f"MFA failed: {result}")
        return 1

    print(f"Login failed: {result}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
