#!/usr/bin/env python3
"""
Probe the iFit API to discover workout library endpoints.

Uses techniques from open-source ifit-garmin-sync and ifitsync projects.
Tries both web-session and mobile-app auth flows to work around WAF blocks.

Usage:
    python scripts/ifit_probe.py
    # Reads IFIT_EMAIL, IFIT_PASSWORD, IFIT_PROFILE from .env or prompts
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from getpass import getpass

import httpx
from dotenv import load_dotenv

load_dotenv()

UA_BROWSER = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
UA_MOBILE = (
    "Mozilla/5.0 (Linux; Android 6.0; Google Build/MRA58K; wv) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/74.0.3729.186 Safari/537.36"
)

IFIT_LOGIN_URL = "https://www.ifit.com/web-api/login"
IFIT_OAUTH_URL = "https://api.ifit.com/oauth/token"
IFIT_SETTINGS_URL = "https://www.ifit.com/settings/apps"


def try_web_login(email: str, password: str) -> httpx.Client | None:
    """Attempt login via www.ifit.com/web-api/login with browser-like headers."""
    client = httpx.Client(timeout=30, follow_redirects=True)
    client.headers.update({
        "User-Agent": UA_BROWSER,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Origin": "https://www.ifit.com",
        "Referer": "https://www.ifit.com/login",
    })

    print("  [1] Web login (browser UA)...")
    resp = client.post(IFIT_LOGIN_URL, json={
        "email": email,
        "password": password,
        "rememberMe": True,
    })
    print(f"      Status: {resp.status_code}")

    if resp.status_code == 200:
        data = resp.json()
        print(f"      User ID: {data.get('userId', '?')}")
        print(f"      Keys: {list(data.keys())}")
        return client

    print(f"      Body: {resp.text[:200]}")
    client.close()
    return None


def try_mobile_login(email: str, password: str) -> httpx.Client | None:
    """Attempt login with mobile-app-style headers (may bypass WAF)."""
    client = httpx.Client(timeout=30, follow_redirects=True)
    client.headers.update({
        "User-Agent": UA_MOBILE,
        "Content-Type": "application/json",
        "Accept": "*/*",
        "Origin": "https://onboarding-webview.ifit.com",
        "Referer": "https://onboarding-webview.ifit.com/0.27.0/index.html?page=login-email",
        "X-Requested-With": "com.ifit.wolf",
        "Accept-Encoding": "gzip, deflate",
        "Accept-Language": "en-US,en;q=0.9",
    })

    print("  [2] Web login (mobile app UA)...")
    resp = client.post(IFIT_LOGIN_URL, json={
        "email": email,
        "password": password,
        "rememberMe": False,
    })
    print(f"      Status: {resp.status_code}")

    if resp.status_code == 200:
        data = resp.json()
        print(f"      User ID: {data.get('userId', '?')}")
        print(f"      Keys: {list(data.keys())}")
        return client

    print(f"      Body: {resp.text[:200]}")
    client.close()
    return None


def get_oauth_token(
    email: str, password: str, web_client: httpx.Client | None
) -> str | None:
    """Get an OAuth access token via the API.

    Strategy: scrape clientId/clientSecret from the settings page (requires
    authenticated web session), then exchange credentials for a token.
    """

    client_id = client_secret = None

    if web_client:
        print("  Scraping client credentials from settings page...")
        resp = web_client.get(IFIT_SETTINGS_URL)
        if resp.status_code == 200:
            cid = re.findall(r"['\"]clientId['\"]:\s*['\"]([^'\"]+)['\"]", resp.text)
            csec = re.findall(r"['\"]clientSecret['\"]:\s*['\"]([^'\"]+)['\"]", resp.text)
            # Also try the ifitsync regex style
            if not cid:
                cid = re.findall(r"'clientId':'([^']+)'", resp.text)
            if not csec:
                csec = re.findall(r"'clientSecret':'([^']+)'", resp.text)
            if cid and csec:
                client_id, client_secret = cid[0], csec[0]
                print(f"      client_id: {client_id[:12]}...")
                print(f"      client_secret: {client_secret[:8]}...")

    if not client_id:
        print("  Could not scrape client credentials, skipping OAuth")
        return None

    print("  Requesting OAuth token...")
    resp = httpx.post(IFIT_OAUTH_URL, json={
        "grant_type": "password",
        "username": email,
        "password": password,
        "client_id": client_id,
        "client_secret": client_secret,
    }, headers={
        "Content-Type": "application/json",
        "User-Agent": UA_MOBILE,
        "X-Requested-With": "com.ifit.wolf",
    }, timeout=30)
    print(f"      Status: {resp.status_code}")

    if resp.status_code == 200:
        data = resp.json()
        print(f"      Token keys: {list(data.keys())}")
        token = data.get("access_token")
        # Cache credentials for future use
        cache = {
            "access_token": token,
            "refresh_token": data.get("refresh_token"),
            "expires_in": data.get("expires_in"),
            "client_id": client_id,
            "client_secret": client_secret,
            "timestamp": time.time(),
        }
        cache_path = os.path.join(os.path.dirname(__file__), "..", ".ifit_token.json")
        with open(cache_path, "w") as f:
            json.dump(cache, f, indent=2)
        print(f"      Token cached to .ifit_token.json")
        return token

    print(f"      Body: {resp.text[:200]}")
    return None


def try_cached_token() -> str | None:
    """Load a previously cached OAuth token if still valid."""
    cache_path = os.path.join(os.path.dirname(__file__), "..", ".ifit_token.json")
    if not os.path.exists(cache_path):
        return None
    with open(cache_path) as f:
        data = json.load(f)
    elapsed = time.time() - data.get("timestamp", 0)
    expires = data.get("expires_in", 0)
    if elapsed < expires - 60:
        print(f"  Using cached token ({int(expires - elapsed)}s remaining)")
        return data["access_token"]
    # Try refresh
    if data.get("refresh_token") and data.get("client_id"):
        print("  Cached token expired, trying refresh...")
        resp = httpx.post(IFIT_OAUTH_URL, json={
            "grant_type": "refresh_token",
            "refresh_token": data["refresh_token"],
            "client_id": data["client_id"],
            "client_secret": data["client_secret"],
        }, headers={
            "Content-Type": "application/json",
            "User-Agent": UA_MOBILE,
            "X-Requested-With": "com.ifit.wolf",
        }, timeout=30)
        if resp.status_code == 200:
            new_data = resp.json()
            data["access_token"] = new_data["access_token"]
            data["refresh_token"] = new_data.get("refresh_token", data["refresh_token"])
            data["expires_in"] = new_data.get("expires_in", expires)
            data["timestamp"] = time.time()
            with open(cache_path, "w") as f:
                json.dump(data, f, indent=2)
            print(f"      Refreshed OK")
            return data["access_token"]
        print(f"      Refresh failed: {resp.status_code}")
    return None


def probe_endpoints(web_client: httpx.Client | None, token: str | None) -> list[str]:
    """Try all known and guessed endpoints. Returns URLs that returned 200."""

    api_headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": UA_MOBILE,
    }
    if token:
        api_headers["Authorization"] = f"Bearer {token}"

    endpoints = [
        # Web session endpoints (cookie auth)
        ("WEB", "https://www.ifit.com/me/workouts"),
        ("WEB", "https://www.ifit.com/web-api/workouts"),
        ("WEB", "https://www.ifit.com/web-api/library"),
        ("WEB", "https://www.ifit.com/web-api/users"),
        ("WEB", "https://www.ifit.com/web-api/family"),
        ("WEB", "https://www.ifit.com/web-api/me/family"),

        # OAuth API -- core endpoints from ifitsync/ifit-garmin-sync
        ("API", "https://api.ifit.com/v1/me"),
        ("API", "https://api.ifit.com/v1/activity_logs"),
        ("API", "https://api.ifit.com/v1/activity_logs?perPage=3"),
        ("API", "https://api.ifit.com/v1/workouts"),
        ("API", "https://api.ifit.com/v1/workouts?perPage=3"),
        ("API", "https://api.ifit.com/v1/programs"),
        ("API", "https://api.ifit.com/v1/programs?perPage=3"),
        ("API", "https://api.ifit.com/v1/trainers"),
        ("API", "https://api.ifit.com/v1/categories"),

        # Guesses for workout library / content
        ("API", "https://api.ifit.com/v1/library"),
        ("API", "https://api.ifit.com/v1/library/workouts"),
        ("API", "https://api.ifit.com/v1/content"),
        ("API", "https://api.ifit.com/v1/search"),
        ("API", "https://api.ifit.com/v1/search/workouts"),
        ("API", "https://api.ifit.com/v1/me/workouts"),
        ("API", "https://api.ifit.com/v1/me/library"),
        ("API", "https://api.ifit.com/v1/me/favorites"),
        ("API", "https://api.ifit.com/v1/me/programs"),

        # V2 guesses
        ("API", "https://api.ifit.com/v2/workouts"),
        ("API", "https://api.ifit.com/v2/library"),
        ("API", "https://api.ifit.com/v2/activity_logs"),
    ]

    print(f"\n{'='*70}")
    print(f"Probing {len(endpoints)} endpoints...")
    print(f"{'='*70}")

    hits = []

    for source, url in endpoints:
        try:
            if source == "WEB" and web_client:
                resp = web_client.get(url)
            elif source == "API":
                resp = httpx.get(url, headers=api_headers, timeout=15)
            else:
                continue

            status = resp.status_code
            ct = resp.headers.get("content-type", "")
            preview = ""

            if status == 200:
                if "json" in ct:
                    try:
                        data = resp.json()
                        if isinstance(data, dict):
                            preview = f"keys={list(data.keys())[:8]}"
                        elif isinstance(data, list):
                            preview = f"list[{len(data)} items]"
                        else:
                            preview = str(data)[:100]
                    except Exception:
                        preview = resp.text[:100]
                elif "html" in ct:
                    preview = f"HTML ({len(resp.text)} chars)"
                else:
                    preview = f"type={ct}, len={len(resp.text)}"
                hits.append(url)
                marker = "*** HIT ***"
            elif status in (301, 302, 307, 308):
                marker = "redirect"
                preview = f"-> {resp.headers.get('location', '?')}"
            elif status == 401:
                marker = "auth required"
            elif status == 403:
                marker = "forbidden"
            elif status == 404:
                marker = "not found"
            else:
                marker = f"[{status}]"
                preview = resp.text[:80]

            print(f"  [{status}] {marker:15s} {url}")
            if preview:
                print(f"        {preview[:140]}")

        except Exception as e:
            print(f"  [ERR] {url}: {e}")

    print(f"\n{'='*70}")
    print(f"HITS ({len(hits)}):")
    for url in hits:
        print(f"  {url}")

    return hits


def deep_dive(token: str | None, urls: list[str]) -> None:
    """Print full JSON from interesting endpoints."""
    if not token:
        return

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": UA_MOBILE,
    }

    # Always probe these + any hits
    must_check = [
        "https://api.ifit.com/v1/me",
        "https://api.ifit.com/v1/activity_logs?perPage=2",
    ]
    to_check = list(dict.fromkeys(must_check + urls))

    for url in to_check:
        print(f"\n{'='*70}")
        print(f"DEEP DIVE: {url}")
        print(f"{'='*70}")
        try:
            resp = httpx.get(url, headers=headers, timeout=30)
            print(f"Status: {resp.status_code}")
            if resp.status_code == 200:
                ct = resp.headers.get("content-type", "")
                if "json" in ct:
                    print(json.dumps(resp.json(), indent=2, default=str)[:3000])
                else:
                    print(f"({ct})")
                    print(resp.text[:1000])
            else:
                print(resp.text[:500])
        except Exception as e:
            print(f"ERROR: {e}")


def try_auth0_token() -> str | None:
    """Try to get a token via the Auth0 PKCE flow (ifit_auth.py)."""
    try:
        from ifit_auth import get_valid_token
        return get_valid_token()
    except ImportError:
        pass

    # Fallback: try importing from scripts dir
    script_dir = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, script_dir)
    try:
        from ifit_auth import get_valid_token
        return get_valid_token()
    except ImportError:
        return None


def main() -> int:
    email = os.environ.get("IFIT_EMAIL", "")
    password = os.environ.get("IFIT_PASSWORD", "")
    profile = os.environ.get("IFIT_PROFILE", "Alexey")

    print(f"\niFit API Probe (profile={profile})")
    print(f"{'='*70}")

    # Phase 1: Check for Auth0-based token (preferred)
    print("\nPhase 1: Auth0 token")
    token = try_auth0_token()
    if token:
        print("  Using Auth0 token from ifit_auth.py")

    # Phase 2: Check for legacy cached token
    if not token:
        print("\nPhase 2: Legacy token cache")
        token = try_cached_token()

    # Phase 3: Web session login (try two UA strategies)
    web_client = None
    if not token:
        if not email:
            email = input("iFit email: ").strip()
        if not password:
            password = getpass("iFit password: ")
        if not email or not password:
            print("ERROR: Need email and password")
            return 1

        print("\nPhase 3: Web login")
        web_client = try_web_login(email, password)
        if not web_client:
            web_client = try_mobile_login(email, password)

        # Phase 4: Legacy OAuth token
        if not token:
            print("\nPhase 4: Legacy OAuth token")
            token = get_oauth_token(email, password, web_client)

    if not token and not web_client:
        print("\n*** WARNING: No auth succeeded. Results will be limited. ***")
        print("    Run first:  python scripts/ifit_auth.py")
        print("    Or if on VPN (including Tailscale), try disconnecting.")

    # Phase 5: Probe endpoints
    hits = probe_endpoints(web_client, token)

    # Phase 6: Deep dive into hits
    if token:
        deep_dive(token, [u for u in hits if "api.ifit.com" in u])

    if web_client:
        web_client.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
