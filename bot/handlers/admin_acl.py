import html
import math

from aiogram import Bot, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.filters.callback_data import CallbackData
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.commands import refresh_user_commands
from bot.keyboards import nav_row
from bot.services.acl import AccessControl, Kind
from bot.services.context import real_reply

PAGE_SIZE = 8


class AclCb(CallbackData, prefix="acl"):
    action: str  # "rm" | "page"
    kind: str = ""
    id: int = 0
    page: int = 0


def label(target_id: int, name: str) -> str:
    return f"{html.escape(name)} (<code>{target_id}</code>)" if name else f"<code>{target_id}</code>"


async def _lookup_name(bot: Bot, chat_id: int) -> str:
    """Best-effort display name; only works for chats the bot can see."""
    try:
        chat = await bot.get_chat(chat_id)
    except TelegramAPIError:
        return ""
    return chat.title or " ".join(filter(None, [chat.first_name, chat.last_name])) or chat.username or ""


def _parse_id(command: CommandObject) -> int | None:
    args = (command.args or "").split()
    if not args:
        return None
    try:
        return int(args[0])
    except ValueError:
        return None


async def _target_user(message: Message, command: CommandObject, bot: Bot) -> tuple[int, str] | None:
    """User given as an argument, or the author of the message being replied to."""
    target = _parse_id(command)
    if target is not None:
        return target, await _lookup_name(bot, target)
    reply = real_reply(message)
    if reply is not None and reply.from_user is not None and not reply.from_user.is_bot:
        return reply.from_user.id, reply.from_user.full_name
    return None


async def _target_group(message: Message, command: CommandObject, bot: Bot) -> tuple[int, str] | None:
    """Group given as an argument, or the current group."""
    target = _parse_id(command)
    if target is not None:
        return target, await _lookup_name(bot, target)
    if message.chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
        return message.chat.id, message.chat.title or ""
    return None


# ---- users ----


async def cmd_allow_user(message: Message, command: CommandObject, bot: Bot, acl: AccessControl) -> None:
    target = await _target_user(message, command, bot)
    if target is None:
        await message.reply("用法：/allow_user &lt;user_id&gt;，或回复某人的消息发送 /allow_user")
        return
    user_id, name = target
    if await acl.add(Kind.USER, user_id, name, message.from_user.id):
        await message.reply(f"✅ 已将 {label(user_id, name)} 加入白名单。")
    else:
        await message.reply(f"ℹ️ {label(user_id, name)} 已在白名单中。")


async def cmd_deny_user(message: Message, command: CommandObject, bot: Bot, acl: AccessControl) -> None:
    target = await _target_user(message, command, bot)
    if target is None:
        await message.reply("用法：/deny_user &lt;user_id&gt;，或回复某人的消息发送 /deny_user")
        return
    user_id, name = target
    if await acl.remove(Kind.USER, user_id):
        await message.reply(f"✅ 已将 {label(user_id, name)} 移出白名单。")
    else:
        await message.reply(f"ℹ️ {label(user_id, name)} 不在白名单中。")


# ---- groups ----


async def cmd_allow_group(message: Message, command: CommandObject, bot: Bot, acl: AccessControl) -> None:
    target = await _target_group(message, command, bot)
    if target is None:
        await message.reply("用法：在群内发送 /allow_group，或私聊发送 /allow_group &lt;chat_id&gt;")
        return
    chat_id, title = target
    if chat_id >= 0:
        await message.reply("群组 ID 应为负数（通常以 -100 开头），可在群里发送 /id 查看。")
        return
    if await acl.add(Kind.CHAT, chat_id, title, message.from_user.id):
        await message.reply(f"✅ 已授权群组 {label(chat_id, title)}，群成员现在可以 @我 或回复我的消息来对话。")
    else:
        await message.reply(f"ℹ️ 群组 {label(chat_id, title)} 已授权。")


async def cmd_deny_group(message: Message, command: CommandObject, bot: Bot, acl: AccessControl) -> None:
    target = await _target_group(message, command, bot)
    if target is None:
        await message.reply("用法：在群内发送 /deny_group，或私聊发送 /deny_group &lt;chat_id&gt;")
        return
    chat_id, title = target
    if await acl.remove(Kind.CHAT, chat_id):
        await message.reply(f"✅ 已取消授权群组 {label(chat_id, title)}。")
    else:
        await message.reply(f"ℹ️ 群组 {label(chat_id, title)} 未授权。")


# ---- admins (owner only) ----


