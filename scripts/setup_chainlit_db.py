#!/usr/bin/env python3
"""
Ensure the Chainlit chat-persistence database and tables exist.

Creates the 'healthcoach_chat' PostgreSQL database (if missing) and runs
the Chainlit SQLAlchemy schema DDL idempotently.  Safe to re-run on every
addon startup.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

import psycopg2

CHAT_DB = "healthcoach_chat"

DDL = """
CREATE TABLE IF NOT EXISTS users (
    "id"         UUID PRIMARY KEY,
    "identifier" TEXT NOT NULL UNIQUE,
    "metadata"   JSONB NOT NULL,
    "createdAt"  TEXT
);

CREATE TABLE IF NOT EXISTS threads (
    "id"             UUID PRIMARY KEY,
    "createdAt"      TEXT,
    "name"           TEXT,
    "userId"         UUID,
    "userIdentifier" TEXT,
    "tags"           TEXT[],
    "metadata"       JSONB,
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
    "metadata"      JSONB,
    "tags"          TEXT[],
    "input"         TEXT,
    "output"        TEXT,
    "createdAt"     TEXT,
    "command"       TEXT,
    "start"         TEXT,
    "end"           TEXT,
    "generation"    JSONB,
    "showInput"     TEXT,
    "language"      TEXT,
    "indent"        INT,
    "defaultOpen"   BOOLEAN,
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
    "props"        JSONB,
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


def _conn(dbname: str):
    return psycopg2.connect(
        host=os.environ["DB_HOST"],
        port=os.environ.get("DB_PORT", "5432"),
        dbname=dbname,
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
    )


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

    # Apply schema idempotently
    try:
        conn = _conn(CHAT_DB)
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute(DDL)
        print(f"  Chainlit schema ready in {CHAT_DB}")
        conn.close()
    except Exception as exc:
        print(f"  WARNING: Could not apply Chainlit schema: {exc}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
