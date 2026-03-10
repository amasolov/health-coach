"""
Cross-channel conversation context for Telegram <-> Web chat continuity.

Provides helpers to:
- Persist and retrieve Telegram messages (in the main health DB)
- Retrieve recent web chat messages (from Chainlit's healthcoach_chat DB)
- Format cross-channel context for system prompt injection
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

import psycopg2
import psycopg2.extras

log = logging.getLogger(__name__)

CONTEXT_WINDOW_HOURS = 24
MAX_CROSS_CHANNEL_MESSAGES = 10


def _health_conn():
    return psycopg2.connect(
        host=os.environ.get("DB_HOST", "localhost"),
        port=os.environ.get("DB_PORT", "5432"),
        dbname=os.environ.get("DB_NAME", "health"),
        user=os.environ.get("DB_USER", "postgres"),
        password=os.environ.get("DB_PASSWORD", ""),
    )


def _chat_conn():
    return psycopg2.connect(
        host=os.environ.get("DB_HOST", "localhost"),
        port=os.environ.get("DB_PORT", "5432"),
        dbname="healthcoach_chat",
        user=os.environ.get("DB_USER", "postgres"),
        password=os.environ.get("DB_PASSWORD", ""),
    )


# ---------------------------------------------------------------------------
# Telegram message persistence
# ---------------------------------------------------------------------------

def save_telegram_message(user_id: int, chat_id: int, role: str, content: str) -> None:
    conn = _health_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO telegram_messages (user_id, chat_id, role, content)
               VALUES (%s, %s, %s, %s)""",
            (user_id, chat_id, role, content),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        log.debug("Failed to persist telegram message", exc_info=True)
    finally:
        conn.close()


def load_telegram_history(
    user_id: int,
    limit: int = 40,
) -> list[dict]:
    """Load recent Telegram messages for LLM context (chronological order)."""
    conn = _health_conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """SELECT role, content, created_at
               FROM telegram_messages
               WHERE user_id = %s
               ORDER BY created_at DESC
               LIMIT %s""",
            (user_id, limit),
        )
        rows = [dict(r) for r in cur.fetchall()]
        rows.reverse()
        return rows
    finally:
        conn.close()


def clear_telegram_history(chat_id: int) -> None:
    conn = _health_conn()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM telegram_messages WHERE chat_id = %s", (chat_id,))
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Cross-channel context retrieval
# ---------------------------------------------------------------------------

def get_recent_web_messages(
    user_email: str,
    limit: int = MAX_CROSS_CHANNEL_MESSAGES,
    hours: int = CONTEXT_WINDOW_HOURS,
) -> list[dict]:
    """Fetch recent web chat messages from Chainlit's DB for a user."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    try:
        conn = _chat_conn()
    except Exception:
        log.debug("Cannot connect to healthcoach_chat DB", exc_info=True)
        return []
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """SELECT s."type", s."output", s."createdAt"
               FROM steps s
               JOIN threads t ON t."id" = s."threadId"
               JOIN users u ON u."id" = t."userId"
               WHERE u."identifier" = %s
                 AND s."type" IN ('user_message', 'assistant_message')
                 AND s."output" IS NOT NULL AND s."output" != ''
                 AND s."createdAt" > %s
               ORDER BY s."createdAt" DESC
               LIMIT %s""",
            (user_email, cutoff, limit),
        )
        rows = [dict(r) for r in cur.fetchall()]
        rows.reverse()
        return rows
    except Exception:
        log.debug("Failed to query web chat history", exc_info=True)
        return []
    finally:
        conn.close()


def get_recent_telegram_messages(
    user_id: int,
    limit: int = MAX_CROSS_CHANNEL_MESSAGES,
    hours: int = CONTEXT_WINDOW_HOURS,
) -> list[dict]:
    """Fetch recent Telegram messages for cross-channel context."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    conn = _health_conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """SELECT role, content, created_at
               FROM telegram_messages
               WHERE user_id = %s AND created_at > %s
               ORDER BY created_at DESC
               LIMIT %s""",
            (user_id, cutoff, limit),
        )
        rows = [dict(r) for r in cur.fetchall()]
        rows.reverse()
        return rows
    except Exception:
        log.debug("Failed to query telegram history", exc_info=True)
        return []
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Context formatting for system prompt injection
# ---------------------------------------------------------------------------

def format_web_context(messages: list[dict]) -> str:
    """Format web chat messages as a context block for the Telegram bot."""
    if not messages:
        return ""
    lines = []
    for m in messages:
        role = "User" if m["type"] == "user_message" else "Coach"
        text = m["output"]
        if len(text) > 200:
            text = text[:200] + "..."
        lines.append(f"[{role}]: {text}")
    return (
        "\nRecent web chat context (last 24h):\n"
        + "\n".join(lines)
        + "\n(End of web context)\n"
    )


def format_telegram_context(messages: list[dict]) -> str:
    """Format Telegram messages as a context block for the web chatbot."""
    if not messages:
        return ""
    lines = []
    for m in messages:
        role = "User" if m["role"] == "user" else "Coach"
        text = m["content"]
        if len(text) > 200:
            text = text[:200] + "..."
        lines.append(f"[{role}]: {text}")
    return (
        "\nRecent Telegram chat context (last 24h):\n"
        + "\n".join(lines)
        + "\n(End of Telegram context)\n"
    )
