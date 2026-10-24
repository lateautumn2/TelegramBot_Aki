"""End-to-end: private chat, whitelists, groups and admin commands."""

from datetime import datetime
from types import SimpleNamespace

import pytest

from aiogram.methods import (
    AnswerCallbackQuery,
    DeleteMyCommands,
    EditMessageText,
    LeaveChat,
    SendMessage,
    SendMessageDraft,
    SetMyCommands,
)
from aiogram.types import Chat, Message
from helpers import (
    ADMIN2,
    API_KEY,
    BOT_ID,
    BOT_NAME,
    GROUP,
    MEMBER,
    OTHER_GROUP,
    OWNER,
    STRANGER,
    make_config,
)

from bot.db.repo import Repo
from bot.handlers.admin_acl import AclCb
from bot.main import bootstrap_access
from bot.middlewares import auth
from bot.services.acl import AccessControl, Kind
from bot.services.crypto import SecretBox
from bot.services.llm import LLMError

# ---- bootstrap ----


async def test_bootstrap_created_provider(h):
    active = await h.repo.get_active_model()
    assert (active.model, active.base_url, active.api_key) == ("m1", "https://api.test/v1", API_KEY)


async def test_env_whitelist_is_imported_only_once(tmp_path):
    config = make_config(tmp_path, allowed_user_ids="5,6", allowed_chat_ids="-1007")
    repo = Repo(config.db_url, SecretBox(config.secret_key))
    await repo.init()
    acl = AccessControl(repo, config.admin_ids)
    await bootstrap_access(acl, repo, config)
    assert acl.has(Kind.USER, 5) and acl.has(Kind.USER, 6) and acl.has(Kind.CHAT, -1007)

    await acl.remove(Kind.USER, 5)
    acl2 = AccessControl(repo, config.admin_ids)  # simulated restart
    await bootstrap_access(acl2, repo, config)
    assert not acl2.has(Kind.USER, 5) and acl2.has(Kind.USER, 6)
    await repo.close()


# ---- private chat & whitelist ----


@pytest.mark.parametrize("now", [-10.0, 0.0, 5.0, 1000.0])
async def test_stranger_is_denied_but_can_get_id(h, monkeypatch, now):
    # Replace only this module's clock so asyncio and database timers keep running.
    monkeypatch.setattr(auth, "time", SimpleNamespace(monotonic=lambda: now))
    await h.send(STRANGER, "hello")
    await h.send(STRANGER, "hello again")  # notice is rate limited
    await h.send(STRANGER, "/id")
    assert len(h.texts()) == 2
    assert "没有使用权限" in h.texts()[0] and str(STRANGER) in h.texts()[0]
    assert f"user_id: <code>{STRANGER}</code>" in h.texts()[1]
    assert h.llm.calls == []


async def test_denial_notice_cooldown_is_per_user_and_expires(h, monkeypatch):
    now = 0.0
    monkeypatch.setattr(auth, "time", SimpleNamespace(monotonic=lambda: now))
    await h.send(STRANGER, "hello")
    assert len(h.texts()) == 1 and str(STRANGER) in h.last_text()

    now = 1.0
    await h.send(STRANGER, "hello again")
    assert len(h.texts()) == 1
    await h.send(MEMBER, "hello")
    assert len(h.texts()) == 2 and str(MEMBER) in h.last_text()

    # Suppressed messages must not extend the original cooldown.
    now = 600.0
    await h.send(STRANGER, "still waiting")
    assert len(h.texts()) == 2
    now = 601.0
    await h.send(STRANGER, "hello after cooldown")
    assert len(h.texts()) == 3
    assert "没有使用权限" in h.last_text() and str(STRANGER) in h.last_text()
    assert h.llm.calls == []


