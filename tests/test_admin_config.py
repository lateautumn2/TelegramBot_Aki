"""End-to-end: /model, /provider and /settings."""

from aiogram.methods import AnswerCallbackQuery, DeleteMessage, EditMessageText, SendMessage
from helpers import API_KEY, GROUP, MEMBER, OWNER, Harness

from bot.handlers.admin_model import ModelCb
from bot.handlers.admin_provider import ProvCb
from bot.handlers.admin_settings import SPECS, SetCb
from bot.services.acl import Kind
from bot.services.llm import LLMError

# ---- helpers ----


def screens(h: Harness) -> list:
    """Every message the bot sent or edited, oldest first."""
    return [r for r in h.session.requests if isinstance(r, (SendMessage, EditMessageText))]


def screen(h: Harness):
    return screens(h)[-1]


def buttons(h: Harness) -> dict[str, str]:
    """Button text -> callback data of the most recent message that has buttons."""
    for r in reversed(screens(h)):
        if r.reply_markup is not None:
            return {b.text: b.callback_data for row in r.reply_markup.inline_keyboard for b in row}
    return {}


def last_answer(h: Harness) -> AnswerCallbackQuery:
    return h.session.of(AnswerCallbackQuery)[-1]


def spec(key: str) -> int:
    return next(i for i, s in enumerate(SPECS) if s.key == key)


async def default_provider(h: Harness):
    (provider, models), *_ = await h.repo.list_providers()
    return provider, models


async def add_provider_steps(h: Harness, name="New One", url="https://new.example.com", key="sk-new-secret-abcdef"):
    await h.send(OWNER, "/provider")
    await h.click(OWNER, buttons(h)["➕ 添加"])
    await h.send(OWNER, name)
    await h.send(OWNER, url)
    return await h.send(OWNER, key)


# ---- /model ----


async def test_model_menu_and_switch(h):
    provider, (m1,) = await default_provider(h)
    m2 = await h.repo.add_model(provider.id, "m2")

    await h.send(OWNER, "/model")
    assert "当前模型：<b>m1</b>" in screen(h).text
    assert {"— default —", "✅ m1", "m2"} <= set(buttons(h))

    await h.click(OWNER, buttons(h)["m2"])
    assert (await h.repo.get_active_model()).model_id == m2.id
    assert "当前模型：<b>m2</b>" in screen(h).text and "✅ m2" in buttons(h)
    assert last_answer(h).text == "已切换到 m2"

    await h.send(OWNER, "/model M1")
    assert "已切换到 <b>m1</b>" in h.last_text()
    assert (await h.repo.get_active_model()).model_id == m1.id
    await h.send(OWNER, "/model nope")
    assert "未找到" in h.last_text()


async def test_model_requires_admin_and_works_in_groups(h):
    await h.acl.add(Kind.USER, MEMBER)
    await h.send(MEMBER, "/model")
    assert "仅管理员" in h.last_text()
    await h.click(MEMBER, ModelCb(action="set", id=1).pack())
    assert last_answer(h).show_alert

    await h.send(OWNER, "/model", GROUP)
    assert "当前模型" in h.last_text()


async def test_model_menu_without_models(h):
    provider, _ = await default_provider(h)
    await h.repo.remove_model(provider.id, "m1")
    await h.send(OWNER, "/model")
    assert "尚未配置可用模型" in h.last_text()


# ---- /provider: add ----


async def test_add_provider_happy_path(h):
    await h.send(OWNER, "/provider")
    listing = screen(h).text
    assert "default" in listing and "sk-t****7890" in listing and API_KEY not in listing

    await h.click(OWNER, buttons(h)["➕ 添加"])
    assert "请输入名称" in h.last_text()
    await h.send(OWNER, "DEFAULT")
    assert "同名" in h.last_text()
    await h.send(OWNER, "New One")
    await h.send(OWNER, "not a url")
    assert "不是有效" in h.last_text()
    await h.send(OWNER, "https://new.example.com/v1/chat/completions")
    assert "https://new.example.com/v1" in h.last_text()
    key_msg = await h.send(OWNER, "sk-new-secret-abcdef")

    assert key_msg.message_id in [d.message_id for d in h.session.of(DeleteMessage)]
    providers = await h.repo.list_providers()
    new, models = providers[1]
    assert (new.name, new.base_url, h.repo.provider_key(new)) == ("New One", "https://new.example.com/v1",
                                                                   "sk-new-secret-abcdef")
    assert models == []

    # Picker: nothing enabled yet; tick one model, then finish.
    assert "⬜ gpt-x" in buttons(h)
    await h.click(OWNER, buttons(h)["⬜ gpt-x"])
    assert [m.name for m in await h.repo.list_models(new.id)] == ["gpt-x"]
    assert "✅ gpt-x" in buttons(h)
    await h.click(OWNER, buttons(h)["✔️ 完成"])
    assert "已启用模型（1）：gpt-x" in screen(h).text

    # The key never appears in anything the bot showed.
    assert all("sk-new-secret-abcdef" not in (r.text or "") for r in screens(h))


