import logging

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.filters import JOIN_TRANSITION, ChatMemberUpdatedFilter
from aiogram.types import ChatMemberUpdated, Message

from bot.db.repo import Repo
from bot.services.acl import AccessControl, Kind, Role

log = logging.getLogger(__name__)


async def on_bot_added(event: ChatMemberUpdated, bot: Bot, acl: AccessControl, repo: Repo) -> None:
    chat = event.chat
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP) or acl.has(Kind.CHAT, chat.id):
        return
    if acl.admin_role(event.from_user.id) >= Role.ADMIN:
        await bot.send_message(chat.id, "本群尚未授权。管理员在群内发送 /allow_group 即可授权本群。")
        return
    settings = await repo.get_settings()
    if settings.leave_unauthorized_groups:
        log.info("leaving unauthorized group %s (%s), added by %s", chat.id, chat.title, event.from_user.id)
        await bot.leave_chat(chat.id)


async def on_migrate(message: Message, acl: AccessControl) -> None:
    """A group upgraded to a supergroup gets a new chat id; carry its authorization over."""
    if message.migrate_to_chat_id:
        old, new = message.chat.id, message.migrate_to_chat_id
    else:
        old, new = message.migrate_from_chat_id, message.chat.id
    if acl.has(Kind.CHAT, old):
        name = next((e.name for e in acl.entries(Kind.CHAT) if e.id == old), "")
        await acl.add(Kind.CHAT, new, message.chat.title or name)
        await acl.remove(Kind.CHAT, old)
        log.info("group %s migrated to %s; authorization moved", old, new)


def build() -> Router:
    router = Router(name="membership")
    router.my_chat_member.register(on_bot_added, ChatMemberUpdatedFilter(JOIN_TRANSITION))
    router.message.register(on_migrate, F.migrate_to_chat_id | F.migrate_from_chat_id, flags={"public": True})
    return router
