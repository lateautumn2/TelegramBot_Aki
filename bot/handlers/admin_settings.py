"""/settings: runtime parameters as a button menu."""

import html
from dataclasses import dataclass

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, StateFilter
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.db.repo import Repo
from bot.services.settings import BotSettings


@dataclass(frozen=True)
class Spec:
    key: str
    label: str
    kind: str  # text | int | float | bool | choice | multi
    min: float | None = None
    max: float | None = None
    choices: dict[str, str] | None = None
    hint: str = ""


TRIGGERS = {"mention": "@Bot", "reply": "回复 Bot", "command": "/ask"}

SPECS = [
    Spec("system_prompt", "系统提示词", "text", max=4000, hint="发送 <code>-</code> 表示不使用系统提示词。"),
    Spec("temperature", "temperature", "float", 0, 2, hint="越高越发散，越低越稳定。"),
    Spec("max_tokens", "单次回复上限", "int", 0, 1_000_000, hint="单位 token，0 表示不限制。"),
    Spec("history_limit", "保留历史条数", "int", 0, 500, hint="每次请求最多带上多少条历史消息。"),
    Spec("context_token_budget", "上下文 token 预算", "int", 500, 2_000_000, hint="历史超出预算时从最旧的开始丢弃。"),
    Spec("session_timeout_min", "闲置自动新对话", "int", 0, 525_600, hint="单位分钟，0 表示永不过期。"),
    Spec("request_timeout_s", "请求超时", "int", 5, 600, hint="单位秒。"),
    Spec("stream", "流式输出", "bool"),
    Spec("group_triggers", "群聊触发方式", "multi", choices=TRIGGERS),
    Spec("access_mode", "访问模式", "choice", choices={"whitelist": "仅白名单", "open": "对所有人开放"}),
    Spec("allow_whitelisted_user_in_any_group", "白名单用户可在未授权群使用", "bool"),
    Spec("leave_unauthorized_groups", "被陌生人拉进群时自动退群", "bool"),
]
_DEFAULTS = BotSettings()


class SettingsForm(StatesGroup):
    value = State()


class SetCb(CallbackData, prefix="set"):
    action: str  # menu | open | multi | reset | cancel
    i: int = 0
    value: str = ""


def _btn(text: str, **cb) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=SetCb(**cb).pack())


def display(spec: Spec, value: object) -> str:
    if spec.kind == "bool":
        return "开" if value else "关"
    if spec.kind == "choice":
        return spec.choices.get(value, str(value))
    if spec.kind == "multi":
        return "、".join(spec.choices[v] for v in spec.choices if v in value) or "全部关闭"
    if spec.key == "max_tokens" and not value:
        return "不限制"
    if spec.key == "session_timeout_min" and value == 0:
        return "永不过期"
    if spec.kind == "text":
        text = str(value).replace("\n", " ") or "（不使用）"
        return text if len(text) <= 24 else text[:24] + "…"
    return str(value)


def render_menu(settings: BotSettings) -> tuple[str, InlineKeyboardMarkup]:
    rows = [[_btn(f"{spec.label}：{display(spec, getattr(settings, spec.key))}", action="open", i=i)]
            for i, spec in enumerate(SPECS)]
    text = "⚙️ <b>参数配置</b>\n修改后立即生效，重启不丢失。点击要修改的项："
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def render_multi(spec_index: int, settings: BotSettings) -> tuple[str, InlineKeyboardMarkup]:
    spec = SPECS[spec_index]
    current = getattr(settings, spec.key)
    rows = [
        [_btn(f"{'✅' if key in current else '⬜'} {label}", action="multi", i=spec_index, value=key)]
        for key, label in spec.choices.items()
    ]
    rows.append([_btn("« 返回", action="menu")])
    text = f"<b>{spec.label}</b>\n在群里可以通过勾选的方式向 Bot 提问（对管理员同样生效）。私聊不受影响。"
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def _edit(query: CallbackQuery, text: str, markup: InlineKeyboardMarkup | None) -> None:
    if not isinstance(query.message, Message):
        return
    try:
        await query.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as exc:
        if "message is not modified" not in exc.message:
            raise


async def cmd_settings(message: Message, state: FSMContext, repo: Repo) -> None:
    await state.clear()
    text, markup = render_menu(await repo.get_settings())
    await message.answer(text, reply_markup=markup)


