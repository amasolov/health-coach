"""Typed addon configuration backed by Pydantic Settings.

Loads settings from ``/data/options.json`` (Home Assistant addon runtime) with
an automatic fallback to **environment variables** (local dev / CI).  Field
names match ``options.json`` keys exactly; the corresponding env var is the
UPPER_CASE version (e.g. ``db_host`` ↔ ``DB_HOST``).

Importing this module has two side-effects:

1. ``load_dotenv()`` populates ``os.environ`` from the repo-root ``.env``
   file (no-op when the file is absent, e.g. inside the HA addon).
2. A module-level ``config`` singleton is created for direct attribute access.

Usage::

    from scripts.addon_config import config
    print(config.db_host)

The module also provides :func:`write_s6_env` to export all settings as
individual files into ``/run/s6/container_environment/`` so that s6-overlay
services launched via ``with-contenv`` inherit them.
"""

from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path

from dotenv import load_dotenv
from pydantic_settings import BaseSettings, JsonConfigSettingsSource, SettingsConfigDict

_log = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent
_OPTIONS_FILE = Path("/data/options.json")
_CHAINLIT_SECRET_FILE = Path("/config/healthcoach/.chainlit_auth_secret")

load_dotenv(_ROOT / ".env")


_json_cfg: dict = {}
if _OPTIONS_FILE.exists():
    _json_cfg = {"json_file": str(_OPTIONS_FILE), "json_file_encoding": "utf-8"}


class AddonConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="",
        extra="ignore",
        **_json_cfg,
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
    grafana_ingress_path: str = ""
    grafana_db_host: str = ""

    # --- Ports & intervals ---
    mcp_port: int = 8765
    mcp_host: str = "0.0.0.0"
    mcp_api_key: str = ""
    chat_port: int = 8080
    sync_interval_minutes: int = 30
    sync_max_retries: int = 2
    sync_retry_base: int = 10
    sync_user_timeout: int = 600

    # --- AI / LLM ---
    openrouter_api_key: str = ""
    openai_api_key: str = ""
    llm_base_url: str = "https://openrouter.ai/api/v1"
    chat_model: str = "google/gemini-2.5-flash"
    chat_model_complex: str = "anthropic/claude-sonnet-4"
    model_routing: str = "escalate"
    extraction_model: str = "google/gemini-2.5-flash"
    embedding_api_base: str = ""
    embedding_model: str = "text-embedding-3-small"
    embedding_dim: int = 768

    # --- Chat UI ---
    chainlit_url: str = ""
    allow_registration: bool = False

    # --- OAuth (Google) ---
    google_oauth_client_id: str = ""
    google_oauth_client_secret: str = ""

    # --- GitHub ---
    github_token: str = ""
    github_repo: str = "amasolov/health-coach"

    # --- R2 / S3 ---
    r2_account_id: str = ""
    r2_access_key_id: str = ""
    r2_secret_access_key: str = ""
    r2_bucket_name: str = "health-coach-ifit"

    # --- Telegram ---
    telegram_bot_token: str = ""
    telegram_bot_username: str = ""

    # --- External APIs ---
    hevy_api_key: str = ""

    # --- Home Assistant ---
    supervisor_token: str = ""

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
        """Persistent JWT secret — DB-first, file-fallback, auto-generate."""
        return _resolve_chainlit_secret()

    @property
    def ifit_token_file(self) -> str:
        return "/config/healthcoach/.ifit_token.json"


def _resolve_chainlit_secret() -> str:
    """Resolve the Chainlit JWT secret: DB -> file -> generate new."""
    try:
        from scripts.credential_store import get_credential, put_credential
        cred = get_credential("chainlit_auth_secret")
        if cred and cred.get("secret"):
            return cred["secret"]
    except Exception:
        _log.debug("addon_config: DB credential lookup failed", exc_info=True)

    if _CHAINLIT_SECRET_FILE.exists():
        secret = _CHAINLIT_SECRET_FILE.read_text().strip()
        if secret:
            try:
                from scripts.credential_store import put_credential
                put_credential("chainlit_auth_secret", {"secret": secret})
            except Exception:
                pass
            return secret

    secret = secrets.token_urlsafe(48)

    try:
        from scripts.credential_store import put_credential
        put_credential("chainlit_auth_secret", {"secret": secret})
    except Exception:
        pass

    try:
        _CHAINLIT_SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
        _CHAINLIT_SECRET_FILE.write_text(secret)
    except OSError:
        pass

    return secret


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
    "MCP_HOST": "mcp_host",
    "CHAT_PORT": "chat_port",
    "SYNC_INTERVAL": "sync_interval_minutes",
    "OPENROUTER_API_KEY": "openrouter_api_key",
    "OPENAI_API_KEY": "openai_api_key",
    "LLM_BASE_URL": "llm_base_url",
    "CHAT_MODEL": "chat_model",
    "CHAT_MODEL_COMPLEX": "chat_model_complex",
    "MODEL_ROUTING": "model_routing",
    "EXTRACTION_MODEL": "extraction_model",
    "EMBEDDING_API_BASE": "embedding_api_base",
    "EMBEDDING_MODEL": "embedding_model",
    "EMBEDDING_DIM": "embedding_dim",
    "CHAINLIT_URL": "chainlit_url",
    "ALLOW_REGISTRATION": "",
    "GITHUB_TOKEN": "github_token",
    "GITHUB_REPO": "github_repo",
    "R2_ACCOUNT_ID": "r2_account_id",
    "R2_ACCESS_KEY_ID": "r2_access_key_id",
    "R2_SECRET_ACCESS_KEY": "r2_secret_access_key",
    "R2_BUCKET_NAME": "r2_bucket_name",
    "TELEGRAM_BOT_TOKEN": "telegram_bot_token",
    "TELEGRAM_BOT_USERNAME": "telegram_bot_username",
    "HEVY_API_KEY": "hevy_api_key",
    "SUPERVISOR_TOKEN": "supervisor_token",
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

