"""/provider: manage AI endpoints (name + Base URL + API key) and their models.

Multi-step inputs use FSM states; the model picker keeps the fetched model
list in FSM data so button payloads only carry an index.
"""

import contextlib
import html
import math
import re

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command, StateFilter
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.db.repo import Repo
from bot.keyboards import nav_row
from bot.services.crypto import mask_key
from bot.services.llm import LLMError, LLMService, normalize_base_url

PICKER_PAGE = 10
NAME_MAX = 32
# These mean the endpoint itself is unusable; anything else means it merely
# doesn't implement GET /models.
_FATAL_KINDS = {"auth", "connection", "timeout"}

CANCEL_HINT = "\n\n发送 /cancel 取消。"


class ProviderForm(StatesGroup):
    name = State()
    base_url = State()
    api_key = State()
    edit_name = State()
    edit_url = State()
    edit_key = State()
    manual_models = State()
    search = State()


class ProvCb(CallbackData, prefix="prv"):
    # list | add | view | test | rename | url | key | del | del_ok | retry_url | retry_key | save_anyway
    # models | pt (toggle) | pp (page) | ps (search) | pc (clear search) | pm (manual) | pd (done)
    action: str
    id: int = 0
    i: int = 0
    page: int = 0


def _btn(text: str, **cb) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=ProvCb(**cb).pack())


async def _show(target: Message | CallbackQuery, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
    """Edit the message a button belongs to, or send a new message."""
    if isinstance(target, CallbackQuery):
        if isinstance(target.message, Message):
            try:
                await target.message.edit_text(text, reply_markup=markup)
            except TelegramBadRequest as exc:
                if "message is not modified" not in exc.message:
                    raise
        return
    await target.answer(text, reply_markup=markup)


def _masked(repo: Repo, provider) -> str:
    try:
        return mask_key(repo.provider_key(provider))
    except ValueError:
        return "（无法解密，SECRET_KEY 是否变更过？）"


def _short_error(err: LLMError) -> str:
    return err.user_message + (f"\n<code>{html.escape(err.detail[:300])}</code>" if err.detail else "")


# ---- list & detail ----


async def render_list(repo: Repo) -> tuple[str, InlineKeyboardMarkup]:
    providers = await repo.list_providers()
    active = await repo.get_active_model()
    lines = [f"🔌 <b>接入配置</b>（{len(providers)} 个）"]
    builder = InlineKeyboardBuilder()
    for provider, models in providers:
        current = " ✅当前" if active is not None and active.provider_id == provider.id else ""
        lines.append(
            f"\n<b>{html.escape(provider.name)}</b>{current}\n"
            f"{html.escape(provider.base_url)} · {_masked(repo, provider)} · {len(models)} 个模型"
        )
        builder.row(_btn(f"{provider.name}{current}", action="view", id=provider.id))
    if not providers:
        lines.append("\n还没有接入配置，点击下方按钮添加。")
    builder.row(_btn("➕ 添加", action="add"))
    return "\n".join(lines), builder.as_markup()


async def render_detail(repo: Repo, provider_id: int) -> tuple[str, InlineKeyboardMarkup] | None:
    provider = await repo.get_provider(provider_id)
    if provider is None:
        return None
    models = await repo.list_models(provider_id)
    active = await repo.get_active_model()
    shown = "、".join(html.escape(m.name) for m in models[:30]) or "（无，点击「管理模型」启用）"
    if len(models) > 30:
        shown += f" …等 {len(models)} 个"
    current = ""
    if active is not None and active.provider_id == provider_id:
        current = f"\n当前使用：<b>{html.escape(active.model)}</b>"
    text = (
        f"🔌 <b>{html.escape(provider.name)}</b>{current}\n\n"
        f"Base URL：{html.escape(provider.base_url)}\n"
        f"API Key：{_masked(repo, provider)}\n"
        f"已启用模型（{len(models)}）：{shown}"
    )
    pid = provider_id
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [_btn("🧩 管理模型", action="models", id=pid), _btn("🩺 测试连接", action="test", id=pid)],
            [_btn("✏️ 改名称", action="rename", id=pid), _btn("🔗 改 URL", action="url", id=pid),
             _btn("🔑 改 Key", action="key", id=pid)],
            [_btn("🗑 删除", action="del", id=pid), _btn("« 返回列表", action="list")],
        ]
    )
    return text, markup