async def test_owner_chat_streams_and_keeps_context(h):
    await h.send(OWNER, "first question")
    assert h.texts() == ["Hi <b>there</b>"]
    assert h.session.of(SendMessageDraft)

    await h.send(OWNER, "second question")
    assert [(m["role"], m["content"]) for m in h.llm.calls[1]] == [
        ("system", "You are a helpful assistant."),
        ("user", "first question"),
        ("assistant", "Hi **there**"),
        ("user", "second question"),
    ]
    await h.send(OWNER, "/new")
    assert "已开启新对话" in h.last_text()
    await h.send(OWNER, "third")
    assert [m["role"] for m in h.llm.calls[2]] == ["system", "user"]


async def test_allow_and_deny_user_by_id(h):
    await h.send(OWNER, f"/allow_user {MEMBER}")
    assert "已将" in h.last_text() and str(MEMBER) in h.last_text()
    await h.send(MEMBER, "hi")
    assert len(h.llm.calls) == 1

    await h.send(OWNER, f"/allow_user {MEMBER}")
    assert "已在白名单中" in h.last_text()
    await h.send(OWNER, f"/deny_user {MEMBER}")
    assert "移出白名单" in h.last_text()
    h.clear()
    await h.send(MEMBER, "hi again")
    assert "没有使用权限" in h.last_text() and len(h.llm.calls) == 1


async def test_allow_user_usage_and_reply_target(h):
    await h.send(OWNER, "/allow_user abc")
    assert "用法" in h.last_text()

    their_msg = h.message(MEMBER, "can I use it?", GROUP)
    await h.send(OWNER, "/allow_user", GROUP, reply_to_message=their_msg)
    assert f"user{MEMBER}" in h.last_text()
    assert h.acl.entries(Kind.USER)[0].name == f"user{MEMBER}"


async def test_member_cannot_run_admin_commands(h):
    await h.acl.add(Kind.USER, MEMBER)
    await h.send(MEMBER, "/allow_user 5")
    assert "仅管理员" in h.last_text()
    await h.send(MEMBER, f"/add_admin {MEMBER}")
    assert "仅超级管理员" in h.last_text()
    assert not h.acl.has(Kind.USER, 5)


async def test_help_depends_on_role(h):
    await h.acl.add(Kind.USER, MEMBER)
    await h.send(MEMBER, "/help")
    assert "/new" in h.last_text() and "/allow_user" not in h.last_text()
    await h.send(OWNER, "/help")
    assert "/allow_user" in h.last_text() and "/add_admin" in h.last_text()


async def test_open_mode_lets_anyone_chat(h):
    await h.repo.set_setting("access_mode", "open")
    await h.send(STRANGER, "hello")
    assert len(h.llm.calls) == 1
    await h.send(STRANGER, "/allow_user 5")
    assert "仅管理员" in h.last_text()


# ---- groups ----


async def test_unauthorized_group_is_ignored_silently(h):
    await h.send(MEMBER, f"@{BOT_NAME} hello", GROUP)
    await h.send(MEMBER, "/ask hello", GROUP)
    await h.send(MEMBER, "/new", GROUP)
    assert h.texts() == [] and h.llm.calls == []


async def test_admin_can_use_bot_in_any_group(h):
    await h.send(OWNER, f"@{BOT_NAME} hello", GROUP)
    assert len(h.llm.calls) == 1


async def test_authorized_group_triggers(h):
    await h.send(OWNER, "/allow_group", GROUP)
    assert "已授权群组" in h.last_text() and "Test Group" in h.last_text()
    h.clear()

    # Plain chatter is ignored.
    await h.send(STRANGER, "just talking", GROUP)
    assert h.texts() == [] and h.llm.calls == []

    # Mention: anyone in the group, mention stripped, answer replies to the asker.
    asked = await h.send(STRANGER, f"@{BOT_NAME} what is 2+2?", GROUP)
    assert h.llm.last_prompt == "what is 2+2?"
    reply = h.session.of(SendMessage)[-1]
    assert reply.reply_parameters.message_id == asked.message_id
    assert not h.session.of(SendMessageDraft)  # drafts are private-chat only

    # Replying to the bot's latest answer continues without quoting it.
    bot_answer = h.session.sent[-1]
    await h.send(STRANGER, "and 3+3?", GROUP, reply_to_message=bot_answer)
    assert h.llm.last_prompt == "and 3+3?"
    assert [m["role"] for m in h.llm.calls[-1]] == ["system", "user", "assistant", "user"]

    # /ask works; empty /ask gets a hint.
    await h.send(MEMBER, "/ask hello", GROUP)
    assert h.llm.last_prompt == "hello"
    await h.send(MEMBER, "/ask", GROUP)
    assert "写上你的问题" in h.last_text()


