#!/usr/bin/env python3
"""
iFit authentication via the gateway API (cockatoo service).

Uses the app's OAuth2 client credentials to refresh tokens.
Initial tokens are obtained via MITM capture of the iFit mobile app;
subsequent refreshes work indefinitely without re-capture.

Token lifecycle: access_token lasts 7 days, refresh_token rotates on use.

Usage:
    python scripts/ifit_auth.py          # refresh and verify token
    python scripts/ifit_auth.py --check  # just verify current token
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time

import httpx
from dotenv import load_dotenv

load_dotenv()

_LOCAL_TOKEN_FILE = os.path.join(os.path.dirname(__file__), "..", ".ifit_token.json")
TOKEN_FILE = os.environ.get(
    "IFIT_TOKEN_FILE",
    "/config/healthcoach/.ifit_token.json" if os.path.isdir("/config/healthcoach") else _LOCAL_TOKEN_FILE,
)

REFRESH_URL = "https://gateway.ifit.com/cockatoo/v2/login/refresh"
GATEWAY_BASE = "https://gateway.ifit.com"
API_BASE = "https://api.ifit.com"


def _load_cached() -> dict | None:
    if not os.path.exists(TOKEN_FILE):
        return None
    with open(TOKEN_FILE) as f:
        return json.load(f)


def _save_cache(data: dict) -> None:
    with open(TOKEN_FILE, "w") as f:
        json.dump(data, f, indent=2)


def _basic_header(data: dict) -> str:
    cid = data.get("app_client_id", "")
    csecret = data.get("app_client_secret", "")
    return base64.b64encode(f"{cid}:{csecret}".encode()).decode()


def refresh_token(data: dict) -> dict | None:
    """Exchange a refresh token for a new access + refresh token pair."""
    rt = data.get("refresh_token")
    basic = _basic_header(data)
    if not rt or not basic:
        return None

    resp = httpx.post(REFRESH_URL, json={"refresh_token": rt}, headers={
        "Authorization": f"Basic {basic}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "GLSUSRAUTH v10.1.3",
    }, timeout=15)

    if resp.status_code != 200:
        return None

    new_tokens = resp.json()
    data["access_token"] = new_tokens["access_token"]
    data["refresh_token"] = new_tokens["refresh_token"]
    data["expires_in"] = new_tokens.get("expires_in", 604800)
    data["timestamp"] = time.time()
    _save_cache(data)
    return data


def get_valid_token() -> str | None:
    """Return a valid access token, refreshing if needed."""
    data = _load_cached()
    if not data:
        return None

    elapsed = time.time() - data.get("timestamp", 0)
    expires = data.get("expires_in", 0)

    if elapsed < expires - 300:
        return data["access_token"]

    refreshed = refresh_token(data)
    if refreshed:
        return refreshed["access_token"]

    return None


def get_auth_headers() -> dict[str, str]:
    """Return headers dict with a valid Bearer token."""
    token = get_valid_token()
    if not token:
        raise RuntimeError("No valid iFit token. Capture a new one via MITM proxy.")
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }


def check_token() -> bool:
    token = get_valid_token()
    if not token:
        print("No valid iFit token.")
        return False

    resp = httpx.get(f"{API_BASE}/v1/me", headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }, timeout=15)

    if resp.status_code == 200:
        me = resp.json()
        name = f"{me.get('firstname', '')} {me.get('lastname', '')}".strip()
        print(f"  Authenticated as: {name or me.get('email', '?')}")
        print(f"  Premium: {me.get('premium', False)}")
        return True

    print(f"  Token invalid (status {resp.status_code})")
    return False


def main() -> int:
    if "--check" in sys.argv:
        return 0 if check_token() else 1

    data = _load_cached()
    if not data:
        print("No .ifit_token.json found.")
        print("Run the MITM proxy capture to obtain initial tokens.")
        return 1

    token = get_valid_token()
    if token:
        print("Token is valid.")
        check_token()
        return 0

    print("Token expired, attempting refresh...")
    refreshed = refresh_token(data)
    if refreshed:
        print("Token refreshed successfully.")
        check_token()
        return 0

    print("Token refresh failed. Re-capture needed via MITM proxy.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