async def _show_detail(target: Message | CallbackQuery, repo: Repo, provider_id: int) -> None:
    rendered = await render_detail(repo, provider_id)
    if rendered is None:
        text, markup = await render_list(repo)
        await _show(target, "该接入配置已不存在。\n\n" + text, markup)
        return
    await _show(target, *rendered)


async def cmd_provider(message: Message, state: FSMContext, repo: Repo) -> None:
    await state.clear()
    await _show(message, *await render_list(repo))


# ---- model picker ----


async def _open_picker(
    target: Message | CallbackQuery,
    state: FSMContext,
    repo: Repo,
    llm: LLMService,
    provider_id: int,
    fetched: list[str] | None = None,
    note: str = "",
) -> None:
    provider = await repo.get_provider(provider_id)
    if provider is None:
        await _show(target, *await render_list(repo))
        return
    if fetched is None:
        try:
            fetched = await llm.list_models(provider.base_url, repo.provider_key(provider))
        except LLMError as err:
            fetched, note = [], f"⚠️ 获取模型列表失败：{err.user_message}"
    enabled = [m.name for m in await repo.list_models(provider_id)]
    picker = {"pid": provider_id, "names": sorted(set(fetched) | set(enabled)), "filter": "", "note": note}
    await state.update_data(picker=picker)
    await _show(target, *await _render_picker(repo, picker, 0))


async def _render_picker(repo: Repo, picker: dict, page: int) -> tuple[str, InlineKeyboardMarkup]:
    pid, names, keyword = picker["pid"], picker["names"], picker["filter"]
    provider = await repo.get_provider(pid)
    enabled = {m.name for m in await repo.list_models(pid)}
    visible = [i for i, n in enumerate(names) if keyword.lower() in n.lower()]
    pages = max(1, math.ceil(len(visible) / PICKER_PAGE))
    page = min(max(page, 0), pages - 1)

    lines = [f"🧩 <b>{html.escape(provider.name if provider else '?')}</b> 的模型：已启用 {len(enabled)} 个"]
    if picker.get("note"):
        lines.append(picker["note"])
    if not names:
        lines.append("没有可选的模型，请点击「手动添加」输入模型名。")
    else:
        lines.append(f"共 {len(names)} 个，点击切换启用状态。")
    if keyword:
        lines.append(f"🔍 搜索「{html.escape(keyword)}」：{len(visible)} 个结果")
    if pages > 1:
        lines.append(f"第 {page + 1}/{pages} 页")

    builder = InlineKeyboardBuilder()
    for i in visible[page * PICKER_PAGE : (page + 1) * PICKER_PAGE]:
        mark = "✅" if names[i] in enabled else "⬜"
        builder.row(_btn(f"{mark} {names[i][:48]}", action="pt", id=pid, i=i, page=page))
    if pages > 1:
        builder.row(*nav_row(page, pages, lambda p: ProvCb(action="pp", id=pid, page=p)))
    search = _btn("✖ 清除搜索", action="pc", id=pid) if keyword else _btn("🔍 搜索", action="ps", id=pid)
    builder.row(search, _btn("✍️ 手动添加", action="pm", id=pid), _btn("✔️ 完成", action="pd", id=pid))
    return "\n".join(lines), builder.as_markup()


# ---- button dispatcher ----


