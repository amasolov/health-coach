"""
Per-user Garmin Connect authentication with OAuth token caching.

Token store layout:
  - HA addon: /data/garmin/{slug}/
  - Local dev: .garmin_tokens/{slug}/

Typical flow (orchestrated by MCP tools):
  1. Try loading cached tokens  (try_cached_login)
  2. If expired, login with creds (start_login)
  3. If MFA required, finish with code (finish_mfa_login)
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
)
from garth.exc import GarthException, GarthHTTPError

# Pending MFA sessions keyed by user slug
_MFA_SESSIONS: dict[str, tuple[Garmin, Any]] = {}


def _token_dir(slug: str) -> Path:
    """Resolve the token storage directory for a user."""
    if Path("/data").is_dir():
        base = Path("/data/garmin")
    else:
        base = Path(__file__).resolve().parent.parent / ".garmin_tokens"
    d = base / slug
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    return d


def try_cached_login(slug: str) -> Garmin | None:
    """Attempt to login using cached OAuth tokens. Returns a client or None."""
    token_path = _token_dir(slug)
    token_files = list(token_path.glob("*.json"))
    if not token_files:
        return None
    try:
        client = Garmin()
        client.login(str(token_path))
        return client
    except (
        FileNotFoundError,
        GarthHTTPError,
        GarthException,
        GarminConnectAuthenticationError,
        GarminConnectConnectionError,
    ):
        return None


def start_login(
    slug: str, email: str, password: str
) -> tuple[str, Garmin | None]:
    """
    Start a login flow with email/password.

    Returns:
      ("ok", client)         -- login succeeded, no MFA needed
      ("needs_mfa", None)    -- MFA code required, call finish_mfa_login next
      ("error: ...", None)   -- something went wrong
    """
    try:
        client = Garmin(email=email, password=password, is_cn=False, return_on_mfa=True)
        result1, result2 = client.login()

        if result1 == "needs_mfa":
            _MFA_SESSIONS[slug] = (client, result2)
            return ("needs_mfa", None)

        # No MFA needed -- save tokens
        client.garth.dump(str(_token_dir(slug)))
        return ("ok", client)

    except GarminConnectAuthenticationError as e:
        return (f"error: Authentication failed -- {e}", None)
    except GarminConnectConnectionError as e:
        return (f"error: Connection failed -- {e}", None)
    except (GarthHTTPError, GarthException) as e:
        return (f"error: {e}", None)


def finish_mfa_login(slug: str, mfa_code: str) -> tuple[str, Garmin | None]:
    """
    Complete an MFA login with the code the user received.

    Returns:
      ("ok", client)       -- success
      ("error: ...", None)  -- failure
    """
    session = _MFA_SESSIONS.pop(slug, None)
    if not session:
        return ("error: No pending MFA session for this user. Call start_login first.", None)

    client, mfa_data = session
    try:
        client.resume_login(mfa_data, mfa_code)
        client.garth.dump(str(_token_dir(slug)))
        return ("ok", client)
    except GarminConnectAuthenticationError as e:
        return (f"error: MFA failed -- {e}", None)
    except (GarthHTTPError, GarthException) as e:
        return (f"error: {e}", None)


def get_auth_status(slug: str) -> dict:
    """Check the authentication status for a user."""
    token_path = _token_dir(slug)
    token_files = list(token_path.glob("*.json"))

    if not token_files:
        return {"authenticated": False, "reason": "No cached tokens"}

    client = try_cached_login(slug)
    if client:
        return {"authenticated": True, "token_dir": str(token_path)}

    return {"authenticated": False, "reason": "Cached tokens expired or invalid"}
