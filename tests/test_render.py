import random

from bot.services.render import md_to_html, split_markdown


def test_inline_formatting():
    assert md_to_html("**bold** and *it* and ~~del~~") == "<b>bold</b> and <i>it</i> and <s>del</s>"
    assert md_to_html("use `a<b>` here") == "use <code>a&lt;b&gt;</code> here"
    assert md_to_html("[site](https://x.com/a?b=1&c=2)") == '<a href="https://x.com/a?b=1&amp;c=2">site</a>'


def test_snake_case_and_math_not_italic():
    assert md_to_html("my_var_name") == "my_var_name"
    assert md_to_html("2 * 3 * 4") == "2 * 3 * 4"


def test_html_is_escaped():
    assert md_to_html("a < b && c > d") == "a &lt; b &amp;&amp; c &gt; d"


def test_code_block_contents_untouched():
    md = "```python\nx = 1 if a < b else **2**\n```"
    assert md_to_html(md) == '<pre><code class="language-python">x = 1 if a &lt; b else **2**</code></pre>'


def test_unclosed_code_block_is_closed():
    assert md_to_html("```\nprint(1)") == "<pre><code>print(1)</code></pre>"


def test_blocks():
    md = "# Title\n- one\n- **two**\n> quote\n> more\n---\n| a | b |\n| 1 | 2 |"
    assert md_to_html(md) == (
        "<b>Title</b>\n• one\n• <b>two</b>\n<blockquote>quote\nmore</blockquote>\n——————\n"
        "<pre>| a | b |\n| 1 | 2 |</pre>"
    )


def test_split_short_text_is_single_piece():
    assert split_markdown("hello") == ["hello"]


def test_split_respects_limit_and_prefers_paragraphs():
    text = "\n\n".join(f"para {i} " + "x" * 80 for i in range(50))
    pieces = split_markdown(text, limit=500)
    assert len(pieces) > 1
    assert all(len(p) <= 500 for p in pieces)
    assert all(p.startswith("para") for p in pieces)
    assert "".join(p.replace("\n", "") for p in pieces) == text.replace("\n", "")


def test_split_reopens_code_fence():
    text = "intro\n```js\n" + "\n".join(f"line{i}();" for i in range(200)) + "\n```\nafter"
    pieces = split_markdown(text, limit=300)
    assert len(pieces) > 2
    for piece in pieces:
        assert len(piece) <= 300
        assert piece.count("```") % 2 == 0, piece  # every piece is balanced
    assert pieces[1].startswith("```js\n")
    assert pieces[-1].rstrip().endswith("after")


def test_split_hard_cut_without_whitespace():
    pieces = split_markdown("a" * 1000, limit=300)
    assert all(len(p) <= 300 for p in pieces)
    assert "".join(pieces) == "a" * 1000


def test_split_frozen_pieces_never_change_while_streaming():
    rng = random.Random(42)
    words = ["word", "longer_word", "\n", "\n\n", "```py\n", "\n```\n", "x" * 30]
    text = ""
    frozen: list[str] = []
    for _ in range(3000):
        text += rng.choice(words) + " "
        pieces = split_markdown(text, limit=200)
        # Everything except the last piece is final and must never change later.
        assert pieces[: len(frozen)] == frozen
        frozen = pieces[:-1]
    assert len(frozen) > 10