async def on_provider_button(
    query: CallbackQuery, callback_data: ProvCb, state: FSMContext, repo: Repo, llm: LLMService
) -> None:
    action, pid = callback_data.action, callback_data.id
    message = query.message if isinstance(query.message, Message) else None
    notice: str | None = None
    alert = False

    if action == "list":
        await state.clear()
        await _show(query, *await render_list(repo))
    elif action == "view":
        await _show_detail(query, repo, pid)
    elif action == "add":
        await state.clear()
        await state.set_state(ProviderForm.name)
        if message:
            await message.answer("➕ 添加接入配置（1/3）\n请输入名称，例如 <code>DeepSeek</code>。" + CANCEL_HINT)
    elif action in ("rename", "url", "key"):
        provider = await repo.get_provider(pid)
        if provider is None:
            await _show(query, *await render_list(repo))
        else:
            target_state, prompt = {
                "rename": (ProviderForm.edit_name, "请输入新的名称。"),
                "url": (ProviderForm.edit_url, "请输入新的 Base URL。"),
                "key": (ProviderForm.edit_key, "请输入新的 API Key（发送后我会立即删除这条消息）。"),
            }[action]
            await state.set_state(target_state)
            await state.update_data(edit_id=pid)
            if message:
                await message.answer(f"✏️ {html.escape(provider.name)}：{prompt}{CANCEL_HINT}")
    elif action == "test":
        notice, alert = await _test_provider(repo, llm, pid), True
    elif action == "del":
        provider = await repo.get_provider(pid)
        active = await repo.get_active_model()
        if provider is None:
            await _show(query, *await render_list(repo))
        elif active is not None and active.provider_id == pid:
            notice, alert = "当前正在使用该接入配置的模型，请先用 /model 切换到其他接入配置的模型。", True
        else:
            count = len(await repo.list_models(pid))
            markup = InlineKeyboardMarkup(
                inline_keyboard=[[_btn("⚠️ 确认删除", action="del_ok", id=pid), _btn("取消", action="view", id=pid)]]
            )
            await _show(query, f"确定删除 <b>{html.escape(provider.name)}</b> 及其 {count} 个模型吗？", markup)
    elif action == "del_ok":
        active = await repo.get_active_model()
        if active is not None and active.provider_id == pid:
            notice, alert = "当前正在使用该接入配置的模型，无法删除。", True
        else:
            await repo.delete_provider(pid)
            notice = "已删除"
            await _show(query, *await render_list(repo))
    elif action in ("retry_url", "retry_key"):
        target_state = ProviderForm.base_url if action == "retry_url" else ProviderForm.api_key
        await state.set_state(target_state)
        if message:
            prompt = "请重新输入 Base URL。" if action == "retry_url" else "请重新输入 API Key。"
            await message.answer(prompt + CANCEL_HINT)
    elif action == "save_anyway":
        form = (await state.get_data()).get("form")
        if not form or "api_key" not in form:
            notice, alert = "表单已过期，请重新添加。", True
        else:
            provider = await repo.add_provider(form["name"], form["base_url"], form["api_key"])
            await state.clear()
            await state.set_state(ProviderForm.manual_models)
            await state.update_data(picker={"pid": provider.id, "names": [], "filter": "", "note": ""})
            if message:
                await message.answer(
                    f"✅ 已保存 <b>{html.escape(provider.name)}</b>。\n请输入要启用的模型名，多个用逗号或换行分隔。"
                    + CANCEL_HINT
                )
    elif action == "models":
        await _open_picker(query, state, repo, llm, pid)
    elif action in ("pt", "pp", "pc", "ps", "pm", "pd"):
        await _on_picker_button(query, callback_data, state, repo)
        return
    await query.answer(notice, show_alert=alert)


async def _on_picker_button(query: CallbackQuery, cb: ProvCb, state: FSMContext, repo: Repo) -> None:
    picker = (await state.get_data()).get("picker")
    if picker is None or picker["pid"] != cb.id or (cb.action == "pt" and not 0 <= cb.i < len(picker["names"])):
        await query.answer("该列表已过期，请重新打开 /provider", show_alert=True)
        return
    message = query.message if isinstance(query.message, Message) else None
    page = cb.page

    if cb.action == "pt":
        name = picker["names"][cb.i]
        enabled = {m.name for m in await repo.list_models(cb.id)}
        if name in enabled:
            active = await repo.get_active_model()
            if active is not None and active.provider_id == cb.id and active.model == name:
                await query.answer("这是当前正在使用的模型，请先用 /model 切换到其他模型。", show_alert=True)
                return
            await repo.remove_model(cb.id, name)
        else:
            await repo.add_model(cb.id, name)
    elif cb.action == "pc":
        picker["filter"] = ""
        page = 0
        await state.update_data(picker=picker)
    elif cb.action == "ps":
        await state.set_state(ProviderForm.search)
        if message:
            await message.answer("🔍 请输入模型名关键词，例如 <code>gpt</code>。" + CANCEL_HINT)
        await query.answer()
        return
    elif cb.action == "pm":
        await state.set_state(ProviderForm.manual_models)
        if message:
            await message.answer("✍️ 请输入模型名，多个用逗号或换行分隔。" + CANCEL_HINT)
        await query.answer()
        return
    elif cb.action == "pd":
        await state.clear()
        await _show_detail(query, repo, cb.id)
        await query.answer()
        return

    await _show(query, *await _render_picker(repo, picker, page))
    await query.answer()


