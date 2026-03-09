#!/usr/bin/env python3
"""
Log into iFit via a real headless browser (Playwright) to bypass WAF.

Extracts cookies and OAuth credentials, then probes the API.
Saves the token to .ifit_token.json for use by other scripts.

Usage:
    python scripts/ifit_browser_login.py
"""

from __future__ import annotations

import json
import os
import re
import sys
import time

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

load_dotenv()


def main() -> int:
    email = os.environ.get("IFIT_EMAIL", "")
    password = os.environ.get("IFIT_PASSWORD", "")
    profile = os.environ.get("IFIT_PROFILE", "Alexey")

    if not email or not password:
        print("ERROR: Set IFIT_EMAIL and IFIT_PASSWORD in .env")
        return 1

    print(f"iFit Browser Login (profile={profile})")
    print("=" * 70)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()

        # Capture network requests to find tokens
        tokens_found: dict = {}
        def handle_response(response):
            url = response.url
            if "oauth/token" in url or "access_token" in url:
                try:
                    data = response.json()
                    if "access_token" in data:
                        tokens_found["oauth"] = data
                        print(f"  [CAPTURED] OAuth token from {url}")
                except Exception:
                    pass
            if "web-api/login" in url and response.status == 200:
                try:
                    data = response.json()
                    tokens_found["web_login"] = data
                    print(f"  [CAPTURED] Web login response from {url}")
                except Exception:
                    pass

        page.on("response", handle_response)

        # Step 1: Navigate to login page
        print("\nStep 1: Loading login page...")
        page.goto("https://www.ifit.com/login", wait_until="networkidle", timeout=30000)
        print(f"  URL: {page.url}")
        print(f"  Title: {page.title()}")

        # Check if we're on Auth0 or iFit login
        current_url = page.url
        print(f"  Current URL: {current_url}")

        # Take a screenshot for debugging
        screenshot_path = os.path.join(os.path.dirname(__file__), "..", ".ifit_login_debug.png")
        page.screenshot(path=screenshot_path)
        print(f"  Screenshot saved to .ifit_login_debug.png")

        # Step 2: Try to fill in credentials
        print("\nStep 2: Looking for login form...")

        # Try to find email/password inputs
        page.wait_for_timeout(2000)

        # Log the page content for debugging
        html_content = page.content()
        inputs = page.query_selector_all("input")
        print(f"  Found {len(inputs)} input elements")
        for inp in inputs:
            inp_type = inp.get_attribute("type") or "text"
            inp_name = inp.get_attribute("name") or inp.get_attribute("id") or "?"
            inp_placeholder = inp.get_attribute("placeholder") or ""
            print(f"    - type={inp_type} name={inp_name} placeholder={inp_placeholder}")

        # Try various selectors for email field
        email_selectors = [
            'input[name="email"]',
            'input[type="email"]',
            'input[name="username"]',
            'input[id="username"]',
            'input[placeholder*="email" i]',
            'input[placeholder*="mail" i]',
        ]
        email_input = None
        for sel in email_selectors:
            email_input = page.query_selector(sel)
            if email_input:
                print(f"  Found email input: {sel}")
                break

        password_selectors = [
            'input[name="password"]',
            'input[type="password"]',
            'input[id="password"]',
        ]
        password_input = None
        for sel in password_selectors:
            password_input = page.query_selector(sel)
            if password_input:
                print(f"  Found password input: {sel}")
                break

        if email_input and password_input:
            print("\nStep 3: Filling credentials...")
            email_input.fill(email)
            password_input.fill(password)
            page.wait_for_timeout(500)

            # Find and click submit button
            submit_selectors = [
                'button[type="submit"]',
                'button:has-text("Log In")',
                'button:has-text("Sign In")',
                'button:has-text("Continue")',
                'input[type="submit"]',
            ]
            for sel in submit_selectors:
                btn = page.query_selector(sel)
                if btn:
                    print(f"  Clicking: {sel}")
                    btn.click()
                    break

            print("  Waiting for navigation...")
            page.wait_for_timeout(5000)
            page.wait_for_load_state("networkidle", timeout=15000)
            print(f"  URL after login: {page.url}")

            # Dismiss cookie consent overlay if present
            for dismiss_sel in [
                "#onetrust-accept-btn-handler",
                'button:has-text("Accept")',
                'button:has-text("Accept All")',
                ".onetrust-close-btn-handler",
            ]:
                dismiss_btn = page.query_selector(dismiss_sel)
                if dismiss_btn:
                    print(f"  Dismissing cookie consent: {dismiss_sel}")
                    dismiss_btn.click(force=True)
                    page.wait_for_timeout(1000)
                    break

            # Screenshot after login
            page.screenshot(path=screenshot_path)
            print(f"  Screenshot updated")

            # Check for family/profile selection
            page_text = page.inner_text("body")
            if profile.lower() in page_text.lower():
                print(f"\nStep 3b: Looking for profile '{profile}'...")
                profile_link = page.query_selector(f'text="{profile}"')
                if not profile_link:
                    profile_link = page.query_selector(f':text("{profile}")')
                if profile_link:
                    print(f"  Found profile link, clicking...")
                    profile_link.click(force=True)
                    page.wait_for_timeout(3000)
                    page.wait_for_load_state("networkidle", timeout=15000)
                    print(f"  URL after profile: {page.url}")
        else:
            print("  Could not find login form inputs!")
            # Maybe it's an Auth0 Universal Login - check for tabs
            tabs = page.query_selector_all('[role="tab"]')
            if tabs:
                print(f"  Found {len(tabs)} tabs:")
                for tab in tabs:
                    print(f"    - {tab.inner_text()}")
            links = page.query_selector_all("a")
            print(f"  Found {len(links)} links")
            for link in links[:10]:
                href = link.get_attribute("href") or ""
                text = link.inner_text().strip()
                if text:
                    print(f"    - [{text}] -> {href[:80]}")

        # Step 4: Extract useful data
        print("\nStep 4: Extracting credentials...")

        # Get cookies
        cookies = context.cookies()
        ifit_cookies = [c for c in cookies if "ifit" in c.get("domain", "")]
        print(f"  Total cookies: {len(cookies)}")
        print(f"  iFit cookies: {len(ifit_cookies)}")
        for c in ifit_cookies:
            print(f"    - {c['name']} ({c['domain']}) = {str(c['value'])[:30]}...")

        # Try to get client credentials from settings page
        print("\n  Navigating to settings/apps for client credentials...")
        page.goto("https://www.ifit.com/settings/apps", wait_until="networkidle", timeout=15000)
        settings_html = page.content()

        client_id = client_secret = None
        cid_match = re.findall(r"['\"]clientId['\"]:\s*['\"]([^'\"]+)['\"]", settings_html)
        csec_match = re.findall(r"['\"]clientSecret['\"]:\s*['\"]([^'\"]+)['\"]", settings_html)
        if not cid_match:
            cid_match = re.findall(r"'clientId':'([^']+)'", settings_html)
        if not csec_match:
            csec_match = re.findall(r"'clientSecret':'([^']+)'", settings_html)

        if cid_match and csec_match:
            client_id = cid_match[0]
            client_secret = csec_match[0]
            print(f"  client_id: {client_id}")
            print(f"  client_secret: {client_secret[:8]}...")

        # Step 5: If we have client creds, get OAuth token
        if client_id and client_secret:
            print("\nStep 5: Getting OAuth token...")
            import httpx
            resp = httpx.post("https://api.ifit.com/oauth/token", json={
                "grant_type": "password",
                "username": email,
                "password": password,
                "client_id": client_id,
                "client_secret": client_secret,
            }, timeout=30)
            print(f"  Status: {resp.status_code}")
            if resp.status_code == 200:
                token_data = resp.json()
                print(f"  Token keys: {list(token_data.keys())}")
                cache = {
                    "access_token": token_data["access_token"],
                    "refresh_token": token_data.get("refresh_token"),
                    "expires_in": token_data.get("expires_in"),
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "timestamp": time.time(),
                }
                cache_path = os.path.join(os.path.dirname(__file__), "..", ".ifit_token.json")
                with open(cache_path, "w") as f:
                    json.dump(cache, f, indent=2)
                print(f"  Token saved to .ifit_token.json")

                # Quick test
                me_resp = httpx.get("https://api.ifit.com/v1/me", headers={
                    "Authorization": f"Bearer {token_data['access_token']}",
                }, timeout=15)
                print(f"\n  GET /v1/me: {me_resp.status_code}")
                if me_resp.status_code == 200:
                    me = me_resp.json()
                    print(f"    {json.dumps(me, indent=2, default=str)[:1000]}")

                logs_resp = httpx.get("https://api.ifit.com/v1/activity_logs?perPage=3", headers={
                    "Authorization": f"Bearer {token_data['access_token']}",
                }, timeout=15)
                print(f"\n  GET /v1/activity_logs?perPage=3: {logs_resp.status_code}")
                if logs_resp.status_code == 200:
                    print(f"    {json.dumps(logs_resp.json(), indent=2, default=str)[:2000]}")
            else:
                print(f"  Body: {resp.text[:300]}")

        # Also check if we captured any tokens from network
        if tokens_found:
            print("\nCaptured tokens from network:")
            for key, val in tokens_found.items():
                print(f"  {key}: {json.dumps(val, indent=2, default=str)[:500]}")

        browser.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
