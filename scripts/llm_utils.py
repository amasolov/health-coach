"""
Shared LLM utilities for cost optimization.

Functions here are used by both chat_app.py and telegram_bot.py
to reduce OpenRouter costs via prompt caching, history trimming,
compact serialization, and tiered model routing.
"""

from __future__ import annotations

import json
import re
from typing import Any

MAX_HISTORY_MESSAGES = 20

# Escalation thresholds for tiered model routing (Option C).
# Start on the cheap base model; escalate to the complex model when the
# conversation shows signs of a hard query (deep tool loops or many
# parallel tool calls).
ESCALATE_ROUND_THRESHOLD = 2       # 0-indexed; round 3 in human terms
ESCALATE_TOOL_CALL_THRESHOLD = 3   # parallel tool calls in a single round

# ---------------------------------------------------------------------------
# Semantic complexity classification
# ---------------------------------------------------------------------------
# Each pattern carries a weight.  When the sum meets COMPLEXITY_THRESHOLD
# the message is pre-escalated to the complex model *before* the first
# LLM round, saving wasted cheap-model rounds on inherently hard queries.
# ---------------------------------------------------------------------------

COMPLEXITY_THRESHOLD = 3
_MIN_LENGTH_FOR_COMPLEX = 20

_COMPLEX_SIGNALS: list[tuple[re.Pattern[str], int]] = [
    # Multi-part questions (2+ question marks)
    (re.compile(r"\?[^?]+\?"), 2),
    # Explicit comparison / contrastive analysis
    (re.compile(r"\b(compare|contrast|versus|vs\.?)\b", re.I), 2),
    # Analytical / correlative reasoning
    (re.compile(r"\b(analy[sz]e|correlat[ei]|relationship\s+between)\b", re.I), 2),
    # Training-plan / periodisation requests (high-confidence complex)
    (re.compile(r"\b(training\s+plan|race\s+plan|periodiz\w*|build\s+a\s+program)\b", re.I), 3),
    (re.compile(r"\b(create\s+a\s+plan|design\s+a\s+(program|plan)|prepare\s+for)\b", re.I), 3),
    # Step-by-step / detailed walkthrough
    (re.compile(r"\b(step[\s-]by[\s-]step|break\s+down|walk\s+me\s+through)\b", re.I), 2),
    # Temporal multi-point analysis
    (re.compile(r"\b(trend|progression|trajectory)\b", re.I), 1),
    (re.compile(r"\bover\s+the\s+(last|past)\s+\d+", re.I), 1),
    # Causal / explanatory reasoning
    (re.compile(r"\b(explain\s+why|how\s+does\s+\w+\s+affect|what\s+caused)\b", re.I), 1),
    # Correlation / relatedness questions
    (re.compile(r"\brelated\s+to\b", re.I), 1),
    # Multi-factor consideration
    (re.compile(r"\b(considering|taking\s+into\s+account|factoring\s+in)\b", re.I), 1),
    # Optimisation + possessive ("optimize my …")
    (re.compile(r"\b(adjust|optimiz[ei]|improve)\s+(my|the)\b", re.I), 1),
]


def classify_message_complexity(user_message: str) -> bool:
    """Return *True* when *user_message* looks complex enough to justify
    starting on the expensive model.

    The function is deterministic, zero-cost (regex only) and intended to
    be called once per user message, before the tool loop begins.
    """
    if len(user_message) < _MIN_LENGTH_FOR_COMPLEX:
        return False

    score = 0
    for pattern, weight in _COMPLEX_SIGNALS:
        if pattern.search(user_message):
            score += weight
            if score >= COMPLEXITY_THRESHOLD:
                return True
    return False


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
