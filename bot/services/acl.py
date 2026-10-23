from dataclasses import dataclass
from enum import Enum

from aiogram.enums import ChatType
from aiogram.types import Chat, User

from bot.db.repo import Repo
from bot.services.settings import BotSettings


class Kind(str, Enum):
    USER = "user"
    CHAT = "chat"
    ADMIN = "admin"


class Role(int, Enum):
    NONE = 0
    MEMBER = 1  # may chat here
    ADMIN = 2
    OWNER = 3  # from ADMIN_IDS; cannot be removed from inside the bot


@dataclass(frozen=True)
class Entry:
    kind: Kind
    id: int
    name: str


class AccessControl:
    """In-memory view of the whitelist tables; every write goes to the DB first."""

    def __init__(self, repo: Repo, owner_ids: frozenset[int]) -> None:
        self.repo = repo
        self.owner_ids = owner_ids
        self._entries: dict[Kind, dict[int, str]] = {kind: {} for kind in Kind}

    async def load(self) -> None:
        for kind in Kind:
            self._entries[kind].clear()
        for row in await self.repo.list_access():
            self._entries[Kind(row.kind)][row.target_id] = row.name

    def has(self, kind: Kind, target_id: int) -> bool:
        return target_id in self._entries[kind]

    def entries(self, kind: Kind) -> list[Entry]:
        return [Entry(kind, i, name) for i, name in self._entries[kind].items()]

    async def add(self, kind: Kind, target_id: int, name: str = "", added_by: int | None = None) -> bool:
        is_new = await self.repo.add_access(kind.value, target_id, name, added_by)
        if is_new or name:
            self._entries[kind][target_id] = name or self._entries[kind].get(target_id, "")
        return is_new

    async def remove(self, kind: Kind, target_id: int) -> bool:
        removed = await self.repo.remove_access(kind.value, target_id)
        self._entries[kind].pop(target_id, None)
        return removed

    def admin_role(self, user_id: int) -> Role:
        if user_id in self.owner_ids:
            return Role.OWNER
        if self.has(Kind.ADMIN, user_id):
            return Role.ADMIN
        return Role.NONE

    def role(self, user: User, chat: Chat | None, settings: BotSettings) -> Role:
        """What `user` may do in `chat` (None = no chat context, treated as private)."""
        admin = self.admin_role(user.id)
        if admin != Role.NONE:
            return admin
        if settings.access_mode == "open":
            return Role.MEMBER
        user_ok = self.has(Kind.USER, user.id)
        if chat is None or chat.type == ChatType.PRIVATE:
            return Role.MEMBER if user_ok else Role.NONE
        if self.has(Kind.CHAT, chat.id):
            return Role.MEMBER
        if user_ok and settings.allow_whitelisted_user_in_any_group:
            return Role.MEMBER
        return Role.NONE