async def test_group_context_is_per_user(h):
    await h.acl.add(Kind.CHAT, GROUP)
    await h.send(MEMBER, f"@{BOT_NAME} I am A", GROUP)
    await h.send(STRANGER, f"@{BOT_NAME} I am B", GROUP)
    assert [m["role"] for m in h.llm.calls[-1]] == ["system", "user"]
    await h.send(MEMBER, f"@{BOT_NAME} who am I?", GROUP)
    assert [m["content"] for m in h.llm.calls[-1][1:]] == ["I am A", "Hi **there**", "who am I?"]


async def test_reply_to_someone_else_is_quoted(h):
    await h.acl.add(Kind.CHAT, GROUP)
    other = h.message(MEMBER, "The meeting moved to Friday.", GROUP)
    await h.send(STRANGER, f"@{BOT_NAME} translate this", GROUP, reply_to_message=other)
    assert h.llm.last_prompt == "引用的消息：\nThe meeting moved to Friday.\n\ntranslate this"


async def test_reply_to_older_bot_message_is_quoted(h):
    await h.acl.add(Kind.CHAT, GROUP)
    old_answer = h.message(BOT_ID, "An old answer", GROUP)
    await h.send(MEMBER, "explain more", GROUP, reply_to_message=old_answer)
    assert h.llm.last_prompt.startswith("引用的消息：\nAn old answer")


async def test_forum_topic_root_is_not_a_reply(h):
    await h.acl.add(Kind.CHAT, GROUP)
    root = h.message(MEMBER, "Topic created", GROUP)
    kw = dict(message_thread_id=root.message_id, is_topic_message=True, reply_to_message=root)
    await h.send(MEMBER, "chatter in topic", GROUP, **kw)
    assert h.llm.calls == []
    await h.send(MEMBER, f"@{BOT_NAME} hi", GROUP, **kw)
    assert h.llm.last_prompt == "hi"
    assert h.session.of(SendMessage)[-1].message_thread_id == root.message_id


async def test_whitelisted_user_in_other_group_depends_on_setting(h):
    await h.acl.add(Kind.USER, MEMBER)
    await h.send(MEMBER, f"@{BOT_NAME} hi", OTHER_GROUP)
    assert h.llm.calls == []
    await h.repo.set_setting("allow_whitelisted_user_in_any_group", True)
    await h.send(MEMBER, f"@{BOT_NAME} hi", OTHER_GROUP)
    assert len(h.llm.calls) == 1


async def test_allow_group_by_id_and_deny(h):
    await h.send(OWNER, "/allow_group")
    assert "用法" in h.last_text()
    await h.send(OWNER, "/allow_group 12345")
    assert "应为负数" in h.last_text()
    await h.send(OWNER, f"/allow_group {OTHER_GROUP}")
    assert h.acl.has(Kind.CHAT, OTHER_GROUP)
    await h.send(OWNER, "/deny_group", OTHER_GROUP)
    assert "已取消授权" in h.last_text() and not h.acl.has(Kind.CHAT, OTHER_GROUP)


# ---- /whitelist panel ----


