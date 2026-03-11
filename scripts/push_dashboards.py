#!/usr/bin/env python3
"""
Push Grafana dashboard JSON files via the Grafana HTTP API.

Creates a 'Health Coach' folder and provisions all dashboards into it.
Reads dashboard files from grafana/dashboards/.

Usage:
    python scripts/push_dashboards.py
    # or via task:
    task grafana:push
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import httpx

from scripts import ops_emit
from scripts.addon_config import config
from scripts.db_pool import dsn_kwargs

ROOT = Path(__file__).resolve().parent.parent
DASHBOARDS_DIR = ROOT / "grafana" / "dashboards"

FOLDER_UID = "healthcoach"
FOLDER_TITLE = "Health Coach"


def get_grafana_url() -> str:
    host = config.grafana_host
    port = config.grafana_port
    base = f"http://{host}:{port}"

    ingress_path = config.grafana_ingress_path
    if ingress_path:
        return f"{base}{ingress_path}"

    try:
        resp = httpx.get(f"{base}/api/health", follow_redirects=False, timeout=5)
        if resp.status_code in (301, 302):
            location = resp.headers.get("location", "")
            if "hassio_ingress" in location:
                match = re.search(r"(/api/hassio_ingress/[^/]+)", location)
                if match:
                    return f"{base}{match.group(1)}"
    except Exception:
        pass

    return base


def get_api_key() -> str | None:
    return config.grafana_api_key or None


# ---------------------------------------------------------------------------
# Folder management
# ---------------------------------------------------------------------------

def ensure_folder(client: httpx.Client, uid: str, title: str, parent_uid: str = "") -> str:
    """Create folder if it doesn't exist. Returns the folder UID."""
    resp = client.get(f"/api/folders/{uid}")
    if resp.status_code == 200:
        print(f"  Folder '{title}' exists (uid={uid})")
        return uid

    payload: dict = {"uid": uid, "title": title}
    if parent_uid:
        payload["parentUid"] = parent_uid

    resp = client.post("/api/folders", json=payload)
    if resp.status_code == 200:
        print(f"  Folder '{title}' created (uid={uid})")
        return uid
    else:
        print(f"  WARN: Folder '{title}': {resp.status_code} {resp.text[:200]}")
        return uid


# ---------------------------------------------------------------------------
# Datasource
# ---------------------------------------------------------------------------

def _ds_payload() -> dict:
    db = dsn_kwargs()
    db_host = config.grafana_db_host or db["host"]
    return {
        "name": "TimescaleDB",
        "type": "grafana-postgresql-datasource",
        "access": "proxy",
        "url": f"{db_host}:{db['port']}",
        "database": db["dbname"],
        "user": db["user"],
        "jsonData": {"sslmode": "disable", "postgresVersion": 1700, "timescaledb": True},
        "secureJsonData": {"password": db["password"]},
        "isDefault": False,
    }


def _try_supervisor_datasource() -> bool:
    """Create datasource via HA Supervisor ingress (admin-level access)."""
    supervisor_token = config.supervisor_token
    if not supervisor_token:
        return False

    sup_headers = {"Authorization": f"Bearer {supervisor_token}"}
    try:
        resp = httpx.post(
            "http://supervisor/ingress/session",
            headers=sup_headers,
            json={"addon": "a0d7b954_grafana"},
            timeout=10,
        )
        if resp.status_code != 200:
            return False
        session = resp.json().get("data", {}).get("session", "")
        if not session:
            return False
    except Exception:
        return False

    grafana_host = config.grafana_host
    grafana_port = config.grafana_port
    grafana_url = f"http://{grafana_host}:{grafana_port}"

    try:
        resp = httpx.post(
            f"{grafana_url}/api/datasources",
            json=_ds_payload(),
            cookies={"ingress_session": session},
            headers={"Content-Type": "application/json"},
            timeout=15,
        )
        if resp.status_code in (200, 409):
            print("  Datasource 'TimescaleDB' created (via Supervisor ingress)")
            return True
        print(f"  Supervisor ingress attempt: {resp.status_code} {resp.text[:200]}")
    except Exception as exc:
        print(f"  Supervisor ingress attempt failed: {exc}")
    return False


def ensure_datasource(client: httpx.Client) -> None:
    resp = client.get("/api/datasources/name/TimescaleDB")
    if resp.status_code == 200:
        print(f"  Datasource 'TimescaleDB' exists (uid={resp.json().get('uid', '?')})")
        return

    payload = _ds_payload()
    resp = client.post("/api/datasources", json=payload)
    if resp.status_code in (200, 409):
        print("  Datasource 'TimescaleDB' created")
        return

    if resp.status_code == 403 and _try_supervisor_datasource():
        return

    db_url = payload["url"]
    print(f"  WARN: Cannot auto-create datasource ({resp.status_code})")
    print(f"  The API key lacks datasources:create permission.")
    print(f"  Create it once in the Grafana UI  ->  Connections > Data sources > Add:")
    print(f"    Name:       TimescaleDB")
    print(f"    Type:       PostgreSQL")
    print(f"    Host:       {db_url}")
    print(f"    Database:   {payload['database']}")
    print(f"    User:       {payload['user']}")
    print(f"    TLS/SSL:    disable")
    print(f"    TimescaleDB: ON")
    print(f"    PostgreSQL:  17")
    print(f"  Or upgrade the service account to Admin role in Grafana.")


# ---------------------------------------------------------------------------
# Dashboard push
# ---------------------------------------------------------------------------

def push_dashboard(client: httpx.Client, dashboard_path: Path, folder_uid: str) -> bool:
    with open(dashboard_path) as f:
        payload = json.load(f)

    payload["folderUid"] = folder_uid
    payload["overwrite"] = True

    resp = client.post("/api/dashboards/db", json=payload)
    if resp.status_code == 200:
        result = resp.json()
        print(f"  OK  {dashboard_path.name} -> {result.get('url', '?')}")
        return True
    else:
        print(f"  ERR {dashboard_path.name}: {resp.status_code} {resp.text[:200]}")
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    url = get_grafana_url()
    api_key = get_api_key()

    if not api_key:
        print("WARN: GRAFANA_API_KEY not set. Skipping dashboard provisioning.")
        return 0

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    print(f"Connecting to Grafana at {url}")

    ok = True
    with httpx.Client(base_url=url, headers=headers, timeout=30) as client:
        try:
            health = client.get("/api/health")
            if health.status_code != 200:
                print(f"ERROR: Grafana health check failed: {health.status_code}")
                return 1
            version = health.json().get("version", "?")
            print(f"  Grafana v{version} is healthy")
        except httpx.ConnectError:
            print(f"ERROR: Cannot connect to Grafana at {url}")
            return 1

        ensure_datasource(client)
        folder_uid = ensure_folder(client, FOLDER_UID, FOLDER_TITLE)

        dashboards = sorted(DASHBOARDS_DIR.glob("*.json"))
        if not dashboards:
            print("No dashboard files found in grafana/dashboards/")
            return 0

        print(f"Pushing {len(dashboards)} dashboard(s) to folder '{FOLDER_TITLE}'...")
        pushed = 0
        failed = 0
        for path in dashboards:
            if push_dashboard(client, path, folder_uid):
                pushed += 1
            else:
                ok = False
                failed += 1

        ops_emit.emit(
            "grafana", "push_dashboards",
            status="ok" if ok else "error",
            pushed=pushed, failed=failed, total=len(dashboards),
        )

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
