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
    base = f"http://{host}:{port}"

    # Detect HA ingress: if port returns a redirect to hassio_ingress, follow it
    ingress_path = os.environ.get("GRAFANA_INGRESS_PATH", "")
    if ingress_path:
        return f"{base}{ingress_path}"

    try:
        import httpx as _httpx
        resp = _httpx.get(f"{base}/api/health", follow_redirects=False, timeout=5)
        if resp.status_code in (301, 302):
            location = resp.headers.get("location", "")
            if "hassio_ingress" in location:
                import re
                match = re.search(r"(/api/hassio_ingress/[^/]+)", location)
                if match:
                    return f"{base}{match.group(1)}"
    except Exception:
        pass

    return base


def get_api_key() -> str | None:
    return os.environ.get("GRAFANA_API_KEY") or None


def ensure_datasource(client: httpx.Client) -> None:
    """Create the TimescaleDB datasource if it doesn't exist."""
    resp = client.get("/api/datasources/name/TimescaleDB")
    if resp.status_code == 200:
        print(f"  Datasource 'TimescaleDB' already exists (uid={resp.json().get('uid', '?')})")
        return

    # When Grafana and TimescaleDB are both HA addons, they talk via
    # the internal addon hostname. From outside (Tailscale), we use
    # the Tailscale IP, but Grafana needs the internal address.
    db_host_for_grafana = os.environ.get("GRAFANA_DB_HOST", "")
    if not db_host_for_grafana:
        db_host_for_grafana = os.environ.get("DB_HOST", "localhost")

    ds_payload = {
        "name": "TimescaleDB",
        "type": "grafana-postgresql-datasource",
        "access": "proxy",
        "url": f"{db_host_for_grafana}:{os.environ.get('DB_PORT', '5432')}",
        "database": os.environ.get("DB_NAME", "health"),
        "user": os.environ.get("DB_USER", "postgres"),
        "jsonData": {
            "sslmode": "disable",
            "postgresVersion": 1700,
            "timescaledb": True,
        },
        "secureJsonData": {
            "password": os.environ.get("DB_PASSWORD", ""),
        },
        "isDefault": False,
    }
    resp = client.post("/api/datasources", json=ds_payload)
    if resp.status_code in (200, 409):
        result = resp.json()
        print(f"  Datasource 'TimescaleDB' created (uid={result.get('datasource', {}).get('uid', result.get('uid', '?'))})")
    else:
        print(f"  WARN: Datasource creation: {resp.status_code} {resp.text[:300]}")


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
