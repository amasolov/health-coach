"""
Tests for OpenRouter cost optimizations:
- Prompt caching (cache_control on system messages)
- Message history trimming
- Compact JSON for tool results sent to LLM
- Cache hit metrics extraction
"""

from __future__ import annotations

import json

import pytest

from scripts.llm_utils import (
    build_system_message,
    trim_history,
    compact_json,
    extract_cache_metrics,
    MAX_HISTORY_MESSAGES,
)


# ── History trimming ─────────────────────────────────────────────────────

class TestTrimHistory:

    def test_short_history_untouched(self):
        system = {"role": "system", "content": [{"type": "text", "text": "sys"}]}
        msgs = [system] + [{"role": "user", "content": f"m{i}"} for i in range(5)]
        original_len = len(msgs)

        trim_history(msgs)

        assert len(msgs) == original_len
        assert msgs[0] is system

    def test_long_history_trimmed(self):
        system = {"role": "system", "content": [{"type": "text", "text": "sys"}]}
        msgs = [system] + [{"role": "user", "content": f"m{i}"} for i in range(100)]

        trim_history(msgs)

        assert len(msgs) == MAX_HISTORY_MESSAGES + 1
        assert msgs[0] is system
        assert msgs[-1]["content"] == "m99"

    def test_preserves_system_message(self):
        system = {"role": "system", "content": [{"type": "text", "text": "important"}]}
        msgs = [system] + [{"role": "user", "content": f"m{i}"} for i in range(200)]

        trim_history(msgs)

        assert msgs[0] is system
        assert msgs[0]["content"][0]["text"] == "important"

    def test_custom_max(self):
        system = {"role": "system", "content": "sys"}
        msgs = [system] + [{"role": "user", "content": f"m{i}"} for i in range(30)]

        trim_history(msgs, max_messages=10)

        assert len(msgs) == 11
        assert msgs[0] is system
        assert msgs[1]["content"] == "m20"

    def test_exact_boundary_no_trim(self):
        system = {"role": "system", "content": "sys"}
        msgs = [system] + [{"role": "user", "content": f"m{i}"} for i in range(MAX_HISTORY_MESSAGES)]

        trim_history(msgs)

        assert len(msgs) == MAX_HISTORY_MESSAGES + 1


# ── Prompt caching format ────────────────────────────────────────────────

class TestBuildSystemMessage:

    def test_returns_system_role(self):
        msg = build_system_message("hello")
        assert msg["role"] == "system"

    def test_content_is_array(self):
        msg = build_system_message("hello")
        assert isinstance(msg["content"], list)
        assert len(msg["content"]) >= 1

    def test_has_cache_control(self):
        msg = build_system_message("hello")
        last_block = msg["content"][-1]
        assert last_block["type"] == "text"
        assert last_block["cache_control"] == {"type": "ephemeral"}

    def test_text_preserved(self):
        msg = build_system_message("I am your fitness coach.")
        texts = [b["text"] for b in msg["content"] if b["type"] == "text"]
        assert any("I am your fitness coach." in t for t in texts)


# ── Compact JSON ─────────────────────────────────────────────────────────

class TestCompactJson:

    def test_no_whitespace(self):
        result = compact_json({"key": "value", "nested": {"a": 1}})
        assert " " not in result
        assert "\n" not in result

    def test_shorter_than_indented(self):
        data = {"key": "value", "list": [1, 2, 3], "nested": {"a": "b"}}
        assert len(compact_json(data)) < len(json.dumps(data, indent=2))

    def test_roundtrips(self):
        data = {"status": "ok", "items": [1, 2, 3]}
        assert json.loads(compact_json(data)) == data

    def test_handles_non_serializable(self):
        from datetime import datetime, timezone
        data = {"time": datetime(2026, 1, 1, tzinfo=timezone.utc)}
        result = compact_json(data)
        parsed = json.loads(result)
        assert "2026" in parsed["time"]


# ── Cache metrics extraction ─────────────────────────────────────────────

class TestExtractCacheMetrics:

    def test_with_cached_tokens(self):
        class FakeDetails:
            cached_tokens = 5000

        class FakeUsage:
            prompt_tokens = 8000
            completion_tokens = 200
            prompt_tokens_details = FakeDetails()

        metrics = extract_cache_metrics(FakeUsage())
        assert metrics["cached_tokens"] == 5000

    def test_no_details(self):
        class FakeUsage:
            prompt_tokens = 8000
            completion_tokens = 200
            prompt_tokens_details = None

        metrics = extract_cache_metrics(FakeUsage())
        assert metrics["cached_tokens"] == 0

    def test_none_usage(self):
        metrics = extract_cache_metrics(None)
        assert metrics["cached_tokens"] == 0

    def test_missing_cached_tokens_attr(self):
        class FakeDetails:
            pass

        class FakeUsage:
            prompt_tokens = 100
            completion_tokens = 50
            prompt_tokens_details = FakeDetails()

        metrics = extract_cache_metrics(FakeUsage())
        assert metrics["cached_tokens"] == 0


# ── Telegram sanitize_tool_result compact output ─────────────────────────

class TestTelegramSanitizeCompact:

    def test_sanitize_tool_result_compact(self):
        try:
            from scripts.telegram_bot import sanitize_tool_result
        except ImportError:
            pytest.skip("telegram_bot deps not installed")

        result = {"status": "ok", "data": [1, 2, 3]}
        sanitized = sanitize_tool_result(result)

        assert "\n" not in sanitized
        parsed = json.loads(sanitized)
        assert parsed["status"] == "ok"
