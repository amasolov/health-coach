"""
Tests for Telegram bot message sending — HTML formatting + fallback.

These tests verify that:
1. LLM responses are converted to Telegram HTML before sending
2. Messages are sent with parse_mode="HTML"
3. If Telegram rejects the HTML (BadRequest), we fall back to plain text
4. The chunk_message helper splits correctly for the 4096-char limit
5. Chart messages are not affected by the HTML formatting
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from scripts.telegram_bot import _chunk_message
from scripts.telegram_format import md_to_telegram_html, chunk_html


# ── _chunk_message (existing helper) ─────────────────────────────────────

class TestChunkMessage:
    def test_short_message(self):
        assert _chunk_message("hello") == ["hello"]

    def test_splits_long_message(self):
        text = "line\n" * 2000
        chunks = _chunk_message(text, limit=500)
        assert len(chunks) > 1
        for c in chunks:
            assert len(c) <= 500

    def test_prefers_newline_boundary(self):
        text = "a" * 100 + "\n" + "b" * 100
        chunks = _chunk_message(text, limit=120)
        assert chunks[0] == "a" * 100


# ── _send_reply integration ──────────────────────────────────────────────

class TestSendReply:
    """Test the _send_reply helper that formats + sends with fallback."""

    def test_sends_html_parse_mode(self):
        from scripts.telegram_bot import _send_reply

        mock_message = AsyncMock()
        mock_message.reply_text = AsyncMock()

        asyncio.run(_send_reply(mock_message, "**bold text**"))

        mock_message.reply_text.assert_called()
        call_kwargs = mock_message.reply_text.call_args
        assert call_kwargs.kwargs.get("parse_mode") == "HTML"
        assert "<b>bold text</b>" in call_kwargs.args[0]

    def test_plain_text_passes_through(self):
        from scripts.telegram_bot import _send_reply

        mock_message = AsyncMock()
        mock_message.reply_text = AsyncMock()

        asyncio.run(_send_reply(mock_message, "just plain text"))

        call_args = mock_message.reply_text.call_args
        assert call_args.args[0] == "just plain text"

    def test_falls_back_on_bad_request(self):
        """If Telegram rejects HTML (BadRequest), resend as plain text."""
        from telegram.error import BadRequest
        from scripts.telegram_bot import _send_reply

        mock_message = AsyncMock()
        call_count = 0

        async def _side_effect(text, **kwargs):
            nonlocal call_count
            call_count += 1
            if kwargs.get("parse_mode") == "HTML":
                raise BadRequest("Can't parse entities")
            return None

        mock_message.reply_text = AsyncMock(side_effect=_side_effect)

        asyncio.run(_send_reply(mock_message, "**bold** text"))

        assert call_count == 2
        last_call = mock_message.reply_text.call_args
        assert last_call.kwargs.get("parse_mode") is None

    def test_chunks_long_html(self):
        """Long messages should be split into multiple sends."""
        from scripts.telegram_bot import _send_reply

        mock_message = AsyncMock()
        mock_message.reply_text = AsyncMock()

        text = "word " * 2000  # ~10,000 chars
        asyncio.run(_send_reply(mock_message, text))

        assert mock_message.reply_text.call_count > 1

    def test_empty_text_sends_nothing(self):
        from scripts.telegram_bot import _send_reply

        mock_message = AsyncMock()
        mock_message.reply_text = AsyncMock()

        asyncio.run(_send_reply(mock_message, ""))
        mock_message.reply_text.assert_not_called()


# ── md_to_telegram_html is applied to LLM output ─────────────────────────

class TestFormatApplied:
    def test_markdown_headings_become_bold(self):
        result = md_to_telegram_html("## Training Summary")
        assert "<b>Training Summary</b>" in result

    def test_bullet_list_converted(self):
        result = md_to_telegram_html("- CTL: 42\n- ATL: 38")
        assert "•" in result
        assert "CTL: 42" in result

    def test_code_spans_preserved(self):
        result = md_to_telegram_html("Use `get_zones` to check")
        assert "<code>get_zones</code>" in result


# ── Telegram trims history before each LLM round ─────────────────────────

class TestTelegramTrimBeforeRound:
    """Verify that _trim_history runs before each LLM call, not only after
    the final response.  This prevents context explosion during multi-round
    tool loops."""

    def test_trim_called_before_create(self):
        """Inspect the handle_message source to confirm _trim_history
        precedes client.chat.completions.create inside the tool loop."""
        import inspect
        from scripts import telegram_bot

        source = inspect.getsource(telegram_bot.handle_message)

        loop_start = source.find("for _round in range(MAX_TOOL_ROUNDS)")
        assert loop_start != -1, "Tool loop not found in handle_message"

        loop_body = source[loop_start:]
        trim_pos = loop_body.find("_trim_history(messages)")
        create_pos = loop_body.find("client.chat.completions.create")
        assert trim_pos != -1, "_trim_history not found in tool loop"
        assert create_pos != -1, "chat.completions.create not found in tool loop"
        assert trim_pos < create_pos, (
            "_trim_history must come BEFORE client.chat.completions.create "
            "inside the tool loop to prevent context explosion"
        )