async def on_settings_button(query: CallbackQuery, callback_data: SetCb, state: FSMContext, repo: Repo) -> None:
    action = callback_data.action
    if action != "menu" and not 0 <= callback_data.i < len(SPECS):
        await query.answer("菜单已过期，请重新打开 /settings", show_alert=True)
        return
    spec = SPECS[callback_data.i]
    settings = await repo.get_settings()
    notice, alert = None, False

    if action == "menu":
        await _edit(query, *render_menu(settings))
    elif action == "cancel":
        await state.clear()
        await _edit(query, *render_menu(settings))
    elif action == "reset":
        await state.clear()
        await repo.set_setting(spec.key, getattr(_DEFAULTS, spec.key))
        notice = f"{spec.label} 已恢复默认"
        await _edit(query, *render_menu(await repo.get_settings()))
    elif action == "multi":
        current = list(getattr(settings, spec.key))
        value = callback_data.value
        current = [v for v in current if v != value] if value in current else current + [value]
        await repo.set_setting(spec.key, [k for k in spec.choices if k in current])
        await _edit(query, *render_multi(callback_data.i, await repo.get_settings()))
    elif spec.kind == "bool":
        await repo.set_setting(spec.key, not getattr(settings, spec.key))
        notice = f"{spec.label}：{'已开启' if not getattr(settings, spec.key) else '已关闭'}"
        await _edit(query, *render_menu(await repo.get_settings()))
    elif spec.kind == "choice":
        keys = list(spec.choices)
        new = keys[(keys.index(getattr(settings, spec.key)) + 1) % len(keys)]
        await repo.set_setting(spec.key, new)
        notice = f"{spec.label}：{spec.choices[new]}"
        if spec.key == "access_mode" and new == "open":
            notice, alert = "⚠️ 已切换为对所有人开放：任何人私聊或在任何群里都可以使用 Bot。", True
        await _edit(query, *render_menu(await repo.get_settings()))
    elif spec.kind == "multi":
        await _edit(query, *render_multi(callback_data.i, settings))
    else:
        await state.set_state(SettingsForm.value)
        await state.update_data(setting=callback_data.i)
        current = getattr(settings, spec.key)
        shown = html.escape(str(current))[:3000] if current not in (None, "") else "（空）"
        default = display(spec, getattr(_DEFAULTS, spec.key))
        bounds = ""
        if spec.kind in ("int", "float"):
            bounds = f"\n范围：{spec.min:g} – {spec.max:g}"
        markup = InlineKeyboardMarkup(
            inline_keyboard=[[_btn(f"恢复默认（{default}）", action="reset", i=callback_data.i),
                              _btn("取消", action="cancel", i=callback_data.i)]]
        )
        if isinstance(query.message, Message):
            prompt = f"请发送新的「{spec.label}」。{bounds}\n{spec.hint}\n\n当前值：\n<code>{shown}</code>"
            await query.message.answer(prompt, reply_markup=markup)
    await query.answer(notice, show_alert=alert)


def parse_value(spec: Spec, raw: str) -> object:
    """Validate typed input for a text/number setting. Raises ValueError with a user-facing message."""
    raw = raw.strip()
    if spec.kind == "text":
        if raw == "-":
            return ""
        if len(raw) > spec.max:
            raise ValueError(f"太长了，最多 {spec.max:g} 个字符。")
        return raw
    try:
        value = int(raw) if spec.kind == "int" else float(raw)
    except ValueError:
        raise ValueError("请输入整数。" if spec.kind == "int" else "请输入数字，例如 0.7。") from None
    if not spec.min <= value <= spec.max:
        raise ValueError(f"超出范围，请输入 {spec.min:g} – {spec.max:g} 之间的值。")
    if spec.key == "max_tokens" and value == 0:
        return None
    return value


async def on_value(message: Message, state: FSMContext, repo: Repo) -> None:
    index = (await state.get_data()).get("setting")
    if index is None or not 0 <= index < len(SPECS):
        await state.clear()
        return
    spec = SPECS[index]
    try:
        value = parse_value(spec, message.text)
    except ValueError as exc:
        await message.reply(f"{exc}\n发送 /cancel 取消。")
        return
    await repo.set_setting(spec.key, value)
    await state.clear()
    await message.answer(f"✅ {spec.label} 已更新为：{html.escape(display(spec, value))}")
    text, markup = render_menu(await repo.get_settings())
    await message.answer(text, reply_markup=markup)


def build() -> Router:
    router = Router(name="admin_settings")
    flags = {"admin": True, "private_only": True}
    router.message.register(cmd_settings, Command("settings"), flags=flags)
    router.callback_query.register(on_settings_button, SetCb.filter(), flags=flags)
    router.message.register(on_value, StateFilter(SettingsForm.value), F.text, ~F.text.startswith("/"), flags=flags)
    return router