async def test_add_provider_connection_failure_then_retry(h):
    h.llm.models = LLMError("API Key 无效或已过期。", "AuthenticationError: 401", "auth")
    await add_provider_steps(h)
    assert "连接失败" in screen(h).text
    assert len(await h.repo.list_providers()) == 1  # nothing saved

    # Re-entering the URL re-tests with the key already given.
    await h.click(OWNER, buttons(h)["🔗 重新输入 URL"])
    await h.send(OWNER, "https://other.example.com")
    assert "连接失败" in screen(h).text

    h.llm.models = ["a", "b"]
    await h.click(OWNER, buttons(h)["🔑 重新输入 Key"])
    await h.send(OWNER, "sk-good-key-123456")
    new, _ = (await h.repo.list_providers())[1]
    assert new.base_url == "https://other.example.com/v1"
    assert h.repo.provider_key(new) == "sk-good-key-123456"
    assert {"⬜ a", "⬜ b"} <= set(buttons(h))


async def test_add_provider_save_anyway_then_manual_models(h):
    h.llm.models = LLMError("无法连接", "ConnectError", "connection")
    await add_provider_steps(h)
    await h.click(OWNER, buttons(h)["仍然保存"])
    assert "已保存" in h.last_text()
    await h.send(OWNER, "x1, x2\nx3")
    new, models = (await h.repo.list_providers())[1]
    assert [m.name for m in models] == ["x1", "x2", "x3"]
    assert h.llm.probes[-1] == ("https://new.example.com/v1", "sk-new-secret-abcdef", "x1")
    assert "x1 测试可用" in screen(h).text


async def test_add_provider_without_model_listing(h):
    h.llm.models = LLMError("模型不存在", "NotFoundError", "not_found")
    await add_provider_steps(h)
    new, _ = (await h.repo.list_providers())[1]
    assert "不提供模型列表" in screen(h).text and "没有可选的模型" in screen(h).text

    h.llm.probe_error = LLMError("模型不存在或 Base URL 路径错误。", "", "not_found")
    await h.click(OWNER, buttons(h)["✍️ 手动添加"])
    await h.send(OWNER, "foo，bar")
    assert [m.name for m in await h.repo.list_models(new.id)] == ["bar", "foo"]
    assert "foo 测试失败" in screen(h).text


async def test_cancel_abandons_a_flow(h):
    await h.send(OWNER, "/provider")
    await h.click(OWNER, buttons(h)["➕ 添加"])
    await h.send(OWNER, "/cancel")
    assert h.last_text() == "已取消。"
    await h.send(OWNER, "hello")  # back to normal chat
    assert len(h.llm.calls) == 1 and len(await h.repo.list_providers()) == 1
    await h.send(OWNER, "/cancel")
    assert "没有进行中" in h.last_text()


async def test_provider_is_private_and_admin_only(h):
    await h.send(OWNER, "/provider", GROUP)
    assert "只能在私聊" in h.last_text()
    await h.acl.add(Kind.USER, MEMBER)
    await h.send(MEMBER, "/provider")
    assert "仅管理员" in h.last_text()
    await h.click(MEMBER, ProvCb(action="del_ok", id=1).pack())
    assert last_answer(h).show_alert and len(await h.repo.list_providers()) == 1


# ---- /provider: edit, test, delete ----


