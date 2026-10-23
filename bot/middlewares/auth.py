import time
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.dispatcher.flags import get_flag
from aiogram.enums import ChatType
from aiogram.types import CallbackQuery, Message, TelegramObject

from bot.db.repo import Repo
from bot.services.acl import AccessControl, Role

# Don't answer the same stranger more than once per this many seconds.
_NOTICE_COOLDOWN = 600


class AccessMiddleware(BaseMiddleware):
    """Default-deny gate in front of every message and callback handler.

    Handler flags:
      public       — runs for everyone (e.g. /start, /id)
      admin/owner  — requires that role
      private_only — refuses to run in groups (commands that show secrets)

    Sets `role`, `is_admin` and `authorized` in handler data.
    """

    def __init__(self, acl: AccessControl, repo: Repo) -> None:
        self.acl = acl
        self.repo = repo
        self._noticed: dict[int, float] = {}

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if isinstance(event, Message):
            user, chat = event.from_user, event.chat
        elif isinstance(event, CallbackQuery):
            user, chat = event.from_user, event.message.chat if event.message else None
        else:
            return None
        if user is None or user.is_bot:
            # Service messages (e.g. group migration) may have no human sender.
            if get_flag(data, "public"):
                data.update(role=Role.NONE, is_admin=False, authorized=False)
                return await handler(event, data)
            return None

        settings = await self.repo.get_settings()
        role = self.acl.role(user, chat, settings)
        data["role"] = role
        data["is_admin"] = role >= Role.ADMIN
        data["authorized"] = role >= Role.MEMBER

        if get_flag(data, "public"):
            return await handler(event, data)

        if role == Role.NONE:
            await self._deny_stranger(event)
            return None

        needed = Role.OWNER if get_flag(data, "owner") else Role.ADMIN if get_flag(data, "admin") else Role.MEMBER
        if role < needed:
            who = "超级管理员" if needed == Role.OWNER else "管理员"
            await self._reply(event, f"⛔ 该操作仅{who}可用。")
            return None

        if get_flag(data, "private_only") and chat is not None and chat.type != ChatType.PRIVATE:
            await self._reply(event, "🔒 该命令只能在私聊中使用。")
            return None

        return await handler(event, data)

    @staticmethod
    async def _reply(event: Message | CallbackQuery, text: str) -> None:
        if isinstance(event, CallbackQuery):
            await event.answer(text, show_alert=True)
        else:
            await event.reply(text)

    async def _deny_stranger(self, event: Message | CallbackQuery) -> None:
        if isinstance(event, CallbackQuery):
            await event.answer("你没有使用权限。", show_alert=True)
            return
        # Groups: stay silent so the bot doesn't spam chats it isn't meant for.
        if event.chat.type != ChatType.PRIVATE:
            return
        user_id = event.from_user.id
        now = time.monotonic()
        if now - self._noticed.get(user_id, 0.0) > _NOTICE_COOLDOWN:
            self._noticed[user_id] = now
            await event.answer(f"你还没有使用权限。请把你的 ID 发给管理员：<code>{user_id}</code>")