async def _test_provider(repo: Repo, llm: LLMService, provider_id: int) -> str:
    provider = await repo.get_provider(provider_id)
    if provider is None:
        return "该接入配置已不存在。"
    key = repo.provider_key(provider)
    try:
        names = await llm.list_models(provider.base_url, key)
        return f"✅ 连接正常，可获取 {len(names)} 个模型。"
    except LLMError as err:
        if err.kind in _FATAL_KINDS:
            return f"❌ {err.user_message}"
    # No model listing: try a real request with one of the enabled models.
    models = await repo.list_models(provider_id)
    if not models:
        return "⚠️ 该服务不提供模型列表，且尚未启用模型，无法测试。"
    try:
        await llm.probe(provider.base_url, key, models[0].name)
        return f"✅ 连接正常（{models[0].name} 可用）。"
    except LLMError as err:
        return f"❌ {models[0].name}：{err.user_message}"


# ---- text input steps ----


def _valid_name(text: str) -> str | None:
    name = text.strip()
    if not name or len(name) > NAME_MAX or "\n" in name:
        return None
    return name


async def on_name(message: Message, state: FSMContext, repo: Repo) -> None:
    name = _valid_name(message.text)
    if name is None:
        await message.reply(f"名称需为 1–{NAME_MAX} 个字符的单行文字，请重新输入。")
        return
    if await repo.provider_name_taken(name):
        await message.reply("已有同名的接入配置，请换一个名称。")
        return
    await state.update_data(form={"name": name})
    await state.set_state(ProviderForm.base_url)
    await message.answer(
        "➕ 添加接入配置（2/3）\n请输入 Base URL，例如 <code>https://api.deepseek.com</code>\n"
        "写到域名、<code>/v1</code> 或完整的 <code>/v1/chat/completions</code> 都可以。" + CANCEL_HINT
    )


async def on_base_url(message: Message, state: FSMContext, repo: Repo, llm: LLMService) -> None:
    try:
        url = normalize_base_url(message.text)
    except ValueError:
        await message.reply("这不是有效的 http(s) 地址，请重新输入。")
        return
    form = (await state.get_data()).get("form", {})
    form["base_url"] = url
    await state.update_data(form=form)
    await state.set_state(ProviderForm.api_key)
    if "api_key" in form:  # came back from "re-enter URL"
        await _finish_add(message, state, form, repo, llm)
        return
    await message.answer(
        f"➕ 添加接入配置（3/3）\n地址：<code>{html.escape(url)}</code>\n"
        "请输入 API Key（发送后我会立即删除这条消息）。" + CANCEL_HINT
    )


async def on_api_key(message: Message, state: FSMContext, repo: Repo, llm: LLMService) -> None:
    key = message.text.strip()
    with contextlib.suppress(TelegramAPIError):
        await message.delete()
    form = (await state.get_data()).get("form", {})
    form["api_key"] = key
    await state.update_data(form=form)
    await _finish_add(message, state, form, repo, llm)


async def _finish_add(message: Message, state: FSMContext, form: dict, repo: Repo, llm: LLMService) -> None:
    status = await message.answer("⏳ 正在测试连接…")
    try:
        names = await llm.list_models(form["base_url"], form["api_key"])
        note = ""
    except LLMError as err:
        if err.kind in _FATAL_KINDS:
            markup = InlineKeyboardMarkup(
                inline_keyboard=[
                    [_btn("🔗 重新输入 URL", action="retry_url"), _btn("🔑 重新输入 Key", action="retry_key")],
                    [_btn("仍然保存", action="save_anyway")],
                ]
            )
            await status.edit_text(f"❌ 连接失败：{_short_error(err)}{CANCEL_HINT}", reply_markup=markup)
            return
        names, note = [], "ℹ️ 该服务不提供模型列表，请手动添加模型名。"

    if await repo.provider_name_taken(form["name"]):
        await status.edit_text("已有同名的接入配置，请重新添加。")
        await state.clear()
        return
    provider = await repo.add_provider(form["name"], form["base_url"], form["api_key"])
    await state.clear()
    found = f"发现 {len(names)} 个模型，请选择要启用的：" if names else ""
    await status.edit_text(f"✅ 已保存 <b>{html.escape(provider.name)}</b>。{found}")
    await _open_picker(message, state, repo, llm, provider.id, fetched=names, note=note)


