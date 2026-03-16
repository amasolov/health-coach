"""
Tests for OpenRouter cost optimizations:
- Prompt caching (cache_control on system messages)
- Message history trimming
- Compact JSON for tool results sent to LLM
- Cache hit metrics extraction
- Default model and history limits
- Tiered model routing (escalation)
"""

from __future__ import annotations

import json

import pytest

from scripts.llm_utils import (
    build_system_message,
    trim_history,
    compact_json,
    extract_cache_metrics,
    pick_chat_model,
    classify_message_complexity,
    ESCALATE_ROUND_THRESHOLD,
    ESCALATE_TOOL_CALL_THRESHOLD,
    COMPLEXITY_THRESHOLD,
    MAX_HISTORY_MESSAGES,
)


# ── Default configuration ────────────────────────────────────────────────

class TestCostDefaults:

    def test_default_chat_model_is_gemini_flash(self):
        from scripts.addon_config import AddonConfig
        assert AddonConfig().chat_model == "google/gemini-2.5-flash"

    def test_default_complex_model_is_sonnet(self):
        from scripts.addon_config import AddonConfig
        assert AddonConfig().chat_model_complex == "anthropic/claude-sonnet-4"

    def test_default_model_routing_is_escalate(self):
        from scripts.addon_config import AddonConfig
        assert AddonConfig().model_routing == "escalate"

    def test_history_limit_is_20(self):
        assert MAX_HISTORY_MESSAGES == 20


# ── Tiered model routing ─────────────────────────────────────────────────

FLASH = "google/gemini-2.5-flash"
SONNET = "anthropic/claude-sonnet-4"


class TestPickChatModel:

    def test_round_0_uses_base_model(self):
        model, escalated = pick_chat_model(
            round_num=0, prev_tool_calls=0, already_escalated=False,
            base_model=FLASH, complex_model=SONNET,
        )
        assert model == FLASH
        assert escalated is False

    def test_round_1_few_tools_stays_on_base(self):
        model, escalated = pick_chat_model(
            round_num=1, prev_tool_calls=2, already_escalated=False,
            base_model=FLASH, complex_model=SONNET,
        )
        assert model == FLASH
        assert escalated is False

    def test_escalates_on_many_tool_calls(self):
        model, escalated = pick_chat_model(
            round_num=1, prev_tool_calls=ESCALATE_TOOL_CALL_THRESHOLD,
            already_escalated=False,
            base_model=FLASH, complex_model=SONNET,
        )
        assert model == SONNET
        assert escalated is True

    def test_escalates_on_round_threshold(self):
        model, escalated = pick_chat_model(
            round_num=ESCALATE_ROUND_THRESHOLD, prev_tool_calls=0,
            already_escalated=False,
            base_model=FLASH, complex_model=SONNET,
        )
        assert model == SONNET
        assert escalated is True

    def test_stays_escalated_once_triggered(self):
        model, escalated = pick_chat_model(
            round_num=0, prev_tool_calls=0, already_escalated=True,
            base_model=FLASH, complex_model=SONNET,
        )
        assert model == SONNET
        assert escalated is True

    def test_routing_off_always_uses_base(self):
        model, escalated = pick_chat_model(
            round_num=5, prev_tool_calls=10, already_escalated=False,
            base_model=FLASH, complex_model=SONNET,
            routing="off",
        )
        assert model == FLASH
        assert escalated is False

    def test_no_complex_model_stays_on_base(self):
        model, escalated = pick_chat_model(
            round_num=5, prev_tool_calls=10, already_escalated=False,
            base_model=FLASH, complex_model="",
        )
        assert model == FLASH
        assert escalated is False

    def test_thresholds_are_sensible(self):
        assert ESCALATE_ROUND_THRESHOLD >= 2
        assert ESCALATE_TOOL_CALL_THRESHOLD >= 3


# ── Semantic message classification ───────────────────────────────────────

