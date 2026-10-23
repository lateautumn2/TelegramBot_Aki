import pytest
from aiogram.types import Chat, User

from bot.logging_setup import redact
from bot.services.context import build_messages, estimate_tokens, scope_key
from bot.services.crypto import SecretBox, mask_key
from bot.services.llm import normalize_base_url


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://api.example.com", "https://api.example.com/v1"),
        ("https://api.example.com/", "https://api.example.com/v1"),
        ("https://api.example.com/v1", "https://api.example.com/v1"),
        ("https://api.example.com/v1/chat/completions", "https://api.example.com/v1"),
        ("https://x.com/api/paas/v4/", "https://x.com/api/paas/v4"),
        ("  http://localhost:11434/v1  ", "http://localhost:11434/v1"),
    ],
)
def test_normalize_base_url(raw, expected):
    assert normalize_base_url(raw) == expected


@pytest.mark.parametrize("bad", ["", "api.example.com", "ftp://x.com"])
def test_normalize_base_url_rejects_garbage(bad):
    with pytest.raises(ValueError):
        normalize_base_url(bad)


def test_estimate_tokens_counts_cjk_per_char():
    assert estimate_tokens("你好世界") > estimate_tokens("abcd")


def test_build_messages_keeps_recent_history_within_budget():
    history = [("user", "q" * 400), ("assistant", "a" * 400)] * 10
    msgs = build_messages("sys", history, "now", token_budget=500)
    assert msgs[0] == {"role": "system", "content": "sys"}
    assert msgs[-1] == {"role": "user", "content": "now"}
    assert msgs[1]["role"] == "user"  # never starts with an orphaned assistant reply
    assert 2 < len(msgs) < 22
    assert sum(estimate_tokens(m["content"]) for m in msgs) <= 500


def test_build_messages_without_system_prompt():
    assert build_messages("", [], "hi", 1000) == [{"role": "user", "content": "hi"}]


def test_scope_key():
    user = User(id=7, is_bot=False, first_name="u")
    assert scope_key(Chat(id=7, type="private"), user) == "p:7"
    assert scope_key(Chat(id=-100123, type="supergroup"), user) == "g:-100123:7"


def test_secret_box_roundtrip_and_wrong_key():
    box = SecretBox("a-long-enough-secret")
    token = box.encrypt("sk-abc")
    assert "sk-abc" not in token
    assert box.decrypt(token) == "sk-abc"
    with pytest.raises(ValueError):
        SecretBox("another-secret-value").decrypt(token)


def test_mask_and_redact():
    assert mask_key("sk-1234567890abcdef") == "sk-1****cdef"
    fake_bot_token = "9" * 9 + ":" + "A" * 35
    line = f"key sk-1234567890abcdef token {fake_bot_token} Bearer abcdefghijk"
    out = redact(line)
    assert "1234567890abcdef" not in out and "AAAAA" not in out and "abcdefghijk" not in out
