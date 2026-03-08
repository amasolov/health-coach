#!/usr/bin/env python3
"""
Push Grafana dashboard JSON files via the Grafana HTTP API.

Usage:
    python scripts/push_dashboards.py
    # or via task:
    task grafana:push
"""

from __future__ import annotations

import json
import os
import sys

from dotenv import load_dotenv
load_dotenv()
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
DASHBOARDS_DIR = ROOT / "grafana" / "dashboards"


def get_grafana_url() -> str:
    host = os.environ.get("GRAFANA_HOST", "localhost")
    port = os.environ.get("GRAFANA_PORT", "3000")
    return f"http://{host}:{port}"


def get_api_key() -> str | None:
    return os.environ.get("GRAFANA_API_KEY") or None


def ensure_datasource(client: httpx.Client) -> None:
    """Create the TimescaleDB datasource if it doesn't exist."""
    resp = client.get("/api/datasources/name/TimescaleDB")
    if resp.status_code == 200:
        print(f"  Datasource 'TimescaleDB' already exists (uid={resp.json().get('uid', '?')})")
        return

    ds_payload = {
        "name": "TimescaleDB",
        "type": "postgres",
        "access": "proxy",
        "url": f"{os.environ.get('DB_HOST', 'localhost')}:{os.environ.get('DB_PORT', '5432')}",
        "database": os.environ.get("DB_NAME", "health"),
        "user": os.environ.get("DB_USER", "postgres"),
        "jsonData": {
            "sslmode": "disable",
            "postgresVersion": 1500,
            "timescaledb": True,
        },
        "secureJsonData": {
            "password": os.environ.get("DB_PASSWORD", ""),
        },
        "isDefault": True,
    }
    resp = client.post("/api/datasources", json=ds_payload)
    if resp.status_code in (200, 409):
        print(f"  Datasource 'TimescaleDB' created")
    else:
        print(f"  WARN: Datasource creation: {resp.status_code} {resp.text[:200]}")


def push_dashboard(client: httpx.Client, dashboard_path: Path) -> bool:
    with open(dashboard_path) as f:
        payload = json.load(f)

    resp = client.post("/api/dashboards/db", json=payload)

    if resp.status_code == 200:
        result = resp.json()
        print(f"  OK  {dashboard_path.name} -> {result.get('url', '?')}")
        return True
    else:
        print(f"  ERR {dashboard_path.name}: {resp.status_code} {resp.text[:200]}")
        return False


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
        # Verify connection
        try:
            health = client.get("/api/health")
            if health.status_code != 200:
                print(f"ERROR: Grafana health check failed: {health.status_code}")
                return 1
            print(f"  Grafana is healthy")
        except httpx.ConnectError:
            print(f"ERROR: Cannot connect to Grafana at {url}")
            print(f"  If using HA addon, ensure Grafana port 3000 is exposed in the addon Network settings")
            return 1

        ensure_datasource(client)

        dashboards = sorted(DASHBOARDS_DIR.glob("*.json"))
        if not dashboards:
            print("No dashboard files found in grafana/dashboards/")
            return 0

        print(f"Pushing {len(dashboards)} dashboard(s)...")
        for path in dashboards:
            if not push_dashboard(client, path):
                ok = False

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
