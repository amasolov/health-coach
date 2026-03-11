"""
Tests for scripts.telegram_format — markdown-to-Telegram-HTML conversion.

The LLM produces standard Markdown; Telegram requires its own subset of HTML
(or MarkdownV2 with heavy escaping).  We convert to HTML for reliability.
"""

from __future__ import annotations

import pytest

from scripts.telegram_format import md_to_telegram_html, chunk_html


# ── Bold ──────────────────────────────────────────────────────────────────

class TestBold:
    def test_double_asterisks(self):
        assert md_to_telegram_html("**hello**") == "<b>hello</b>"

    def test_double_underscores(self):
        assert md_to_telegram_html("__hello__") == "<b>hello</b>"

    def test_mid_sentence(self):
        result = md_to_telegram_html("this is **important** stuff")
        assert result == "this is <b>important</b> stuff"

    def test_multiple_bold_spans(self):
        result = md_to_telegram_html("**a** and **b**")
        assert result == "<b>a</b> and <b>b</b>"

    def test_bold_with_punctuation_inside(self):
        result = md_to_telegram_html("**hello, world!**")
        assert result == "<b>hello, world!</b>"


# ── Italic ────────────────────────────────────────────────────────────────

class TestItalic:
    def test_single_asterisk(self):
        assert md_to_telegram_html("*hello*") == "<i>hello</i>"

    def test_single_underscore(self):
        assert md_to_telegram_html("_hello_") == "<i>hello</i>"

    def test_italic_in_sentence(self):
        result = md_to_telegram_html("this is *subtle* emphasis")
        assert result == "this is <i>subtle</i> emphasis"


# ── Bold + Italic ─────────────────────────────────────────────────────────

class TestBoldItalic:
    def test_bold_italic_triple_asterisk(self):
        result = md_to_telegram_html("***bold italic***")
        assert "<b>" in result and "<i>" in result
        assert "bold italic" in result

    def test_bold_then_italic(self):
        result = md_to_telegram_html("**bold** and *italic*")
        assert "<b>bold</b>" in result
        assert "<i>italic</i>" in result


# ── Strikethrough ─────────────────────────────────────────────────────────

class TestStrikethrough:
    def test_double_tilde(self):
        assert md_to_telegram_html("~~deleted~~") == "<s>deleted</s>"

    def test_mid_sentence(self):
        result = md_to_telegram_html("this is ~~wrong~~ right")
        assert result == "this is <s>wrong</s> right"


# ── Inline code ───────────────────────────────────────────────────────────

class TestInlineCode:
    def test_simple(self):
        assert md_to_telegram_html("`code`") == "<code>code</code>"

    def test_in_sentence(self):
        result = md_to_telegram_html("use `get_data()` here")
        assert result == "use <code>get_data()</code> here"

    def test_html_entities_inside_code(self):
        result = md_to_telegram_html("`a < b && c > d`")
        assert "<code>a &lt; b &amp;&amp; c &gt; d</code>" in result

    def test_no_nested_formatting_inside_code(self):
        result = md_to_telegram_html("`**not bold**`")
        assert "<b>" not in result
        assert "<code>**not bold**</code>" in result


# ── Code blocks ───────────────────────────────────────────────────────────

class TestCodeBlock:
    def test_fenced_no_language(self):
        text = "```\nprint('hi')\n```"
        result = md_to_telegram_html(text)
        assert "<pre>" in result
        assert "print('hi')" in result
        assert "</pre>" in result

    def test_fenced_with_language(self):
        text = "```python\ndef f():\n    pass\n```"
        result = md_to_telegram_html(text)
        assert "<pre>" in result
        assert "<code" in result
        assert "def f():" in result

    def test_html_entities_inside_code_block(self):
        text = "```\nif a < b && c > d:\n```"
        result = md_to_telegram_html(text)
        assert "&lt;" in result
        assert "&gt;" in result
        assert "&amp;" in result

    def test_no_markdown_processing_inside_block(self):
        text = "```\n**not bold** _not italic_\n```"
        result = md_to_telegram_html(text)
        assert "<b>" not in result
        assert "<i>" not in result
        assert "**not bold**" in result

    def test_surrounding_text_preserved(self):
        text = "before\n```\ncode\n```\nafter"
        result = md_to_telegram_html(text)
        assert "before" in result
        assert "after" in result
        assert "<pre>" in result


# ── Links ─────────────────────────────────────────────────────────────────

class TestLinks:
    def test_inline_link(self):
        result = md_to_telegram_html("[click here](https://example.com)")
        assert result == '<a href="https://example.com">click here</a>'

    def test_link_in_sentence(self):
        result = md_to_telegram_html("visit [docs](https://docs.io) now")
        assert '<a href="https://docs.io">docs</a>' in result

    def test_multiple_links(self):
        result = md_to_telegram_html("[a](http://a) and [b](http://b)")
        assert '<a href="http://a">a</a>' in result
        assert '<a href="http://b">b</a>' in result


# ── Headings ──────────────────────────────────────────────────────────────

