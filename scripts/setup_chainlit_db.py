#!/usr/bin/env python3
"""
Ensure the Chainlit chat-persistence database and tables exist.

Creates the 'healthcoach_chat' PostgreSQL database (if missing) and runs
the Chainlit SQLAlchemy schema DDL idempotently.  Safe to re-run on every
addon startup.
"""

from __future__ import annotations

import sys
from pathlib import Path

import psycopg2

from scripts.addon_config import config  # noqa: F401 — triggers load_dotenv
from scripts.db_pool import dsn_kwargs

CHAT_DB = "healthcoach_chat"

DDL = """
CREATE TABLE IF NOT EXISTS users (
    "id"         UUID PRIMARY KEY,
    "identifier" TEXT NOT NULL UNIQUE,
    "metadata"   TEXT NOT NULL,
    "createdAt"  TEXT
);

CREATE TABLE IF NOT EXISTS threads (
    "id"             UUID PRIMARY KEY,
    "createdAt"      TEXT,
    "name"           TEXT,
    "userId"         UUID,
    "userIdentifier" TEXT,
    "tags"           TEXT[],
    "metadata"       TEXT,
    FOREIGN KEY ("userId") REFERENCES users("id") ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS steps (
    "id"            UUID PRIMARY KEY,
    "name"          TEXT NOT NULL,
    "type"          TEXT NOT NULL,
    "threadId"      UUID NOT NULL,
    "parentId"      UUID,
    "streaming"     BOOLEAN NOT NULL,
    "waitForAnswer" BOOLEAN,
    "isError"       BOOLEAN,
    "metadata"      TEXT,
    "tags"          TEXT[],
    "input"         TEXT,
    "output"        TEXT,
    "createdAt"     TEXT,
    "command"       TEXT,
    "start"         TEXT,
    "end"           TEXT,
    "generation"    TEXT,
    "showInput"     TEXT,
    "language"      TEXT,
    "indent"        INT,
    "defaultOpen"   BOOLEAN,
    "autoCollapse"  BOOLEAN,
    FOREIGN KEY ("threadId") REFERENCES threads("id") ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS elements (
    "id"           UUID PRIMARY KEY,
    "threadId"     UUID,
    "type"         TEXT,
    "url"          TEXT,
    "chainlitKey"  TEXT,
    "name"         TEXT NOT NULL,
    "display"      TEXT,
    "objectKey"    TEXT,
    "size"         TEXT,
    "page"         INT,
    "language"     TEXT,
    "forId"        UUID,
    "mime"         TEXT,
    "props"        TEXT,
    FOREIGN KEY ("threadId") REFERENCES threads("id") ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS feedbacks (
    "id"       UUID PRIMARY KEY,
    "forId"    UUID NOT NULL,
    "threadId" UUID NOT NULL,
    "value"    INT NOT NULL,
    "comment"  TEXT,
    FOREIGN KEY ("threadId") REFERENCES threads("id") ON DELETE CASCADE
);
"""

# Migrate any existing JSONB columns to TEXT so Chainlit's LIKE queries work.
MIGRATIONS = """
DO $$ BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name='users' AND column_name='metadata' AND data_type='jsonb'
  ) THEN
    ALTER TABLE users ALTER COLUMN "metadata" TYPE TEXT USING "metadata"::text;
  END IF;
END $$;

DO $$ BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name='threads' AND column_name='metadata' AND data_type='jsonb'
  ) THEN
    ALTER TABLE threads ALTER COLUMN "metadata" TYPE TEXT USING "metadata"::text;
  END IF;
END $$;

DO $$ BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name='steps' AND column_name='metadata' AND data_type='jsonb'
  ) THEN
    ALTER TABLE steps ALTER COLUMN "metadata" TYPE TEXT USING "metadata"::text;
  END IF;
END $$;

DO $$ BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name='steps' AND column_name='generation' AND data_type='jsonb'
  ) THEN
    ALTER TABLE steps ALTER COLUMN "generation" TYPE TEXT USING "generation"::text;
  END IF;
END $$;

DO $$ BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name='elements' AND column_name='props' AND data_type='jsonb'
  ) THEN
    ALTER TABLE elements ALTER COLUMN "props" TYPE TEXT USING "props"::text;
  END IF;
END $$;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name='steps' AND column_name='autoCollapse'
  ) THEN
    ALTER TABLE steps ADD COLUMN "autoCollapse" BOOLEAN;
  END IF;
END $$;
"""


def _conn(dbname: str):
    return psycopg2.connect(**dsn_kwargs(dbname))


def main() -> int:
    # Create the database if it doesn't exist
    try:
        admin = _conn("postgres")
        admin.autocommit = True
        cur = admin.cursor()
        cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (CHAT_DB,))
        if not cur.fetchone():
            cur.execute(f'CREATE DATABASE "{CHAT_DB}"')
            print(f"  Created database: {CHAT_DB}")
        else:
            print(f"  Database already exists: {CHAT_DB}")
        admin.close()
    except Exception as exc:
        print(f"  WARNING: Could not check/create {CHAT_DB}: {exc}")
        return 1

    # Apply schema idempotently then migrate JSONB → TEXT columns
    try:
        conn = _conn(CHAT_DB)
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute(DDL)
        cur.execute(MIGRATIONS)
        print(f"  Chainlit schema ready in {CHAT_DB}")
        conn.close()
    except Exception as exc:
        print(f"  WARNING: Could not apply Chainlit schema: {exc}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
