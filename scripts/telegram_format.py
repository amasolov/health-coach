"""
Convert standard Markdown (as produced by LLMs) into the HTML subset that
Telegram's Bot API accepts.

Telegram HTML supports: <b>, <i>, <u>, <s>, <code>, <pre>, <a href>,
<blockquote>, <tg-spoiler>.  We map common Markdown constructs to these tags.

Reference: https://core.telegram.org/bots/api#html-style
"""

from __future__ import annotations

import re
from html import escape as _html_escape


def md_to_telegram_html(text: str) -> str:
    """Convert Markdown *text* to Telegram-compatible HTML."""
    if not text:
        return ""

    # Split into code-blocks vs prose so we never transform inside code.
    parts = _split_code_blocks(text)

    out: list[str] = []
    for is_code, content in parts:
        if is_code:
            out.append(content)
        else:
            out.append(_convert_prose(content))

    return "".join(out)


# ── Code block extraction ─────────────────────────────────────────────────

_CODE_BLOCK_RE = re.compile(
    r"```(\w*)\n(.*?)```",
    re.DOTALL,
)


def _split_code_blocks(text: str) -> list[tuple[bool, str]]:
    """Return a list of (is_code_html, segment) tuples."""
    parts: list[tuple[bool, str]] = []
    last = 0
    for m in _CODE_BLOCK_RE.finditer(text):
        if m.start() > last:
            parts.append((False, text[last:m.start()]))
        lang = m.group(1)
        code = _html_escape(m.group(2).rstrip("\n"), quote=False)
        if lang:
            parts.append((True, f"<pre><code class=\"language-{lang}\">{code}</code></pre>"))
        else:
            parts.append((True, f"<pre>{code}</pre>"))
        last = m.end()
    if last < len(text):
        parts.append((False, text[last:]))
    return parts


# ── Inline code extraction (protect from further transforms) ──────────────

_INLINE_CODE_RE = re.compile(r"`([^`]+?)`")

_PLACEHOLDER = "\x00IC{}\x00"
_PLACEHOLDER_RE = re.compile(r"\x00IC(\d+)\x00")


def _extract_inline_code(text: str) -> tuple[str, list[str]]:
    """Replace inline code spans with placeholders; return (text, codes)."""
    codes: list[str] = []

    def _repl(m: re.Match) -> str:
        idx = len(codes)
        codes.append(f"<code>{_html_escape(m.group(1))}</code>")
        return _PLACEHOLDER.format(idx)

    return _INLINE_CODE_RE.sub(_repl, text), codes


def _restore_inline_code(text: str, codes: list[str]) -> str:
    def _repl(m: re.Match) -> str:
        return codes[int(m.group(1))]
    return _PLACEHOLDER_RE.sub(_repl, text)


# ── Prose conversion ─────────────────────────────────────────────────────

def _convert_prose(text: str) -> str:
    """Convert a non-code segment of Markdown to Telegram HTML."""
    # 1. Extract inline code first (protect from escaping / transforms).
    text, codes = _extract_inline_code(text)

    # 2. Convert `* list` markers to bullets BEFORE escaping / italic so
    #    they are not mistaken for italic delimiters.
    text = re.sub(r"(?m)^\*\s+", "• ", text)

    # 3. Unescape any pre-existing HTML entities, then re-escape cleanly
    #    so we never double-encode (e.g. &amp; in source stays &amp;).
    from html import unescape as _html_unescape
    text = _html_unescape(text)
    text = _html_escape(text, quote=False)

    # 4. Links — must be before bold/italic to avoid mangling URLs.
    text = re.sub(
        r"\[([^\]]+)\]\(([^)]+)\)",
        r'<a href="\2">\1</a>',
        text,
    )

    # 5. Bold-italic (***text*** or ___text___)
    text = re.sub(r"\*{3}(.+?)\*{3}", r"<b><i>\1</i></b>", text)
    text = re.sub(r"_{3}(.+?)_{3}", r"<b><i>\1</i></b>", text)

    # 6. Bold (**text** or __text__)
    text = re.sub(r"\*{2}(.+?)\*{2}", r"<b>\1</b>", text)
    text = re.sub(r"__(.+?)__", r"<b>\1</b>", text)

    # 7. Italic (*text* or _text_) — only when not part of a word
    text = re.sub(r"(?<!\w)\*([^*]+?)\*(?!\w)", r"<i>\1</i>", text)
    text = re.sub(r"(?<!\w)_([^_]+?)_(?!\w)", r"<i>\1</i>", text)

    # 8. Strikethrough (~~text~~)
    text = re.sub(r"~~(.+?)~~", r"<s>\1</s>", text)

    # 9. Process line-level constructs (headings, lists, blockquotes, hrs).
    text = _process_lines(text)

    # 10. Restore inline code placeholders.
    text = _restore_inline_code(text, codes)

    return text


# ── Line-level transforms ────────────────────────────────────────────────

def _process_lines(text: str) -> str:
    lines = text.split("\n")
    out: list[str] = []
    in_blockquote = False
    bq_lines: list[str] = []

    def _flush_bq():
        nonlocal in_blockquote
        if bq_lines:
            out.append("<blockquote>" + "\n".join(bq_lines) + "</blockquote>")
            bq_lines.clear()
        in_blockquote = False

    for line in lines:
        stripped = line.strip()

        # Blockquote
        if stripped.startswith("&gt; ") or stripped == "&gt;":
            content = stripped[5:] if stripped.startswith("&gt; ") else ""
            bq_lines.append(content)
            in_blockquote = True
            continue
        elif in_blockquote:
            _flush_bq()

        # Horizontal rule (---, ***, ___)
        if re.match(r"^[-*_]{3,}$", stripped) and len(stripped) >= 3:
            out.append("")
            continue

        # Headings
        heading_m = re.match(r"^(#{1,6})\s+(.+)$", stripped)
        if heading_m:
            out.append(f"\n<b>{heading_m.group(2)}</b>\n")
            continue

        # Unordered list items (- item or * item, but not **bold**)
        list_m = re.match(r"^[-*]\s+(.+)$", stripped)
        if list_m:
            out.append(f"• {list_m.group(1)}")
            continue

        out.append(line)

    _flush_bq()
    return "\n".join(out)


# ── HTML-safe chunking ───────────────────────────────────────────────────

def chunk_html(text: str, limit: int = 4096) -> list[str]:
    """Split *text* into chunks of at most *limit* characters.

    Tries to split at newline boundaries to avoid breaking mid-tag.
    """
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        split_at = text.rfind("\n", 0, limit)
        if split_at < limit // 2:
            split_at = limit
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    return chunks
