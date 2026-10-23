"""Markdown (as produced by LLMs) -> Telegram HTML, plus length-safe splitting."""

import html
import re

TELEGRAM_LIMIT = 4096
# Headroom below Telegram's hard limit for fence markers added when splitting.
DEFAULT_CHUNK = 4000

_FENCE = re.compile(r"^\s*```\s*([\w+#.-]*)")


def _fence_state(text: str, state: str | None = None) -> str | None:
    """Language of the code fence left open at the end of `text` ('' if no
    language), or None when no fence is open."""
    for line in text.split("\n"):
        m = _FENCE.match(line)
        if m:
            state = None if state is not None else m.group(1)
    return state


def _find_cut(window: str) -> int:
    for sep, min_ratio in (("\n\n", 0.5), ("\n", 0.3), (" ", 0.3)):
        idx = window.rfind(sep)
        if idx > len(window) * min_ratio:
            return idx + len(sep)
    return len(window)


def split_markdown(text: str, limit: int = DEFAULT_CHUNK) -> list[str]:
    """Split into pieces of at most `limit` chars, closing and reopening code
    fences across boundaries.

    Deterministic in the prefix: once a piece is no longer the last one, it never
    changes as more text is appended. Streaming relies on this to freeze pieces.
    """
    pieces: list[str] = []
    pos = 0
    prefix = ""  # fence reopened from the previous piece
    while True:
        remaining = text[pos:]
        budget = limit - len(prefix) - len("\n```")
        if len(remaining) <= budget:
            pieces.append(prefix + remaining)
            return pieces
        cut = _find_cut(remaining[:budget])
        chunk = remaining[:cut]
        lang = _fence_state(chunk, _fence_state(prefix))
        piece = prefix + chunk.rstrip("\n") if lang is None else prefix + chunk.rstrip("\n") + "\n```"
        pieces.append(piece)
        pos += cut
        if lang is None:
            while pos < len(text) and text[pos] == "\n":
                pos += 1
            prefix = ""
        else:
            prefix = f"```{lang}\n"


# ---- inline formatting ----

_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)")
_BOLD = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*|(?<!\w)__(?=\S)(.+?)(?<=\S)__(?!\w)")
_STRIKE = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~")
_ITALIC = re.compile(r"(?<![*\w])\*(?=[^\s*])(.+?)(?<=[^\s*])\*(?![*\w])|(?<!\w)_(?=[^\s_])(.+?)(?<=[^\s_])_(?!\w)")


def _inline(text: str) -> str:
    codes: list[str] = []

    def stash(m: re.Match) -> str:
        codes.append(f"<code>{html.escape(m.group(1), quote=False)}</code>")
        return f"\x00{len(codes) - 1}\x00"

    text = _INLINE_CODE.sub(stash, text)
    text = html.escape(text, quote=False)
    text = _LINK.sub(lambda m: f'<a href="{m.group(2).replace(chr(34), "%22")}">{m.group(1)}</a>', text)
    text = _BOLD.sub(lambda m: f"<b>{m.group(1) or m.group(2)}</b>", text)
    text = _STRIKE.sub(r"<s>\1</s>", text)
    text = _ITALIC.sub(lambda m: f"<i>{m.group(1) or m.group(2)}</i>", text)
    return re.sub(r"\x00(\d+)\x00", lambda m: codes[int(m.group(1))], text)


# ---- block structure ----

_HEADING = re.compile(r"^#{1,6}\s+(.*?)\s*#*\s*$")
_QUOTE = re.compile(r"^>\s?(.*)$")
_BULLET = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_HR = re.compile(r"^\s*([-*_])(\s*\1){2,}\s*$")
_TABLE = re.compile(r"^\s*\|.*\|\s*$")


def md_to_html(md: str) -> str:
    out: list[str] = []
    lines = md.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        fence = _FENCE.match(line)
        if fence:
            lang = fence.group(1)
            body: list[str] = []
            i += 1
            while i < len(lines) and not _FENCE.match(lines[i]):
                body.append(lines[i])
                i += 1
            i += 1  # closing fence (or end of text for an unclosed block)
            code = html.escape("\n".join(body), quote=False)
            attr = f' class="language-{lang}"' if lang else ""
            out.append(f"<pre><code{attr}>{code}</code></pre>")
            continue

        if _TABLE.match(line):
            rows = []
            while i < len(lines) and _TABLE.match(lines[i]):
                rows.append(lines[i].strip())
                i += 1
            out.append(f"<pre>{html.escape(chr(10).join(rows), quote=False)}</pre>")
            continue

        if _QUOTE.match(line):
            quoted = []
            while i < len(lines) and (m := _QUOTE.match(lines[i])):
                quoted.append(_inline(m.group(1)))
                i += 1
            out.append(f"<blockquote>{chr(10).join(quoted)}</blockquote>")
            continue

        if m := _HEADING.match(line):
            out.append(f"<b>{_inline(m.group(1))}</b>")
        elif _HR.match(line):
            out.append("——————")
        elif m := _BULLET.match(line):
            out.append(f"{m.group(1)}• {_inline(m.group(2))}")
        else:
            out.append(_inline(line))
        i += 1
    return "\n".join(out)
