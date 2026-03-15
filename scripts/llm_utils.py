"""
Shared LLM utilities for cost optimization.

Functions here are used by both chat_app.py and telegram_bot.py
to reduce OpenRouter costs via prompt caching, history trimming,
compact serialization, and tiered model routing.
"""

from __future__ import annotations

import json
from typing import Any

MAX_HISTORY_MESSAGES = 20

# Escalation thresholds for tiered model routing (Option C).
# Start on the cheap base model; escalate to the complex model when the
# conversation shows signs of a hard query (deep tool loops or many
# parallel tool calls).
ESCALATE_ROUND_THRESHOLD = 2       # 0-indexed; round 3 in human terms
ESCALATE_TOOL_CALL_THRESHOLD = 3   # parallel tool calls in a single round


def pick_chat_model(
    round_num: int,
    prev_tool_calls: int,
    already_escalated: bool,
    base_model: str,
    complex_model: str,
    routing: str = "escalate",
) -> tuple[str, bool]:
    """Choose the chat model for the current tool-loop round.

    Returns ``(model_name, is_escalated)``.  Once escalated the caller
    should pass ``already_escalated=True`` for all subsequent rounds so
    the decision is sticky within a single user message.
    """
    if routing != "escalate" or not complex_model:
        return base_model, False
    if already_escalated:
        return complex_model, True
    if round_num >= ESCALATE_ROUND_THRESHOLD or prev_tool_calls >= ESCALATE_TOOL_CALL_THRESHOLD:
        return complex_model, True
    return base_model, False


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