class TestClassifyMessageComplexity:
    """classify_message_complexity returns True for messages that warrant
    the complex model upfront, False for simple lookups and greetings."""

    # --- Simple messages (should stay on cheap model) -------------------------

    def test_greeting_is_simple(self):
        assert classify_message_complexity("Hi") is False

    def test_short_thanks_is_simple(self):
        assert classify_message_complexity("Thanks!") is False

    def test_single_metric_query_is_simple(self):
        assert classify_message_complexity("What's my CTL?") is False

    def test_sleep_query_is_simple(self):
        assert classify_message_complexity("How did I sleep last night?") is False

    def test_vitals_query_is_simple(self):
        assert classify_message_complexity("Show my vitals") is False

    def test_readiness_query_is_simple(self):
        assert classify_message_complexity("Am I ready to train today?") is False

    def test_yesterday_summary_is_simple(self):
        assert classify_message_complexity("What did I do yesterday?") is False

    def test_empty_string_is_simple(self):
        assert classify_message_complexity("") is False

    def test_single_word_is_simple(self):
        assert classify_message_complexity("yes") is False

    # --- Complex messages (should escalate upfront) ---------------------------

    def test_multipart_question_is_complex(self):
        msg = "How did I sleep last week? And how does that compare to my training load?"
        assert classify_message_complexity(msg) is True

    def test_comparison_request_is_complex(self):
        msg = "Compare my running volume and sleep quality over the last 4 weeks"
        assert classify_message_complexity(msg) is True

    def test_training_plan_is_complex(self):
        msg = "Create a training plan for my half marathon in 8 weeks"
        assert classify_message_complexity(msg) is True

    def test_analysis_with_trend_is_complex(self):
        msg = "Analyze the trend in my recovery scores and correlate with training stress"
        assert classify_message_complexity(msg) is True

    def test_multifactor_reasoning_is_complex(self):
        msg = ("Why has my recovery been declining? Could it be related to "
               "my increased training volume or sleep patterns?")
        assert classify_message_complexity(msg) is True

    def test_stepbystep_request_is_complex(self):
        msg = "Walk me through how to adjust my training zones step by step"
        assert classify_message_complexity(msg) is True

    def test_periodization_is_complex(self):
        msg = "Design a periodization program for the next 12 weeks"
        assert classify_message_complexity(msg) is True

    def test_optimize_with_context_is_complex(self):
        msg = ("Analyze my zone distribution and compare it to the 80/20 principle. "
               "What should I change?")
        assert classify_message_complexity(msg) is True

    # --- Edge cases -----------------------------------------------------------

    def test_single_weak_signal_not_enough(self):
        """A single weak keyword alone shouldn't trigger escalation."""
        assert classify_message_complexity("Why did I feel tired?") is False

    def test_short_message_never_complex(self):
        """Messages under 20 chars are never complex, even with keywords."""
        assert classify_message_complexity("Compare vs plan") is False

    def test_threshold_is_sensible(self):
        assert COMPLEXITY_THRESHOLD >= 2


# ── Semantic routing integrates with pick_chat_model ─────────────────────

class TestSemanticRoutingIntegration:
    """Verify that semantic pre-classification feeds correctly into
    the existing pick_chat_model flow."""

    def test_complex_message_escalates_on_round_0(self):
        """If caller pre-classifies as complex, round 0 uses complex model."""
        is_complex = classify_message_complexity(
            "Create a training plan for my marathon and analyze my fitness trends"
        )
        model, escalated = pick_chat_model(
            round_num=0, prev_tool_calls=0, already_escalated=is_complex,
            base_model=FLASH, complex_model=SONNET,
        )
        assert model == SONNET
        assert escalated is True

    def test_simple_message_stays_on_base_round_0(self):
        """If caller pre-classifies as simple, round 0 uses base model."""
        is_complex = classify_message_complexity("How did I sleep?")
        model, escalated = pick_chat_model(
            round_num=0, prev_tool_calls=0, already_escalated=is_complex,
            base_model=FLASH, complex_model=SONNET,
        )
        assert model == FLASH
        assert escalated is False

    def test_simple_message_still_escalates_mechanically(self):
        """Mechanical escalation still works as fallback for simple messages
        that turn out to need deep tool loops."""
        is_complex = classify_message_complexity("What's my CTL?")
        assert is_complex is False
        model, escalated = pick_chat_model(
            round_num=ESCALATE_ROUND_THRESHOLD, prev_tool_calls=0,
            already_escalated=is_complex,
            base_model=FLASH, complex_model=SONNET,
        )
        assert model == SONNET
        assert escalated is True


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


# ── Model escalation wired into frontends ─────────────────────────────────

class TestEscalationWiring:

    def test_chat_app_uses_pick_chat_model(self):
        import inspect
        from scripts import chat_app
        source = inspect.getsource(chat_app.on_message)
        assert "pick_chat_model" in source, (
            "chat_app.on_message must use pick_chat_model for model routing"
        )

    def test_telegram_uses_pick_chat_model(self):
        import inspect
        from scripts import telegram_bot
        source = inspect.getsource(telegram_bot.handle_message)
        assert "pick_chat_model" in source, (
            "telegram_bot.handle_message must use pick_chat_model for model routing"
        )


class TestSemanticRoutingWiring:

    def test_chat_app_uses_classify_message_complexity(self):
        import inspect
        from scripts import chat_app
        source = inspect.getsource(chat_app.on_message)
        assert "classify_message_complexity" in source, (
            "chat_app.on_message must use classify_message_complexity "
            "for semantic pre-routing"
        )

    def test_telegram_uses_classify_message_complexity(self):
        import inspect
        from scripts import telegram_bot
        source = inspect.getsource(telegram_bot.handle_message)
        assert "classify_message_complexity" in source, (
            "telegram_bot.handle_message must use classify_message_complexity "
            "for semantic pre-routing"
        )


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
