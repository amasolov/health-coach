"""
Per-user Garmin Connect authentication with OAuth token caching.

Token store layout (DB-first, file-fallback):
  - DB: credentials table (cred_type='garmin_oauth', garth base64 string)
  - HA addon: /data/garmin/{slug}/
  - Local dev: .garmin_tokens/{slug}/

Typical flow (orchestrated by MCP tools):
  1. Try loading cached tokens  (try_cached_login)
  2. If expired, login with creds (start_login)
  3. If MFA required, finish with code (finish_mfa_login)
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
)
from garth.exc import GarthException, GarthHTTPError

log = logging.getLogger(__name__)

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


def _resolve_user_id(slug: str) -> int | None:
    """Look up user.id from slug. Returns None if not found."""
    try:
        from scripts.db_pool import get_conn
        conn = get_conn()
        try:
            cur = conn.cursor()
            cur.execute("SELECT id FROM users WHERE slug = %s", (slug,))
            row = cur.fetchone()
            return row[0] if row else None
        finally:
            conn.close()
    except Exception:
        return None


def _save_tokens(slug: str, client: Garmin) -> None:
    """Persist Garmin OAuth tokens to both file and credential store."""
    client.garth.dump(str(_token_dir(slug)))

    try:
        token_str = client.garth.dumps()
        uid = _resolve_user_id(slug)
        if uid is not None:
            from scripts.credential_store import put_credential
            put_credential("garmin_oauth", {"token": token_str}, user_id=uid)
    except Exception:
        log.debug("garmin_auth: DB credential save failed", exc_info=True)


def _load_from_garth_str(token_str: str) -> Garmin | None:
    """Restore a Garmin client from a garth base64 token string."""
    try:
        with tempfile.TemporaryDirectory() as td:
            import garth
            garth.client.loads(token_str)
            garth.client.dump(td)
            client = Garmin()
            client.login(td)
            return client
    except Exception:
        log.debug("garmin_auth: garth.loads() restore failed", exc_info=True)
        return None


def try_cached_login(slug: str) -> Garmin | None:
    """Attempt to login using cached OAuth tokens. Returns a client or None.

    Checks the credential store (DB) first, then falls back to the
    local token directory.
    """
    uid = _resolve_user_id(slug)
    if uid is not None:
        try:
            from scripts.credential_store import get_credential
            cred = get_credential("garmin_oauth", user_id=uid)
            if cred and cred.get("token"):
                client = _load_from_garth_str(cred["token"])
                if client is not None:
                    return client
        except Exception:
            log.debug("garmin_auth: DB credential load failed", exc_info=True)

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

        _save_tokens(slug, client)
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
        _save_tokens(slug, client)
        return ("ok", client)
    except GarminConnectAuthenticationError as e:
        return (f"error: MFA failed -- {e}", None)
    except (GarthHTTPError, GarthException) as e:
        return (f"error: {e}", None)


def get_auth_status(slug: str) -> dict:
    """Check the authentication status for a user."""
    uid = _resolve_user_id(slug)
    if uid is not None:
        try:
            from scripts.credential_store import get_credential
            cred = get_credential("garmin_oauth", user_id=uid)
            if cred and cred.get("token"):
                client = _load_from_garth_str(cred["token"])
                if client is not None:
                    return {"authenticated": True, "store": "db"}
        except Exception:
            pass

    token_path = _token_dir(slug)
    token_files = list(token_path.glob("*.json"))

    if not token_files:
        return {"authenticated": False, "reason": "No cached tokens"}

    client = try_cached_login(slug)
    if client:
        return {"authenticated": True, "token_dir": str(token_path)}

    return {"authenticated": False, "reason": "Cached tokens expired or invalid"}