async def test_whitelist_panel(h):
    await h.send(OWNER, "/whitelist", GROUP)
    assert "只能在私聊" in h.last_text()

    for i in range(10):
        await h.acl.add(Kind.USER, 500 + i, f"u{i}")
    await h.acl.add(Kind.CHAT, GROUP, "Test Group")
    await h.send(OWNER, "/whitelist")
    panel = h.session.of(SendMessage)[-1]
    assert "用户 10 个，群组 1 个" in panel.text and "第 1/2 页" in panel.text
    buttons = [b for row in panel.reply_markup.inline_keyboard for b in row]
    assert len(buttons) == 9  # 8 entries + "next page"
    assert buttons[-1].callback_data == AclCb(action="page", page=1).pack()

    # Non-admins can't press the buttons, even if they get hold of the payload.
    await h.acl.add(Kind.USER, MEMBER)
    await h.click(MEMBER, buttons[0].callback_data)
    assert h.acl.has(Kind.USER, 500)
    assert "仅管理员" in h.session.of(AnswerCallbackQuery)[-1].text

    await h.click(OWNER, buttons[0].callback_data)
    assert not h.acl.has(Kind.USER, 500)
    assert h.session.of(AnswerCallbackQuery)[-1].text == "已移除"
    edited = h.session.of(EditMessageText)[-1]
    assert "用户 10 个" in edited.text  # 9 left + MEMBER

    await h.click(OWNER, AclCb(action="page", page=1).pack())
    assert "第 2/2 页" in h.session.of(EditMessageText)[-1].text


# ---- admins ----


async def test_owner_manages_admins(h):
    await h.send(OWNER, f"/add_admin {ADMIN2}")
    assert "设为管理员" in h.last_text()
    scoped = h.session.of(SetMyCommands)[-1]
    assert scoped.scope.chat_id == ADMIN2
    assert "allow_user" in [c.command for c in scoped.commands]

    await h.send(ADMIN2, f"/allow_user {MEMBER}")
    assert h.acl.has(Kind.USER, MEMBER)
    await h.send(ADMIN2, f"/add_admin {MEMBER}")
    assert "仅超级管理员" in h.last_text()

    await h.send(OWNER, "/remove_admin")
    assert str(ADMIN2) in h.last_text()
    await h.send(OWNER, f"/remove_admin {OWNER}")
    assert "无法在 Bot 内移除" in h.last_text()
    await h.send(OWNER, f"/remove_admin {ADMIN2}")
    assert h.session.of(DeleteMyCommands)[-1].scope.chat_id == ADMIN2
    h.clear()
    await h.send(ADMIN2, "hello")
    assert "没有使用权限" in h.last_text()


# ---- membership ----


async def test_bot_added_by_admin_gets_hint(h):
    await h.bot_added(OWNER, OTHER_GROUP)
    assert "/allow_group" in h.last_text()
    assert not h.session.of(LeaveChat)


async def test_bot_added_by_stranger_leaves_only_if_configured(h):
    await h.bot_added(STRANGER, OTHER_GROUP)
    assert h.session.requests == []
    await h.repo.set_setting("leave_unauthorized_groups", True)
    await h.bot_added(STRANGER, OTHER_GROUP)
    assert h.session.of(LeaveChat)[-1].chat_id == OTHER_GROUP


async def test_bot_added_to_authorized_group_is_quiet(h):
    await h.acl.add(Kind.CHAT, OTHER_GROUP)
    await h.bot_added(OWNER, OTHER_GROUP)
    assert h.session.requests == []


async def test_group_migration_moves_authorization(h):
    await h.acl.add(Kind.CHAT, -42, "Old")
    migrate = Message(
        message_id=5, date=datetime.now(), chat=Chat(id=-42, type="group", title="Old"), migrate_to_chat_id=-100042
    )
    await h._feed(message=migrate)
    assert not h.acl.has(Kind.CHAT, -42) and h.acl.has(Kind.CHAT, -100042)


# ---- errors ----


async def test_llm_error_shows_detail_to_admin_and_is_not_saved(h):
    h.llm.error = LLMError("API Key 无效或已过期。", "AuthenticationError: 401")
    await h.send(OWNER, "q")
    assert "API Key 无效" in h.last_text() and "401" in h.last_text()

    await h.acl.add(Kind.USER, MEMBER)
    await h.send(MEMBER, "q")
    assert "API Key 无效" in h.last_text() and "401" not in h.last_text()

    h.llm.error = None
    await h.send(OWNER, "q2")
    assert [m["role"] for m in h.llm.calls[-1]] == ["system", "user"]
