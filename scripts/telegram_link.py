"""
Telegram account linking helpers.

Used by chat_app.py (generate codes) and telegram_bot.py (validate codes,
look up users by Telegram chat ID).
"""

from __future__ import annotations

import os
import secrets
import string

import psycopg2


def _get_conn():
    return psycopg2.connect(
        host=os.environ.get("DB_HOST", "localhost"),
        port=os.environ.get("DB_PORT", "5432"),
        dbname=os.environ.get("DB_NAME", "health"),
        user=os.environ.get("DB_USER", "postgres"),
        password=os.environ.get("DB_PASSWORD", ""),
    )


_CODE_ALPHABET = string.ascii_uppercase + string.digits
_CODE_LENGTH = 6
_EXPIRY_MINUTES = 10


def generate_link_code(user_id: int) -> str:
    """Create a one-time 6-char alphanumeric link code with 10-min expiry."""
    code = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_LENGTH))
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM telegram_link_codes WHERE user_id = %s", (user_id,))
        cur.execute(
            "INSERT INTO telegram_link_codes (code, user_id, expires_at) "
            "VALUES (%s, %s, NOW() + INTERVAL '%s minutes')",
            (code, user_id, _EXPIRY_MINUTES),
        )
        conn.commit()
    finally:
        conn.close()
    return code


def validate_link_code(code: str) -> int | None:
    """Return user_id if code is valid and not expired, then delete it."""
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM telegram_link_codes WHERE expires_at < NOW()")
        cur.execute(
            "SELECT user_id FROM telegram_link_codes "
            "WHERE code = %s AND expires_at >= NOW()",
            (code.upper().strip(),),
        )
        row = cur.fetchone()
        if not row:
            return None
        user_id = row[0]
        cur.execute("DELETE FROM telegram_link_codes WHERE code = %s", (code.upper().strip(),))
        conn.commit()
        return user_id
    finally:
        conn.close()


def set_telegram_chat_id(user_id: int, chat_id: int) -> bool:
    """Store the Telegram chat ID for a user. Returns True on success."""
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE users SET telegram_chat_id = %s WHERE id = %s",
            (chat_id, user_id),
        )
        conn.commit()
        return cur.rowcount > 0
    except psycopg2.IntegrityError:
        conn.rollback()
        return False
    finally:
        conn.close()


def remove_telegram_chat_id(chat_id: int) -> bool:
    """Unlink a Telegram chat ID. Returns True if a row was updated."""
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE users SET telegram_chat_id = NULL WHERE telegram_chat_id = %s",
            (chat_id,),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def get_user_by_telegram(chat_id: int) -> dict | None:
    """Look up a user by their linked Telegram chat ID.

    Returns a dict with id, slug, display_name or None.
    """
    conn = _get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, slug, display_name FROM users WHERE telegram_chat_id = %s",
            (chat_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        return {"id": row[0], "slug": row[1], "display_name": row[2]}
    finally:
        conn.close()
