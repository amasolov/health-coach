"""Typed addon configuration backed by Pydantic Settings.

Loads settings from ``/data/options.json`` (Home Assistant addon runtime) with
an automatic fallback to **environment variables** (local dev / CI).  Field
names match ``options.json`` keys exactly; the corresponding env var is the
UPPER_CASE version (e.g. ``db_host`` ↔ ``DB_HOST``).

Usage::

    from scripts.addon_config import config
    print(config.db_host)

The module also provides :func:`write_s6_env` to export all settings as
individual files into ``/run/s6/container_environment/`` so that s6-overlay
services launched via ``with-contenv`` inherit them.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path
from pydantic_settings import BaseSettings, JsonConfigSettingsSource, SettingsConfigDict

_OPTIONS_FILE = Path("/data/options.json")


class AddonConfig(BaseSettings):
    model_config = SettingsConfigDict(
        json_file=str(_OPTIONS_FILE),
        json_file_encoding="utf-8",
        env_prefix="",
        extra="ignore",
    )

    @classmethod
    def settings_customise_sources(
        cls, settings_cls, init_settings, env_settings,
        dotenv_settings, file_secret_settings,
    ):
        """env vars > JSON file > defaults.  Skip JSON when file is absent."""
        if _OPTIONS_FILE.exists():
            return (init_settings, env_settings,
                    JsonConfigSettingsSource(settings_cls),
                    file_secret_settings)
        return (init_settings, env_settings, file_secret_settings)

    # --- Database ---
    db_host: str = "localhost"
    db_port: int = 5432
    db_name: str = "health"
    db_user: str = "postgres"
    db_password: str = ""

    # --- Grafana ---
    grafana_host: str = "a0d7b954-grafana"
    grafana_port: int = 3000
    grafana_api_key: str = ""

    # --- Ports & intervals ---
    mcp_port: int = 8765
    chat_port: int = 8080
    sync_interval_minutes: int = 30

    # --- AI / LLM ---
    openrouter_api_key: str = ""
    openai_api_key: str = ""
    chat_model: str = "anthropic/claude-sonnet-4"
    embedding_api_base: str = ""
    embedding_model: str = "text-embedding-3-small"

    # --- Chat UI ---
    chainlit_url: str = ""
    allow_registration: bool = False

    # --- OAuth (Google) ---
    google_oauth_client_id: str = ""
    google_oauth_client_secret: str = ""

    # --- OAuth (Apple) ---
    apple_oauth_client_id: str = ""
    apple_oauth_team_id: str = ""
    apple_oauth_key_id: str = ""
    apple_oauth_private_key_file: str = ""

    # --- GitHub ---
    github_token: str = ""

    # --- R2 / S3 ---
    r2_account_id: str = ""
    r2_access_key_id: str = ""
    r2_secret_access_key: str = ""
    r2_bucket_name: str = "health-coach-ifit"

    # --- Telegram ---
    telegram_bot_token: str = ""
    telegram_bot_username: str = ""

    # ------------------------------------------------------------------
    # Derived values (not in options.json, computed at load time)
    # ------------------------------------------------------------------

    @property
    def sync_interval(self) -> int:
        """Alias used by the task runner (minutes)."""
        return self.sync_interval_minutes

    @property
    def chainlit_db_url(self) -> str:
        return (
            f"postgresql+asyncpg://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/healthcoach_chat"
        )

    @property
    def chainlit_auth_secret(self) -> str:
        """Persistent JWT secret — created on first run, reused after."""
        secret_file = Path("/config/healthcoach/.chainlit_auth_secret")
        if secret_file.exists():
            return secret_file.read_text().strip()
        secret = secrets.token_urlsafe(48)
        secret_file.parent.mkdir(parents=True, exist_ok=True)
        secret_file.write_text(secret)
        return secret

    @property
    def ifit_token_file(self) -> str:
        return "/config/healthcoach/.ifit_token.json"

    @property
    def apple_key_path(self) -> str:
        """Resolve the Apple private key path relative to /config/."""
        raw = self.apple_oauth_private_key_file
        if not raw:
            return ""
        return raw if raw.startswith("/") else f"/config/{raw}"


# Singleton — imported by all modules
config = AddonConfig()


# ------------------------------------------------------------------
# s6-overlay environment export
# ------------------------------------------------------------------

_ENV_MAP: dict[str, str] = {
    "DB_HOST": "db_host",
    "DB_PORT": "db_port",
    "DB_NAME": "db_name",
    "DB_USER": "db_user",
    "DB_PASSWORD": "db_password",
    "GRAFANA_HOST": "grafana_host",
    "GRAFANA_PORT": "grafana_port",
    "GRAFANA_API_KEY": "grafana_api_key",
    "MCP_PORT": "mcp_port",
    "CHAT_PORT": "chat_port",
    "SYNC_INTERVAL": "sync_interval_minutes",
    "OPENROUTER_API_KEY": "openrouter_api_key",
    "OPENAI_API_KEY": "openai_api_key",
    "CHAT_MODEL": "chat_model",
    "EMBEDDING_API_BASE": "embedding_api_base",
    "EMBEDDING_MODEL": "embedding_model",
    "CHAINLIT_URL": "chainlit_url",
    "ALLOW_REGISTRATION": "",
    "GITHUB_TOKEN": "github_token",
    "R2_ACCOUNT_ID": "r2_account_id",
    "R2_ACCESS_KEY_ID": "r2_access_key_id",
    "R2_SECRET_ACCESS_KEY": "r2_secret_access_key",
    "R2_BUCKET_NAME": "r2_bucket_name",
    "TELEGRAM_BOT_TOKEN": "telegram_bot_token",
    "TELEGRAM_BOT_USERNAME": "telegram_bot_username",
}


def write_s6_env(env_dir: str = "/run/s6/container_environment") -> None:
    """Write all config values as individual files for ``with-contenv``.

    Each file is named after the env var and contains its value.
    This is the s6-overlay v3 convention for passing environment to services.
    """
    out = Path(env_dir)
    out.mkdir(parents=True, exist_ok=True)

    for var, attr in _ENV_MAP.items():
        if var == "ALLOW_REGISTRATION":
            value = "true" if config.allow_registration else "false"
        else:
            value = str(getattr(config, attr))
        (out / var).write_text(value)

    # Derived / fixed values
    (out / "PYTHONPATH").write_text("/app")
    (out / "IFIT_TOKEN_FILE").write_text(config.ifit_token_file)
    (out / "CHAINLIT_DB_URL").write_text(config.chainlit_db_url)
    (out / "CHAINLIT_AUTH_SECRET").write_text(config.chainlit_auth_secret)

    # OAuth (Google)
    if config.google_oauth_client_id:
        (out / "OAUTH_GOOGLE_CLIENT_ID").write_text(config.google_oauth_client_id)
        (out / "OAUTH_GOOGLE_CLIENT_SECRET").write_text(config.google_oauth_client_secret)

    # OAuth (Apple)
    if config.apple_oauth_client_id:
        (out / "OAUTH_APPLE_CLIENT_ID").write_text(config.apple_oauth_client_id)
        (out / "OAUTH_APPLE_TEAM_ID").write_text(config.apple_oauth_team_id)
        (out / "OAUTH_APPLE_KEY_ID").write_text(config.apple_oauth_key_id)
        if config.apple_key_path:
            (out / "OAUTH_APPLE_PRIVATE_KEY_FILE").write_text(config.apple_key_path)
