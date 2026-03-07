#!/usr/bin/env python3
"""Run database migrations. Works both locally (via .env) and inside the HA addon."""

from __future__ import annotations

import glob
import os
import sys
from pathlib import Path

import psycopg2

ROOT = Path(__file__).resolve().parent.parent


def get_connection():
    return psycopg2.connect(
        host=os.environ["DB_HOST"],
        port=os.environ.get("DB_PORT", "5432"),
        dbname=os.environ["DB_NAME"],
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
    )


def main() -> int:
    conn = get_connection()
    conn.autocommit = True
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS _migrations (
            filename TEXT PRIMARY KEY,
            applied_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)

    migrations_dir = ROOT / "db" / "migrations"
    for fpath in sorted(migrations_dir.glob("*.sql")):
        fname = fpath.name
        cur.execute("SELECT 1 FROM _migrations WHERE filename = %s", (fname,))
        if cur.fetchone():
            print(f"  skip  {fname}")
            continue
        print(f"  apply {fname}")
        cur.execute(fpath.read_text())
        cur.execute("INSERT INTO _migrations (filename) VALUES (%s)", (fname,))

    cur.close()
    conn.close()
    print("Migrations complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
