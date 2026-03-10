#!/usr/bin/env python3
"""One-shot addon initialisation script.

Runs as the first ``cont-init.d`` step under s6-overlay.  Replaces all of
the inline ``python3 -c "..."`` blocks that used to live in ``run.sh``.

Steps (in order):
    1. Load config via Pydantic and write s6 container environment
    2. Link persistent HA config files
    3. Run database migrations
    4. Set up Chainlit chat database
    5. Migrate users.json → DB (one-time, idempotent)
    6. Backfill onboarding + MCP keys for legacy users
    7. Print registered users
    8. Index knowledge-base PDFs
    9. Warm Garmin authentication tokens
   10. Provision Grafana dashboards
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import traceback
from pathlib import Path

sys.path.insert(0, "/app")
os.chdir("/app")

from scripts.addon_config import config, write_s6_env


def _run(cmd: list[str], label: str) -> bool:
    """Run a subprocess, print a label, return True on success."""
    print(f"\n--- {label} ---")
    result = subprocess.run(cmd, cwd="/app")
    if result.returncode != 0:
        print(f"WARN: {label} failed (exit {result.returncode})")
        return False
    return True


# ------------------------------------------------------------------
# 1. Write s6 container environment
# ------------------------------------------------------------------

def step_write_env() -> None:
    print("=== Health Coach Addon ===")
    write_s6_env()

    # Also set env in THIS process so subsequent steps can use them
    os.environ.setdefault("DB_HOST", str(config.db_host))
    os.environ.setdefault("DB_PORT", str(config.db_port))
    os.environ.setdefault("DB_NAME", config.db_name)
    os.environ.setdefault("DB_USER", config.db_user)
    os.environ.setdefault("DB_PASSWORD", config.db_password)
    os.environ.setdefault("GRAFANA_HOST", config.grafana_host)
    os.environ.setdefault("GRAFANA_PORT", str(config.grafana_port))
    os.environ.setdefault("GRAFANA_API_KEY", config.grafana_api_key)
    os.environ.setdefault("MCP_PORT", str(config.mcp_port))
    os.environ.setdefault("CHAT_PORT", str(config.chat_port))
    os.environ.setdefault("SYNC_INTERVAL", str(config.sync_interval_minutes))
    os.environ.setdefault("OPENROUTER_API_KEY", config.openrouter_api_key)
    os.environ.setdefault("OPENAI_API_KEY", config.openai_api_key)
    os.environ.setdefault("CHAT_MODEL", config.chat_model)
    os.environ.setdefault("GITHUB_TOKEN", config.github_token)
    os.environ.setdefault("TELEGRAM_BOT_TOKEN", config.telegram_bot_token)
    os.environ.setdefault("TELEGRAM_BOT_USERNAME", config.telegram_bot_username)
    os.environ.setdefault("R2_ACCOUNT_ID", config.r2_account_id)
    os.environ.setdefault("R2_ACCESS_KEY_ID", config.r2_access_key_id)
    os.environ.setdefault("R2_SECRET_ACCESS_KEY", config.r2_secret_access_key)
    os.environ.setdefault("R2_BUCKET_NAME", config.r2_bucket_name)
    os.environ.setdefault("EMBEDDING_API_BASE", config.embedding_api_base)
    os.environ.setdefault("EMBEDDING_MODEL", config.embedding_model)
    os.environ.setdefault("ALLOW_REGISTRATION", "true" if config.allow_registration else "false")
    os.environ.setdefault("CHAINLIT_URL", config.chainlit_url)
    os.environ.setdefault("CHAINLIT_DB_URL", config.chainlit_db_url)
    os.environ.setdefault("CHAINLIT_AUTH_SECRET", config.chainlit_auth_secret)
    os.environ.setdefault("IFIT_TOKEN_FILE", config.ifit_token_file)
    os.environ["PYTHONPATH"] = "/app"

    if config.google_oauth_client_id:
        os.environ.setdefault("OAUTH_GOOGLE_CLIENT_ID", config.google_oauth_client_id)
        os.environ.setdefault("OAUTH_GOOGLE_CLIENT_SECRET", config.google_oauth_client_secret)
        print("  Google OAuth: enabled")
    if config.apple_oauth_client_id:
        os.environ.setdefault("OAUTH_APPLE_CLIENT_ID", config.apple_oauth_client_id)
        os.environ.setdefault("OAUTH_APPLE_TEAM_ID", config.apple_oauth_team_id)
        os.environ.setdefault("OAUTH_APPLE_KEY_ID", config.apple_oauth_key_id)
        if config.apple_key_path:
            os.environ.setdefault("OAUTH_APPLE_PRIVATE_KEY_FILE", config.apple_key_path)
        print("  Apple OAuth: enabled")

    print(f"DB: {config.db_host}:{config.db_port}/{config.db_name}")
    print(f"Grafana: {config.grafana_host}:{config.grafana_port}")
    print(f"MCP server: port {config.mcp_port}")
    print(f"Chat UI: port {config.chat_port} (model: {config.chat_model})")
    print(f"Sync interval: {config.sync_interval_minutes} minutes")


# ------------------------------------------------------------------
# 2. Link persistent config files
# ------------------------------------------------------------------

def step_link_config() -> None:
    ha_cfg = Path("/config/healthcoach")
    ha_cfg.mkdir(parents=True, exist_ok=True)

    for cfg in ("equipment.yaml", "zones.yaml"):
        target = ha_cfg / cfg
        link = Path(f"/app/config/{cfg}")
        if not target.exists():
            example = Path(f"/app/config/{cfg.replace('.yaml', '.example.yaml')}")
            if example.exists():
                import shutil
                shutil.copy2(str(example), str(target))
                print(f"INFO: Created {target} from example template")
        link.unlink(missing_ok=True)
        link.symlink_to(target)

    print(f"Config files linked from {ha_cfg}")

    # Persist iFit library cache across restarts
    ifit_cache = ha_cfg / ".ifit_capture"
    ifit_cache.mkdir(parents=True, exist_ok=True)
    link = Path("/app/.ifit_capture")
    link.unlink(missing_ok=True)
    link.symlink_to(ifit_cache)

    if Path(config.ifit_token_file).exists():
        print(f"iFit: token found at {config.ifit_token_file}")
    else:
        print(f"iFit: no token yet — copy .ifit_token.json to {config.ifit_token_file}")


# ------------------------------------------------------------------
# 3–4. Database migrations
# ------------------------------------------------------------------

def step_migrate() -> None:
    _run([sys.executable, "/app/scripts/run_migrate.py"], "Database migrations")
    _run(
        [sys.executable, "/app/scripts/setup_chainlit_db.py"],
        "Chainlit chat database",
    )


# ------------------------------------------------------------------
# 5–6. Migrate users.json and backfill
# ------------------------------------------------------------------

def step_migrate_users() -> None:
    import psycopg2

    users_file = Path("/config/healthcoach/users.json")
    if not users_file.exists():
        print("INFO: No users.json to migrate — users are managed in the database.")
        return

    raw = users_file.read_text().strip()
    if not raw or raw == "[]":
        return

    users = json.loads(raw)
    if not users:
        return

    conn = psycopg2.connect(
        host=config.db_host,
        port=config.db_port,
        dbname=config.db_name,
        user=config.db_user,
        password=config.db_password,
    )
    cur = conn.cursor()
    migrated = 0
    for u in users:
        slug = u.get("slug", "")
        if not slug:
            continue
        mcp_key = u.get("mcp_api_key") or secrets.token_urlsafe(32)
        onboarding = u.get("onboarding_complete", True)
        cur.execute(
            """UPDATE users SET
                email              = COALESCE(NULLIF(%(email)s, ''), email),
                first_name         = COALESCE(NULLIF(%(first_name)s, ''), first_name),
                last_name          = COALESCE(NULLIF(%(last_name)s, ''), last_name),
                garmin_email       = COALESCE(NULLIF(%(garmin_email)s, ''), garmin_email),
                garmin_password    = COALESCE(NULLIF(%(garmin_password)s, ''), garmin_password),
                hevy_api_key       = COALESCE(NULLIF(%(hevy_api_key)s, ''), hevy_api_key),
                mcp_api_key        = COALESCE(NULLIF(%(mcp_api_key)s, ''), mcp_api_key),
                onboarding_complete = %(onboarding)s
            WHERE slug = %(slug)s""",
            {
                "slug": slug,
                "email": u.get("email", ""),
                "first_name": u.get("first_name", ""),
                "last_name": u.get("last_name", ""),
                "garmin_email": u.get("garmin_email", ""),
                "garmin_password": u.get("garmin_password", ""),
                "hevy_api_key": u.get("hevy_api_key", ""),
                "mcp_api_key": mcp_key,
                "onboarding": onboarding,
            },
        )
        if cur.rowcount:
            migrated += 1
            print(f"  Migrated {slug} from users.json -> DB")
    conn.commit()
    cur.close()
    conn.close()

    if migrated:
        print(f"INFO: Migrated {migrated} user(s) into the database.")
        users_file.rename(users_file.with_suffix(".json.migrated"))
        print("INFO: Renamed users.json -> users.json.migrated")
    else:
        print("INFO: All users already in the database — nothing to migrate.")


# ------------------------------------------------------------------
# 7. Print registered users
# ------------------------------------------------------------------

def step_list_users() -> None:
    try:
        from scripts.user_manager import load_all_users

        print("\nRegistered users:")
        for u in load_all_users():
            slug = u.get("slug", "?")
            email = u.get("email", "")
            onb = "yes" if u.get("onboarding_complete") else "no"
            print(f"  {slug} ({email}) onboarding={onb}")
    except Exception:
        print("  (could not load users from DB)")


# ------------------------------------------------------------------
# 8. Knowledge base indexing
# ------------------------------------------------------------------

def step_knowledge_base() -> None:
    kb_dir = Path("/config/healthcoach/knowledge")
    if not kb_dir.is_dir():
        print("No knowledge base PDFs found (optional).")
        return
    pdfs = list(kb_dir.glob("*.pdf"))
    if not pdfs:
        print("No knowledge base PDFs found (optional).")
        return
    print(f"Indexing {len(pdfs)} knowledge base document(s)...")
    try:
        from scripts.knowledge_store import ingest_directory

        for r in ingest_directory(str(kb_dir)):
            fname = r.get("filename", "?")
            status = r.get("status", r.get("error", "unknown"))
            print(f"  {fname}: {status}")
    except Exception as exc:
        print(f"WARN: Knowledge base indexing failed: {exc}")


# ------------------------------------------------------------------
# 9. Warm Garmin authentication
# ------------------------------------------------------------------

def step_garmin_auth() -> None:
    print("\nChecking Garmin authentication...")
    try:
        from scripts import garmin_auth
        from scripts.user_manager import load_all_users

        for u in load_all_users():
            slug = u.get("slug", "")
            email = u.get("garmin_email", "")
            password = u.get("garmin_password", "")
            if not slug or not email or not password:
                continue
            client = garmin_auth.try_cached_login(slug)
            if client:
                print(f"  Garmin OK: {slug}")
            else:
                print(f"  Garmin tokens missing for {slug} — attempting re-auth...")
                status, _ = garmin_auth.start_login(slug, email, password)
                if status == "ok":
                    print(f"  Garmin re-auth OK: {slug}")
                elif status == "needs_mfa":
                    print(f"  Garmin needs MFA for {slug} — user must complete via chat")
                else:
                    print(f"  Garmin re-auth failed for {slug}: {status}")
    except Exception as exc:
        print(f"WARN: Garmin auth check failed: {exc}")


# ------------------------------------------------------------------
# 10. Grafana dashboards
# ------------------------------------------------------------------

def step_grafana() -> None:
    _run(
        [sys.executable, "/app/scripts/push_dashboards.py"],
        "Grafana dashboard provisioning",
    )


# ------------------------------------------------------------------
# Main
# ------------------------------------------------------------------

def main() -> int:
    try:
        step_write_env()
    except Exception:
        traceback.print_exc()
        return 1

    step_link_config()
    step_migrate()
    step_migrate_users()
    step_list_users()
    step_knowledge_base()
    step_garmin_auth()
    step_grafana()
    return 0


if __name__ == "__main__":
    sys.exit(main())
