"""
Custom Apple OAuth provider for Chainlit.

Apple Sign-In uses standard OAuth2 with a few quirks:
  1. The client_secret is a short-lived JWT signed with an ES256 private key
  2. User name is only returned on the FIRST authorization
  3. User info comes from the id_token (no userinfo endpoint)

Required env vars:
  OAUTH_APPLE_CLIENT_ID     - Services ID (e.g. com.example.health-coach)
  OAUTH_APPLE_TEAM_ID       - Apple Developer Team ID (10-char)
  OAUTH_APPLE_KEY_ID        - Key ID for the Sign-In private key
  OAUTH_APPLE_PRIVATE_KEY   - PEM contents of the .p8 file (with newlines)

Optional:
  OAUTH_APPLE_PRIVATE_KEY_FILE - path to .p8 file (used if PRIVATE_KEY is empty)
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

import httpx
import jwt as pyjwt

import chainlit as cl
from chainlit.oauth_providers import OAuthProvider

_TOKEN_URL = "https://appleid.apple.com/auth/token"
_JWKS_URL = "https://appleid.apple.com/auth/keys"


def _load_private_key() -> str:
    """Load the Apple Sign-In private key from env or file."""
    key = os.environ.get("OAUTH_APPLE_PRIVATE_KEY", "")
    if key:
        return key

    key_file = os.environ.get("OAUTH_APPLE_PRIVATE_KEY_FILE", "")
    if key_file and os.path.exists(key_file):
        with open(key_file) as f:
            return f.read()

    return ""


def _generate_client_secret() -> str:
    """Generate a short-lived JWT to use as the Apple client_secret.

    Apple requires the client_secret to be an ES256-signed JWT containing
    the team ID, client ID, and a max 6-month expiry.
    """
    team_id = os.environ.get("OAUTH_APPLE_TEAM_ID", "")
    client_id = os.environ.get("OAUTH_APPLE_CLIENT_ID", "")
    key_id = os.environ.get("OAUTH_APPLE_KEY_ID", "")
    private_key = _load_private_key()

    if not all([team_id, client_id, key_id, private_key]):
        raise ValueError("Apple OAuth not fully configured (missing team/key/client ID or private key)")

    now = int(time.time())
    payload = {
        "iss": team_id,
        "iat": now,
        "exp": now + 86400 * 180,  # 6 months max
        "aud": "https://appleid.apple.com",
        "sub": client_id,
    }
    return pyjwt.encode(
        payload,
        private_key,
        algorithm="ES256",
        headers={"kid": key_id},
    )


class AppleOAuthProvider(OAuthProvider):
    id = "apple"
    env = [
        "OAUTH_APPLE_CLIENT_ID",
        "OAUTH_APPLE_TEAM_ID",
        "OAUTH_APPLE_KEY_ID",
    ]
    authorize_url = "https://appleid.apple.com/auth/authorize"
    authorize_params = {
        "response_type": "code",
        "response_mode": "query",
        "scope": "name email",
    }

    def get_client_id(self) -> str:
        return os.environ.get("OAUTH_APPLE_CLIENT_ID", "")

    async def get_token(self, code: str, url: str) -> str:
        """Exchange the authorization code for tokens.

        Returns the raw token response as a JSON string so get_user_info
        can extract the id_token.
        """
        client_id = self.get_client_id()
        client_secret = _generate_client_secret()

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                _TOKEN_URL,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": url,
                    "client_id": client_id,
                    "client_secret": client_secret,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            resp.raise_for_status()
            return json.dumps(resp.json())

    async def get_user_info(self, token: str) -> tuple[dict[str, Any], cl.User]:
        """Decode the id_token to extract user info.

        Apple doesn't have a userinfo endpoint; all user data is in the
        id_token JWT.  We verify the signature against Apple's JWKS.
        """
        token_data = json.loads(token)
        id_token = token_data.get("id_token", "")

        # Fetch Apple's public keys for verification
        async with httpx.AsyncClient() as client:
            resp = await client.get(_JWKS_URL)
            resp.raise_for_status()
            jwks = resp.json()

        # Decode header to find the right key
        header = pyjwt.get_unverified_header(id_token)
        kid = header.get("kid")

        key_data = None
        for key in jwks.get("keys", []):
            if key.get("kid") == kid:
                key_data = key
                break

        if not key_data:
            raise ValueError("Apple id_token signed with unknown key")

        public_key = pyjwt.algorithms.RSAAlgorithm.from_jwk(key_data)
        claims = pyjwt.decode(
            id_token,
            public_key,
            algorithms=["RS256"],
            audience=os.environ.get("OAUTH_APPLE_CLIENT_ID", ""),
            issuer="https://appleid.apple.com",
        )

        email = claims.get("email", "")
        sub = claims.get("sub", "")

        user = cl.User(
            identifier=email or sub,
            metadata={
                "provider": "apple",
                "sub": sub,
                "email": email,
                "email_verified": claims.get("email_verified", False),
                "image": "",
            },
        )

        return claims, user