async def test_picker_toggle_search_and_expiry(h):
    provider, _ = await default_provider(h)
    await h.click(OWNER, ProvCb(action="models", id=provider.id).pack())
    assert {"⬜ gpt-x", "⬜ gpt-y", "✅ m1", "⬜ m2"} <= set(buttons(h))

    await h.click(OWNER, buttons(h)["✅ m1"])  # the model in use can't be disabled
    assert "当前正在使用" in last_answer(h).text and last_answer(h).show_alert
    await h.click(OWNER, buttons(h)["⬜ m2"])
    assert [m.name for m in await h.repo.list_models(provider.id)] == ["m1", "m2"]

    await h.click(OWNER, buttons(h)["🔍 搜索"])
    await h.send(OWNER, "GPT")
    assert "2 个结果" in screen(h).text
    assert {"⬜ gpt-x", "⬜ gpt-y", "✖ 清除搜索"} <= set(buttons(h)) and "✅ m1" not in buttons(h)
    await h.click(OWNER, buttons(h)["✖ 清除搜索"])
    assert "✅ m1" in buttons(h)

    stale = buttons(h)["⬜ gpt-x"]
    await h.click(OWNER, buttons(h)["✔️ 完成"])
    await h.click(OWNER, stale)
    assert "过期" in last_answer(h).text
    assert [m.name for m in await h.repo.list_models(provider.id)] == ["m1", "m2"]


async def test_edit_name_url_key(h):
    provider, _ = await default_provider(h)
    other = await h.repo.add_provider("other", "https://o.test/v1", "sk-other-123456")

    await h.click(OWNER, ProvCb(action="rename", id=provider.id).pack())
    await h.send(OWNER, "OTHER")
    assert "同名" in h.last_text()
    await h.send(OWNER, "renamed")
    assert (await h.repo.get_provider(provider.id)).name == "renamed"

    await h.click(OWNER, ProvCb(action="url", id=provider.id).pack())
    await h.send(OWNER, "ftp://x")
    assert "不是有效" in h.last_text()
    await h.send(OWNER, "https://moved.test")
    assert (await h.repo.get_provider(provider.id)).base_url == "https://moved.test/v1"
    assert any("Base URL 已更新。✅ 连接正常" in t for t in h.texts())

    await h.click(OWNER, ProvCb(action="key", id=provider.id).pack())
    key_msg = await h.send(OWNER, "sk-rotated-987654")
    assert h.session.of(DeleteMessage)[-1].message_id == key_msg.message_id
    assert h.repo.provider_key(await h.repo.get_provider(provider.id)) == "sk-rotated-987654"
    assert h.repo.provider_key(await h.repo.get_provider(other.id)) == "sk-other-123456"


async def test_connection_test_button(h):
    provider, _ = await default_provider(h)
    cb = ProvCb(action="test", id=provider.id).pack()
    await h.click(OWNER, cb)
    assert last_answer(h).text == "✅ 连接正常，可获取 4 个模型。"

    h.llm.models = LLMError("模型不存在", "", "not_found")
    await h.click(OWNER, cb)
    assert last_answer(h).text == "✅ 连接正常（m1 可用）。"

    h.llm.models = LLMError("API Key 无效或已过期。", "", "auth")
    await h.click(OWNER, cb)
    assert last_answer(h).text == "❌ API Key 无效或已过期。"


async def test_delete_provider(h):
    provider, _ = await default_provider(h)
    await h.click(OWNER, ProvCb(action="del", id=provider.id).pack())
    assert "当前正在使用" in last_answer(h).text

    other = await h.repo.add_provider("other", "https://o.test/v1", "sk-other-123456")
    await h.repo.add_model(other.id, "o1")
    await h.click(OWNER, ProvCb(action="del", id=other.id).pack())
    assert "确定删除 <b>other</b> 及其 1 个模型" in screen(h).text
    await h.click(OWNER, buttons(h)["⚠️ 确认删除"])
    assert await h.repo.get_provider(other.id) is None
    assert last_answer(h).text == "已删除"
    assert "other" not in screen(h).text


# ---- /settings ----


async def test_settings_menu_toggles_and_choices(h):
    await h.send(OWNER, "/settings")
    assert "流式输出：开" in buttons(h)

    await h.click(OWNER, SetCb(action="open", i=spec("stream")).pack())
    assert (await h.repo.get_settings()).stream is False
    assert last_answer(h).text == "流式输出：已关闭" and "流式输出：关" in buttons(h)

    await h.click(OWNER, SetCb(action="open", i=spec("access_mode")).pack())
    assert (await h.repo.get_settings()).access_mode == "open"
    assert last_answer(h).show_alert and "所有人" in last_answer(h).text
    await h.click(OWNER, SetCb(action="open", i=spec("access_mode")).pack())
    assert (await h.repo.get_settings()).access_mode == "whitelist"


