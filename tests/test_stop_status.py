"""End-to-end: stopping a reply mid-generation, and /status."""

import asyncio
from datetime import datetime

from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage, SendMessageDraft
from aiogram.types import Chat, MessageGenerationStopped
from helpers import GROUP, MEMBER, OWNER, STRANGER, Harness

from bot.handlers import chat as chat_handlers
from bot.services.acl import Kind
from bot.services.llm import LLMError


async def wait_for(condition, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not condition():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


async def history(h: Harness, key: str) -> list[tuple[str, str]]:
    session = await h.repo.get_or_create_session(key, 0)
    return await h.repo.load_history(session.id, 50)


def stop_button(request) -> str:
    (button,) = request.reply_markup.inline_keyboard[0]
    assert button.text == "⏹ 停止"
    return button.callback_data


# ---- stop ----


async def test_group_stop_button(h):
    await h.acl.add(Kind.CHAT, GROUP)
    h.llm.reply = "First second third"
    h.llm.gate = asyncio.Event()
    task = asyncio.create_task(h.send(MEMBER, "@testbot tell me", GROUP))
    await wait_for(lambda: h.session.of(SendMessage))

    streaming = h.session.of(SendMessage)[0]
    assert streaming.text.strip() == "First"
    cb = stop_button(streaming)

    await h.click(STRANGER, cb, GROUP)  # only the asker or an admin may stop it
    assert "只有提问的人" in h.session.of(AnswerCallbackQuery)[-1].text
    assert not task.done()

    await h.click(MEMBER, cb, GROUP)
    assert h.session.of(AnswerCallbackQuery)[-1].text == "已停止"
    await asyncio.wait_for(task, 2)

    final = h.session.of(EditMessageText)[-1]
    assert final.text == "First\n\n⏹ 已停止" and final.reply_markup is None
    assert await history(h, f"g:{GROUP}:{MEMBER}") == [("user", "tell me"), ("assistant", "First")]
    assert await h.repo.request_counts(datetime(2000, 1, 1)) == {"stopped": 1}

    await h.click(MEMBER, cb, GROUP)
    assert "已经结束" in h.session.of(AnswerCallbackQuery)[-1].text


async def test_admin_can_stop_someone_elses_reply(h):
    await h.acl.add(Kind.CHAT, GROUP)
    h.llm.gate = asyncio.Event()
    task = asyncio.create_task(h.send(MEMBER, "@testbot hi", GROUP))
    await wait_for(lambda: h.session.of(SendMessage))
    await h.click(OWNER, stop_button(h.session.of(SendMessage)[0]), GROUP)
    await asyncio.wait_for(task, 2)
    assert h.session.of(EditMessageText)[-1].text.endswith("⏹ 已停止")


async def test_private_draft_native_stop(h):
    h.llm.gate = asyncio.Event()
    task = asyncio.create_task(h.send(OWNER, "hello"))
    await wait_for(lambda: h.session.of(SendMessageDraft))
    draft = h.session.of(SendMessageDraft)[0]
    assert draft.can_stop is True

    stopped = MessageGenerationStopped(chat=Chat(id=OWNER, type="private"), draft_id=draft.draft_id)
    await h._feed(stopped_message_generation=stopped)
    await asyncio.wait_for(task, 2)
    assert h.texts() == ["Hi\n\n⏹ 已停止"]
    assert chat_handlers._running == {} and chat_handlers._generations == {}

    # The conversation carries on from the partial answer.
    h.llm.gate = None
    await h.send(OWNER, "go on")
    assert [m["content"] for m in h.llm.calls[-1][1:]] == ["hello", "Hi", "go on"]


async def test_finished_reply_has_no_stop_button(h):
    await h.acl.add(Kind.CHAT, GROUP)
    await h.send(MEMBER, "@testbot hi", GROUP)
    sent = h.session.of(SendMessage)[-1]
    edits = [e for e in h.session.of(EditMessageText) if e.message_id == h.session.sent[-1].message_id]
    final = edits[-1] if edits else sent
    assert final.reply_markup is None and final.text == "Hi <b>there</b>"
    assert await h.repo.request_counts(datetime(2000, 1, 1)) == {"ok": 1}


# ---- /status ----


async def test_status(h, monkeypatch):
    await h.acl.add(Kind.USER, MEMBER)
    await h.acl.add(Kind.CHAT, GROUP)
    await h.send(OWNER, "hi")
    h.llm.error = LLMError("API Key 无效或已过期。", "401", "auth")
    await h.send(OWNER, "hi again")

    monkeypatch.setenv("APP_VERSION", "9.9.9")
    await h.send(OWNER, "/status")
    text = h.last_text()
    assert "版本：9.9.9" in text
    assert "当前模型：m1（default）" in text
    assert "今日请求：2 次（成功 1，停止 0，失败 1）" in text
    assert "白名单：用户 1 个，群组 1 个" in text
    assert "管理员：1 个（其中超级管理员 1 个）" in text
    assert "数据库：" in text and "数据库：—" not in text
    assert "最近一次失败：" in text and "m1 · API Key 无效或已过期。" in text


async def test_status_is_admin_and_private_only(h):
    await h.send(OWNER, "/status", GROUP)
    assert "只能在私聊" in h.last_text()
    await h.acl.add(Kind.USER, MEMBER)
    await h.send(MEMBER, "/status")
    assert "仅管理员" in h.last_text()
