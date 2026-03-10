#!/usr/bin/env python3
"""Run database migrations via Alembic.

Handles the transition from the old ``_migrations`` table to Alembic:
    1. If the legacy ``_migrations`` table exists and contains entries,
       we stamp the Alembic baseline revision without executing any SQL
       (the schema is already in place).
    2. Otherwise Alembic runs ``upgrade head`` normally.

Works both locally (via .env) and inside the HA addon.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

import psycopg2
from alembic import command
from alembic.config import Config

ROOT = Path(__file__).resolve().parent.parent
ALEMBIC_INI = ROOT / "db" / "alembic.ini"
BASELINE_REV = "0001"


def _get_connection():
    return psycopg2.connect(
        host=os.environ["DB_HOST"],
        port=os.environ.get("DB_PORT", "5432"),
        dbname=os.environ["DB_NAME"],
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
    )


def _legacy_migrations_applied(conn) -> bool:
    """Check if the old _migrations table exists and has entries."""
    cur = conn.cursor()
    cur.execute("""
        SELECT EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_name = '_migrations'
        )
    """)
    if not cur.fetchone()[0]:
        return False
    cur.execute("SELECT COUNT(*) FROM _migrations")
    count = cur.fetchone()[0]
    cur.close()
    return count > 0


def _alembic_version_exists(conn) -> bool:
    """Check if alembic_version table already has a revision stamped."""
    cur = conn.cursor()
    cur.execute("""
        SELECT EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_name = 'alembic_version'
        )
    """)
    if not cur.fetchone()[0]:
        return False
    cur.execute("SELECT COUNT(*) FROM alembic_version")
    count = cur.fetchone()[0]
    cur.close()
    return count > 0


def _alembic_cfg() -> Config:
    cfg = Config(str(ALEMBIC_INI))
    return cfg


def main() -> int:
    conn = _get_connection()
    conn.autocommit = True

    cfg = _alembic_cfg()

    if _alembic_version_exists(conn):
        print("  Alembic version table found — running upgrade head")
        conn.close()
        command.upgrade(cfg, "head")
    elif _legacy_migrations_applied(conn):
        print("  Legacy _migrations table found — stamping Alembic baseline")
        conn.close()
        command.stamp(cfg, BASELINE_REV)
        print(f"  Stamped revision {BASELINE_REV}")
        command.upgrade(cfg, "head")
    else:
        print("  Fresh database — running full Alembic upgrade")
        conn.close()
        command.upgrade(cfg, "head")

    print("Migrations complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