async def on_edit_name(message: Message, state: FSMContext, repo: Repo) -> None:
    pid = (await state.get_data()).get("edit_id")
    name = _valid_name(message.text)
    if name is None:
        await message.reply(f"名称需为 1–{NAME_MAX} 个字符的单行文字，请重新输入。")
        return
    if await repo.provider_name_taken(name, exclude_id=pid):
        await message.reply("已有同名的接入配置，请换一个名称。")
        return
    await repo.update_provider(pid, name=name)
    await state.clear()
    await message.answer("✅ 名称已更新。")
    await _show_detail(message, repo, pid)


async def on_edit_url(message: Message, state: FSMContext, repo: Repo, llm: LLMService) -> None:
    pid = (await state.get_data()).get("edit_id")
    try:
        url = normalize_base_url(message.text)
    except ValueError:
        await message.reply("这不是有效的 http(s) 地址，请重新输入。")
        return
    await repo.update_provider(pid, base_url=url)
    await state.clear()
    await message.answer(f"✅ Base URL 已更新。{await _test_provider(repo, llm, pid)}")
    await _show_detail(message, repo, pid)


async def on_edit_key(message: Message, state: FSMContext, repo: Repo, llm: LLMService) -> None:
    pid = (await state.get_data()).get("edit_id")
    with contextlib.suppress(TelegramAPIError):
        await message.delete()
    await repo.update_provider(pid, api_key=message.text.strip())
    await state.clear()
    await message.answer(f"✅ API Key 已更新。{await _test_provider(repo, llm, pid)}")
    await _show_detail(message, repo, pid)


async def on_manual_models(message: Message, state: FSMContext, repo: Repo, llm: LLMService) -> None:
    picker = (await state.get_data()).get("picker")
    if picker is None:
        await state.clear()
        await message.reply("操作已过期，请重新打开 /provider。")
        return
    names = [n.strip() for n in re.split(r"[,，\n]", message.text) if n.strip()]
    if not names:
        await message.reply("请输入至少一个模型名。")
        return
    pid = picker["pid"]
    for name in names:
        await repo.add_model(pid, name)
    picker["names"] = sorted(set(picker["names"]) | set(names))

    provider = await repo.get_provider(pid)
    try:
        await llm.probe(provider.base_url, repo.provider_key(provider), names[0])
        picker["note"] = f"✅ 已添加 {len(names)} 个模型，{html.escape(names[0])} 测试可用。"
    except LLMError as err:
        picker["note"] = f"⚠️ 已添加 {len(names)} 个模型，但 {html.escape(names[0])} 测试失败：{err.user_message}"
    await state.set_state(None)
    await state.update_data(picker=picker)
    await _show(message, *await _render_picker(repo, picker, 0))


async def on_search(message: Message, state: FSMContext, repo: Repo) -> None:
    picker = (await state.get_data()).get("picker")
    if picker is None:
        await state.clear()
        await message.reply("操作已过期，请重新打开 /provider。")
        return
    picker["filter"] = message.text.strip()
    await state.set_state(None)
    await state.update_data(picker=picker)
    await _show(message, *await _render_picker(repo, picker, 0))


def build() -> Router:
    router = Router(name="admin_provider")
    flags = {"admin": True, "private_only": True}
    text_input = (F.text, ~F.text.startswith("/"))
    router.message.register(cmd_provider, Command("provider"), flags=flags)
    router.callback_query.register(on_provider_button, ProvCb.filter(), flags=flags)
    steps = [
        (ProviderForm.name, on_name),
        (ProviderForm.base_url, on_base_url),
        (ProviderForm.api_key, on_api_key),
        (ProviderForm.edit_name, on_edit_name),
        (ProviderForm.edit_url, on_edit_url),
        (ProviderForm.edit_key, on_edit_key),
        (ProviderForm.manual_models, on_manual_models),
        (ProviderForm.search, on_search),
    ]
    for step, handler in steps:
        router.message.register(handler, StateFilter(step), *text_input, flags=flags)
    return router
