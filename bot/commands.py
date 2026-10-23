"""Telegram command menus, scoped so each role only sees what it can use."""

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BotCommand, BotCommandScopeAllGroupChats, BotCommandScopeChat, BotCommandScopeDefault

from bot.services.acl import AccessControl, Kind, Role

log = logging.getLogger(__name__)

USER_COMMANDS = [
    BotCommand(command="new", description="开启新对话"),
    BotCommand(command="help", description="使用帮助"),
    BotCommand(command="id", description="查看我的 ID"),
]
GROUP_COMMANDS = [
    BotCommand(command="ask", description="向 AI 提问"),
    BotCommand(command="new", description="开启新对话"),
    BotCommand(command="id", description="查看 ID"),
]
ADMIN_COMMANDS = USER_COMMANDS + [
    BotCommand(command="model", description="切换模型"),
    BotCommand(command="provider", description="管理接入配置（URL / Key / 模型）"),
    BotCommand(command="settings", description="参数配置"),
    BotCommand(command="status", description="运行状态"),
    BotCommand(command="whitelist", description="管理白名单"),
    BotCommand(command="allow_user", description="添加白名单用户"),
    BotCommand(command="deny_user", description="移除白名单用户"),
    BotCommand(command="allow_group", description="授权群组"),
    BotCommand(command="deny_group", description="取消授权群组"),
    BotCommand(command="cancel", description="取消正在进行的操作"),
]
OWNER_COMMANDS = ADMIN_COMMANDS + [
    BotCommand(command="add_admin", description="添加管理员"),
    BotCommand(command="remove_admin", description="移除管理员"),
]


async def setup_commands(bot: Bot, acl: AccessControl) -> None:
    await bot.set_my_commands(USER_COMMANDS, scope=BotCommandScopeDefault())
    await bot.set_my_commands(GROUP_COMMANDS, scope=BotCommandScopeAllGroupChats())
    for user_id in acl.owner_ids | {e.id for e in acl.entries(Kind.ADMIN)}:
        await refresh_user_commands(bot, acl, user_id)


async def refresh_user_commands(bot: Bot, acl: AccessControl, user_id: int) -> None:
    """Set the private-chat menu of `user_id` to match their current role."""
    scope = BotCommandScopeChat(chat_id=user_id)
    role = acl.admin_role(user_id)
    try:
        if role == Role.OWNER:
            await bot.set_my_commands(OWNER_COMMANDS, scope=scope)
        elif role == Role.ADMIN:
            await bot.set_my_commands(ADMIN_COMMANDS, scope=scope)
        else:
            await bot.delete_my_commands(scope=scope)
    except TelegramAPIError as exc:
        # Fails until the user has opened a private chat with the bot.
        log.info("could not set commands for %s: %s", user_id, exc)
