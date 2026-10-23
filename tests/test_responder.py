from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.methods import SendMessage
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.services.responder import StreamResponder


def bad_request(text: str) -> TelegramBadRequest:
    return TelegramBadRequest(method=SendMessage(chat_id=1, text="x"), message=f"Bad Request: {text}")


class FakeBot:
    def __init__(self, *, drafts_work: bool = True, reject_html: bool = False) -> None:
        self.drafts_work = drafts_work
        self.reject_html = reject_html
        self.calls: list[tuple[str, dict]] = []
        self.sent: dict[int, str] = {}  # message_id -> current text
        self.markups: dict[int, object] = {}  # message_id -> current reply_markup
        self._next_id = 100

    def _check_html(self, kw: dict) -> None:
        if self.reject_html and kw.get("parse_mode"):
            raise bad_request("can't parse entities: unclosed tag")

    async def send_message(self, **kw):
        self._check_html(kw)
        self.calls.append(("send", kw))
        self._next_id += 1
        mid = self._next_id
        self.sent[mid] = kw["text"]
        self.markups[mid] = kw.get("reply_markup")

        async def delete():
            self.calls.append(("delete", {"message_id": mid}))
            self.sent.pop(mid)

        return SimpleNamespace(message_id=mid, delete=delete)

    async def edit_message_text(self, **kw):
        self._check_html(kw)
        mid = kw["message_id"]
        # Like Telegram: only an edit that changes neither text nor keyboard is rejected.
        if self.sent[mid] == kw["text"] and self.markups[mid] == kw.get("reply_markup"):
            raise bad_request("message is not modified")
        self.calls.append(("edit", kw))
        self.sent[mid] = kw["text"]
        self.markups[mid] = kw.get("reply_markup")

    async def send_message_draft(self, **kw):
        if not self.drafts_work:
            raise bad_request("TEXTDRAFT_PEER_INVALID")
        self._check_html(kw)
        self.calls.append(("draft", kw))

    def kinds(self) -> list[str]:
        return [kind for kind, _ in self.calls]


def make(bot: FakeBot, **kw) -> StreamResponder:
    kw.setdefault("use_draft", True)
    return StreamResponder(bot, 1, edit_interval=0, draft_interval=0, **kw)


async def test_draft_then_final_message():
    bot = FakeBot()
    r = make(bot)
    await r.push("Hello")
    await r.push("Hello **world**")
    await r.finish("Hello **world**!")
    assert bot.kinds() == ["draft", "draft", "send"]
    assert bot.calls[1][1]["text"] == "Hello <b>world</b>"
    assert list(bot.sent.values()) == ["Hello <b>world</b>!"]


async def test_long_reply_freezes_pieces_during_draft():
    bot = FakeBot()
    r = make(bot)
    text = ""
    for i in range(400):
        text += f"sentence number {i}. "
        await r.push(text)
    await r.finish(text)
    sends = [kw["text"] for kind, kw in bot.calls if kind == "send"]
    assert len(sends) >= 2
    assert all(len(t) <= 4096 for t in sends)
    assert "".join(sends).replace(" ", "") == text.replace(" ", "")
    # A new draft id is used after each frozen piece.
    draft_ids = {kw["draft_id"] for kind, kw in bot.calls if kind == "draft"}
    assert len(draft_ids) == len(sends)


async def test_falls_back_to_edits_when_drafts_unsupported():
    bot = FakeBot(drafts_work=False)
    r = make(bot)
    await r.push("a")
    await r.push("ab")
    await r.finish("abc")
    assert bot.kinds() == ["send", "edit", "edit"]
    assert r.use_draft is False
    assert list(bot.sent.values()) == ["abc"]


async def test_group_edit_mode_replies_to_user_and_skips_unchanged():
    bot = FakeBot()
    r = make(bot, use_draft=False, reply_to=55)
    await r.push("same")
    await r.push("same")
    await r.finish("same")
    assert bot.kinds() == ["send"]
    assert bot.calls[0][1]["reply_parameters"].message_id == 55


async def test_html_rejected_falls_back_to_plain_text():
    bot = FakeBot(reject_html=True)
    r = make(bot, use_draft=False)
    await r.finish("**x** <y>")
    assert list(bot.sent.values()) == ["**x** <y>"]
    assert bot.calls[0][1]["parse_mode"] is None


async def test_final_shorter_than_stream_deletes_extra_messages():
    bot = FakeBot()
    r = make(bot, use_draft=False)
    await r.push("x " * 3000)  # two pieces
    assert len(bot.sent) == 2
    await r.finish("short")
    assert list(bot.sent.values()) == ["short"]


async def test_push_is_throttled_and_backs_off_on_flood():
    bot = FakeBot()
    r = StreamResponder(bot, 1, use_draft=True, edit_interval=60, draft_interval=60)
    await r.push("a")
    await r.push("ab")  # inside interval: dropped
    assert bot.kinds() == ["draft"]

    async def flood(**kw):
        raise TelegramRetryAfter(method=SendMessage(chat_id=1, text="x"), message="flood", retry_after=30)

    r2 = make(FakeBot())
    r2.bot.send_message_draft = flood
    await r2.push("a")  # swallowed
    assert r2._next_push > 0


async def test_empty_reply_gets_placeholder():
    bot = FakeBot()
    await make(bot).finish("")
    assert list(bot.sent.values()) == ["（空回复）"]


@pytest.mark.parametrize("use_draft", [True, False])
async def test_code_block_survives_split(use_draft):
    bot = FakeBot()
    r = make(bot, use_draft=use_draft)
    text = "```python\n" + "\n".join(f"print({i})" for i in range(600)) + "\n```"
    await r.finish(text)
    sends = list(bot.sent.values())
    assert len(sends) >= 2
    assert all(t.startswith('<pre><code class="language-python">') for t in sends)


STOP = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⏹ 停止", callback_data="stop:1")]])


async def test_stop_button_only_on_the_piece_being_written():
    bot = FakeBot()
    r = make(bot, use_draft=False, stop_markup=STOP)
    await r.push("x " * 1000)
    assert bot.calls[-1][1]["reply_markup"] is STOP
    await r.push("x " * 3000)  # spills into a second message
    sends = [kw for kind, kw in bot.calls if kind == "send"]
    edits = [kw for kind, kw in bot.calls if kind == "edit"]
    assert len(sends) == 2 and sends[1]["reply_markup"] is STOP
    assert edits[-1]["message_id"] == 101 and edits[-1]["reply_markup"] is None  # first piece loses it
    await r.finish("x " * 3000)
    assert bot.calls[-1][0] == "edit" and bot.calls[-1][1]["reply_markup"] is None


async def test_draft_offers_native_stop():
    bot = FakeBot()
    await make(bot, stop_markup=STOP).push("hi")
    await make(bot).push("hi")
    drafts = [kw for kind, kw in bot.calls if kind == "draft"]
    assert drafts[0]["can_stop"] is True and drafts[1]["can_stop"] is None
