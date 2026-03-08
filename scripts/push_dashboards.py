#!/usr/bin/env python3
"""
Push Grafana dashboard JSON files via the Grafana HTTP API.

Creates a 'Health Tracker' folder and provisions all dashboards into it.
Reads dashboard files from grafana/dashboards/.

Usage:
    python scripts/push_dashboards.py
    # or via task:
    task grafana:push
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

import httpx

ROOT = Path(__file__).resolve().parent.parent
DASHBOARDS_DIR = ROOT / "grafana" / "dashboards"

FOLDER_UID = "health-tracker"
FOLDER_TITLE = "Health Tracker"


def get_grafana_url() -> str:
    host = os.environ.get("GRAFANA_HOST", "localhost")
    port = os.environ.get("GRAFANA_PORT", "3000")
    base = f"http://{host}:{port}"

    ingress_path = os.environ.get("GRAFANA_INGRESS_PATH", "")
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
    return os.environ.get("GRAFANA_API_KEY") or None


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

def ensure_datasource(client: httpx.Client) -> None:
    resp = client.get("/api/datasources/name/TimescaleDB")
    if resp.status_code == 200:
        print(f"  Datasource 'TimescaleDB' exists (uid={resp.json().get('uid', '?')})")
        return

    db_host = os.environ.get("GRAFANA_DB_HOST", "") or os.environ.get("DB_HOST", "localhost")
    ds_payload = {
        "name": "TimescaleDB",
        "type": "grafana-postgresql-datasource",
        "access": "proxy",
        "url": f"{db_host}:{os.environ.get('DB_PORT', '5432')}",
        "database": os.environ.get("DB_NAME", "health"),
        "user": os.environ.get("DB_USER", "postgres"),
        "jsonData": {"sslmode": "disable", "postgresVersion": 1700, "timescaledb": True},
        "secureJsonData": {"password": os.environ.get("DB_PASSWORD", "")},
        "isDefault": False,
    }
    resp = client.post("/api/datasources", json=ds_payload)
    if resp.status_code in (200, 409):
        print(f"  Datasource 'TimescaleDB' created")
    else:
        print(f"  WARN: Datasource creation: {resp.status_code} {resp.text[:300]}")


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
        for path in dashboards:
            if not push_dashboard(client, path, folder_uid):
                ok = False

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