async def test_settings_typed_values(h):
    await h.click(OWNER, SetCb(action="open", i=spec("temperature")).pack())
    assert "恢复默认（0.7）" in buttons(h)
    await h.send(OWNER, "abc")
    assert "请输入数字" in h.last_text()
    await h.send(OWNER, "3")
    assert "超出范围" in h.last_text()
    await h.send(OWNER, "1.5")
    assert (await h.repo.get_settings()).temperature == 1.5
    assert any("已更新为：1.5" in t for t in h.texts())

    await h.click(OWNER, SetCb(action="open", i=spec("history_limit")).pack())
    await h.send(OWNER, "1.5")
    assert "请输入整数" in h.last_text()
    await h.send(OWNER, "/cancel")

    await h.click(OWNER, SetCb(action="open", i=spec("max_tokens")).pack())
    await h.send(OWNER, "0")
    assert (await h.repo.get_settings()).max_tokens is None
    assert "单次回复上限：不限制" in buttons(h)

    await h.click(OWNER, SetCb(action="open", i=spec("system_prompt")).pack())
    await h.send(OWNER, "-")
    assert (await h.repo.get_settings()).system_prompt == ""
    await h.send(OWNER, "hi")  # chat again, now without a system message
    assert h.llm.calls[-1] == [{"role": "user", "content": "hi"}]

    await h.click(OWNER, SetCb(action="reset", i=spec("temperature")).pack())
    assert (await h.repo.get_settings()).temperature == 0.7


async def test_group_trigger_switches(h):
    await h.acl.add(Kind.CHAT, GROUP)

    def toggle(value: str) -> str:
        return SetCb(action="multi", i=spec("group_triggers"), value=value).pack()

    await h.click(OWNER, SetCb(action="open", i=spec("group_triggers")).pack())
    assert buttons(h) == {"✅ @Bot": toggle("mention"), "✅ 回复 Bot": toggle("reply"), "✅ /ask": toggle("command"),
                          "« 返回": SetCb(action="menu").pack()}
    await h.click(OWNER, toggle("mention"))
    assert (await h.repo.get_settings()).group_triggers == ["reply", "command"]

    await h.send(MEMBER, "@testbot hi", GROUP)
    assert h.llm.calls == []
    await h.send(MEMBER, "/ask hi", GROUP)
    assert len(h.llm.calls) == 1

    await h.click(OWNER, toggle("command"))
    await h.send(MEMBER, "/ask hi", GROUP)
    assert len(h.llm.calls) == 1
    await h.send(MEMBER, "follow up", GROUP, reply_to_message=h.session.sent[-1])
    assert len(h.llm.calls) == 2
    await h.click(OWNER, toggle("mention"))
    assert (await h.repo.get_settings()).group_triggers == ["mention", "reply"]


async def test_settings_is_private_and_admin_only(h):
    await h.send(OWNER, "/settings", GROUP)
    assert "只能在私聊" in h.last_text()
    await h.acl.add(Kind.USER, MEMBER)
    await h.click(MEMBER, SetCb(action="open", i=spec("stream")).pack())
    assert last_answer(h).show_alert and (await h.repo.get_settings()).stream is True


async def test_picker_buttons_from_another_provider_are_rejected(h):
    provider, _ = await default_provider(h)
    other = await h.repo.add_provider("other", "https://o.test/v1", "sk-other-123456")
    await h.click(OWNER, ProvCb(action="models", id=provider.id).pack())
    stale = buttons(h)["⬜ gpt-x"]
    await h.click(OWNER, ProvCb(action="models", id=other.id).pack())  # replaces the picker state
    await h.click(OWNER, stale)
    assert "过期" in last_answer(h).text
    assert [m.name for m in await h.repo.list_models(provider.id)] == ["m1"]
    assert await h.repo.list_models(other.id) == []


async def test_manual_add_tolerates_already_enabled_names(h):
    provider, _ = await default_provider(h)
    await h.click(OWNER, ProvCb(action="models", id=provider.id).pack())
    await h.click(OWNER, buttons(h)["✍️ 手动添加"])
    await h.send(OWNER, "m1, extra")
    assert [m.name for m in await h.repo.list_models(provider.id)] == ["extra", "m1"]