async def cmd_add_admin(message: Message, command: CommandObject, bot: Bot, acl: AccessControl) -> None:
    target = await _target_user(message, command, bot)
    if target is None:
        await message.reply("用法：/add_admin &lt;user_id&gt;，或回复某人的消息发送 /add_admin")
        return
    user_id, name = target
    if user_id in acl.owner_ids:
        await message.reply(f"ℹ️ {label(user_id, name)} 是超级管理员。")
        return
    if await acl.add(Kind.ADMIN, user_id, name, message.from_user.id):
        await refresh_user_commands(bot, acl, user_id)
        await message.reply(f"✅ 已将 {label(user_id, name)} 设为管理员。")
    else:
        await message.reply(f"ℹ️ {label(user_id, name)} 已经是管理员。")


async def cmd_remove_admin(message: Message, command: CommandObject, bot: Bot, acl: AccessControl) -> None:
    target = await _target_user(message, command, bot)
    if target is None:
        admins = acl.entries(Kind.ADMIN)
        listing = "\n".join(f"• {label(e.id, e.name)}" for e in admins) or "（无）"
        await message.reply(f"当前管理员：\n{listing}\n\n用法：/remove_admin &lt;user_id&gt;")
        return
    user_id, name = target
    if user_id in acl.owner_ids:
        await message.reply("超级管理员由 ADMIN_IDS 配置，无法在 Bot 内移除。")
        return
    if await acl.remove(Kind.ADMIN, user_id):
        await refresh_user_commands(bot, acl, user_id)
        await message.reply(f"✅ 已移除管理员 {label(user_id, name)}。")
    else:
        await message.reply(f"ℹ️ {label(user_id, name)} 不是管理员。")


# ---- /whitelist panel ----


def render_whitelist(acl: AccessControl, page: int) -> tuple[str, InlineKeyboardMarkup]:
    users, chats = acl.entries(Kind.USER), acl.entries(Kind.CHAT)
    items = users + chats
    pages = max(1, math.ceil(len(items) / PAGE_SIZE))
    page = min(max(page, 0), pages - 1)

    admins = [label(i, "") for i in sorted(acl.owner_ids)] + [label(e.id, e.name) for e in acl.entries(Kind.ADMIN)]
    text = f"📋 <b>白名单</b>：用户 {len(users)} 个，群组 {len(chats)} 个\n管理员：{'、'.join(admins) or '（无）'}"
    if items:
        text += f"\n\n点击按钮即可移除（第 {page + 1}/{pages} 页）"
    else:
        text += "\n\n白名单为空。使用 /allow_user 或 /allow_group 添加。"

    builder = InlineKeyboardBuilder()
    for entry in items[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]:
        icon = "👤" if entry.kind == Kind.USER else "👥"
        caption = f"{entry.name[:30]} ({entry.id})" if entry.name else str(entry.id)
        remove = AclCb(action="rm", kind=entry.kind.value, id=entry.id, page=page)
        builder.button(text=f"❌ {icon} {caption}", callback_data=remove)
    builder.adjust(1)
    if pages > 1:
        builder.row(*nav_row(page, pages, lambda p: AclCb(action="page", page=p)))
    return text, builder.as_markup()


async def cmd_whitelist(message: Message, acl: AccessControl) -> None:
    text, markup = render_whitelist(acl, 0)
    await message.answer(text, reply_markup=markup)


async def on_whitelist_button(query: CallbackQuery, callback_data: AclCb, acl: AccessControl) -> None:
    notice = None
    if callback_data.action == "rm":
        await acl.remove(Kind(callback_data.kind), callback_data.id)
        notice = "已移除"
    if isinstance(query.message, Message):
        text, markup = render_whitelist(acl, callback_data.page)
        try:
            await query.message.edit_text(text, reply_markup=markup)
        except TelegramBadRequest as exc:
            if "message is not modified" not in exc.message:
                raise
    await query.answer(notice)


def build() -> Router:
    router = Router(name="admin_acl")
    admin = {"admin": True}
    owner = {"owner": True}
    router.message.register(cmd_allow_user, Command("allow_user"), flags=admin)
    router.message.register(cmd_deny_user, Command("deny_user"), flags=admin)
    router.message.register(cmd_allow_group, Command("allow_group"), flags=admin)
    router.message.register(cmd_deny_group, Command("deny_group"), flags=admin)
    router.message.register(cmd_whitelist, Command("whitelist"), flags={**admin, "private_only": True})
    router.message.register(cmd_add_admin, Command("add_admin"), flags=owner)
    router.message.register(cmd_remove_admin, Command("remove_admin"), flags=owner)
    router.callback_query.register(on_whitelist_button, AclCb.filter(), flags=admin)
    return router

