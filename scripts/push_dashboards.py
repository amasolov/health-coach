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

    dashboards = sorted(DASHBOARDS_DIR.glob("*.json"))
    if not dashboards:
        print("No dashboard files found in grafana/dashboards/")
        return 0

    print(f"Pushing {len(dashboards)} dashboard(s) to {url}")

    ok = True
    with httpx.Client(base_url=url, headers=headers, timeout=30) as client:
        for path in dashboards:
            if not push_dashboard(client, path):
                ok = False

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
