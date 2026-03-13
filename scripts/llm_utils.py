"""
Shared LLM utilities for cost optimization.

Functions here are used by both chat_app.py and telegram_bot.py
to reduce OpenRouter costs via prompt caching, history trimming,
and compact serialization.
"""

from __future__ import annotations

import json
from typing import Any

MAX_HISTORY_MESSAGES = 40


def build_system_message(text: str) -> dict:
    """Build a system message with Anthropic prompt-caching support.

    Returns the message in content-array format with ``cache_control``
    on the last block so that OpenRouter / Anthropic caches the entire
    system prefix (system prompt + tool schemas) across turns.
    """
    return {
        "role": "system",
        "content": [
            {
                "type": "text",
                "text": text,
                "cache_control": {"type": "ephemeral"},
            }
        ],
    }


def trim_history(messages: list[dict], max_messages: int = MAX_HISTORY_MESSAGES) -> None:
    """Keep the system message + the last *max_messages* entries (in-place)."""
    if len(messages) > max_messages + 1:
        system = messages[0]
        messages[:] = [system] + messages[-max_messages:]


def compact_json(obj: Any) -> str:
    """Serialize *obj* to compact JSON (no whitespace) for LLM context."""
    return json.dumps(obj, separators=(",", ":"), default=str)


def extract_cache_metrics(usage: Any) -> dict:
    """Pull prompt-caching stats from an OpenAI-style usage object.

    Returns a dict with ``cached_tokens`` (0 when unavailable).
    """
    if usage is None:
        return {"cached_tokens": 0}

    details = getattr(usage, "prompt_tokens_details", None)
    if details is None:
        return {"cached_tokens": 0}

    return {
        "cached_tokens": getattr(details, "cached_tokens", 0) or 0,
    }
