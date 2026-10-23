import html
import math

from aiogram import Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.filters.callback_data import CallbackData
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.db.repo import Repo
from bot.keyboards import nav_row

PAGE_SIZE = 20


class ModelCb(CallbackData, prefix="mdl"):
    action: str  # "set" | "page"
    id: int = 0
    page: int = 0


async def render_models(repo: Repo, page: int = 0) -> tuple[str, InlineKeyboardMarkup | None]:
    active = await repo.get_active_model()
    providers = await repo.list_providers()
    if active is None:
        text = "⚠️ 尚未配置可用模型。请先用 /provider 添加接入配置并启用模型。"
    else:
        text = f"当前模型：<b>{html.escape(active.model)}</b>（{html.escape(active.provider_name)}）"

    # Flatten into rows: a provider header followed by its models, then paginate.
    rows: list[tuple[str, int | None]] = []
    for provider, models in providers:
        if models:
            rows.append((f"— {provider.name} —", None))
            rows += [(m.name, m.id) for m in models]
    if not rows:
        return text, None
    pages = max(1, math.ceil(len(rows) / PAGE_SIZE))
    page = min(max(page, 0), pages - 1)

    builder = InlineKeyboardBuilder()
    for caption, model_id in rows[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]:
        if model_id is None:
            builder.row(InlineKeyboardButton(text=caption, callback_data=ModelCb(action="page", page=page).pack()))
        else:
            mark = "✅ " if active is not None and model_id == active.model_id else ""
            builder.row(
                InlineKeyboardButton(text=f"{mark}{caption}", callback_data=ModelCb(action="set", id=model_id).pack())
            )
    if pages > 1:
        builder.row(*nav_row(page, pages, lambda p: ModelCb(action="page", page=p)))
        text += f"\n\n第 {page + 1}/{pages} 页"
    return text + "\n\n点击切换（对所有人生效）：", builder.as_markup()


async def cmd_model(message: Message, command: CommandObject, repo: Repo) -> None:
    wanted = (command.args or "").strip()
    if wanted:
        # Quick switch: /model <name>
        matches = [
            (p, m) for p, models in await repo.list_providers() for m in models if m.name.lower() == wanted.lower()
        ]
        if not matches:
            await message.reply(f"未找到已启用的模型 <code>{html.escape(wanted)}</code>。发送 /model 查看列表。")
            return
        provider, model = matches[0]
        await repo.set_setting("current_model_id", model.id)
        await message.reply(f"✅ 已切换到 <b>{html.escape(model.name)}</b>（{html.escape(provider.name)}）")
        return
    text, markup = await render_models(repo)
    await message.answer(text, reply_markup=markup)


async def on_model_button(query: CallbackQuery, callback_data: ModelCb, repo: Repo) -> None:
    notice = None
    page = callback_data.page
    if callback_data.action == "set":
        model = await repo.get_model(callback_data.id)
        if model is None:
            await query.answer("该模型已被移除，请重新打开 /model", show_alert=True)
            return
        await repo.set_setting("current_model_id", model.id)
        notice = f"已切换到 {model.name}"
        page = await _page_of(repo, model.id)
    if isinstance(query.message, Message):
        text, markup = await render_models(repo, page)
        try:
            await query.message.edit_text(text, reply_markup=markup)
        except TelegramBadRequest as exc:
            if "message is not modified" not in exc.message:
                raise
    await query.answer(notice)


async def _page_of(repo: Repo, model_id: int) -> int:
    index = 0
    for _provider, models in await repo.list_providers():
        if not models:
            continue
        index += 1  # header row
        for m in models:
            if m.id == model_id:
                return index // PAGE_SIZE
            index += 1
    return 0


def build() -> Router:
    router = Router(name="admin_model")
    router.message.register(cmd_model, Command("model"), flags={"admin": True})
    router.callback_query.register(on_model_button, ModelCb.filter(), flags={"admin": True})
    return router