class TestHeadings:
    def test_h1(self):
        result = md_to_telegram_html("# Title")
        assert "<b>Title</b>" in result

    def test_h2(self):
        result = md_to_telegram_html("## Section")
        assert "<b>Section</b>" in result

    def test_h3(self):
        result = md_to_telegram_html("### Subsection")
        assert "<b>Subsection</b>" in result

    def test_heading_only_at_line_start(self):
        result = md_to_telegram_html("not # a heading")
        assert "<b>" not in result
        assert "not # a heading" in result


# ── Lists ─────────────────────────────────────────────────────────────────

class TestLists:
    def test_unordered_dash(self):
        result = md_to_telegram_html("- item one\n- item two")
        assert "• item one" in result
        assert "• item two" in result

    def test_unordered_asterisk(self):
        result = md_to_telegram_html("* item one\n* item two")
        assert "• item one" in result
        assert "• item two" in result

    def test_ordered_list(self):
        text = "1. first\n2. second\n3. third"
        result = md_to_telegram_html(text)
        assert "1. first" in result
        assert "2. second" in result

    def test_nested_bold_in_list(self):
        result = md_to_telegram_html("- **bold item**\n- normal item")
        assert "<b>bold item</b>" in result
        assert "• normal item" in result


# ── Blockquotes ───────────────────────────────────────────────────────────

class TestBlockquotes:
    def test_single_line(self):
        result = md_to_telegram_html("> quoted text")
        assert "<blockquote>" in result
        assert "quoted text" in result
        assert "</blockquote>" in result

    def test_multi_line(self):
        result = md_to_telegram_html("> line one\n> line two")
        assert "line one" in result
        assert "line two" in result
        count = result.count("<blockquote>")
        assert count == 1, "consecutive quote lines should merge"


# ── Horizontal rule ──────────────────────────────────────────────────────

class TestHorizontalRule:
    def test_triple_dash(self):
        result = md_to_telegram_html("above\n---\nbelow")
        assert "above" in result
        assert "below" in result
        assert "---" not in result


# ── HTML entity escaping ─────────────────────────────────────────────────

class TestHtmlEscaping:
    def test_ampersand(self):
        assert "&amp;" in md_to_telegram_html("a & b")

    def test_angle_brackets(self):
        result = md_to_telegram_html("a < b > c")
        assert "&lt;" in result
        assert "&gt;" in result

    def test_entities_not_double_escaped(self):
        result = md_to_telegram_html("&amp; already")
        assert "&amp;amp;" not in result

    def test_preserves_emoji(self):
        result = md_to_telegram_html("great job! 💪🏻")
        assert "💪🏻" in result


# ── Mixed / realistic LLM output ─────────────────────────────────────────

class TestRealisticOutput:
    def test_fitness_summary(self):
        text = (
            "## Training Summary\n"
            "\n"
            "Here's your **weekly overview**:\n"
            "\n"
            "- **CTL**: 42.3 (up from 40.1)\n"
            "- **ATL**: 38.7\n"
            "- **TSB**: +3.6 — *fresh*\n"
            "\n"
            "Your ramp rate is 5.5%/week, which is within the safe range.\n"
            "\n"
            "> Keep up the consistency!\n"
        )
        result = md_to_telegram_html(text)
        assert "<b>Training Summary</b>" in result
        assert "<b>weekly overview</b>" in result
        assert "<b>CTL</b>" in result
        assert "<i>fresh</i>" in result
        assert "<blockquote>" in result
        assert "5.5%/week" in result

    def test_code_in_response(self):
        text = (
            "You can check your zones with `get_training_zones`.\n"
            "\n"
            "```json\n"
            '{"zone": 2, "min_hr": 130, "max_hr": 145}\n'
            "```\n"
        )
        result = md_to_telegram_html(text)
        assert "<code>get_training_zones</code>" in result
        assert "<pre>" in result
        assert "&quot;" in result or '"' in result

    def test_empty_string(self):
        assert md_to_telegram_html("") == ""

    def test_plain_text_passthrough(self):
        text = "No formatting here, just plain text."
        result = md_to_telegram_html(text)
        assert result == "No formatting here, just plain text."

    def test_numbers_and_percentages(self):
        result = md_to_telegram_html("Your HRV is 45ms (down 12%)")
        assert "Your HRV is 45ms (down 12%)" == result


# ── chunk_html — safe splitting of HTML messages ─────────────────────────

class TestChunkHtml:
    def test_short_message_single_chunk(self):
        text = "<b>hello</b>"
        chunks = chunk_html(text, limit=4096)
        assert chunks == [text]

    def test_splits_at_newline(self):
        lines = ["line " + str(i) for i in range(200)]
        text = "\n".join(lines)
        chunks = chunk_html(text, limit=200)
        assert len(chunks) > 1
        reconstructed = "\n".join(chunks)
        for line in lines:
            assert line in reconstructed

    def test_respects_limit(self):
        text = "x" * 5000
        chunks = chunk_html(text, limit=4096)
        for c in chunks:
            assert len(c) <= 4096

    def test_empty_string(self):
        assert chunk_html("", limit=4096) == [""]
