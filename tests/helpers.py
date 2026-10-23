"""Test harness: real Dispatcher + handlers + middleware, fake Telegram and LLM."""

import asyncio
from datetime import datetime
from itertools import count

from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import (
    GetChat,
    GetMe,
    SendMessage,
    TelegramMethod,
)
from aiogram.types import (
    CallbackQuery,
    Chat,
    ChatMemberLeft,
    ChatMemberMember,
    ChatMemberUpdated,
    Message,
    MessageEntity,
    Update,
    User,
)

from bot.config import Config
from bot.db.repo import Repo
from bot.services.acl import AccessControl
from bot.services.llm import LLMError

OWNER, ADMIN2, MEMBER, STRANGER = 1001, 1002, 2001, 3003
GROUP, OTHER_GROUP = -100111, -100222
BOT_ID, BOT_NAME = 999, "testbot"
BOT_USER = User(id=BOT_ID, is_bot=True, first_name="Bot", username=BOT_NAME)
FAKE_TOKEN = "123456:" + "A" * 35
API_KEY = "sk-test-" + "1234567890"


def chat_for(chat_id: int, **kw) -> Chat:
    return Chat(id=chat_id, type="private" if chat_id > 0 else "supergroup", **kw)


class RecordingSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.requests: list[TelegramMethod] = []
        self.sent: list[Message] = []
        self._ids = count(10_000)

    async def make_request(self, bot, method, timeout=None):
        self.requests.append(method)
        if isinstance(method, GetMe):
            return BOT_USER
        if isinstance(method, GetChat):
            raise TelegramBadRequest(method=method, message="Bad Request: chat not found")
        if isinstance(method, SendMessage):
            msg = Message(
                message_id=next(self._ids),
                date=datetime.now(),
                chat=chat_for(method.chat_id),
                from_user=BOT_USER,
                text=method.text,
            )
            msg = msg.as_(bot)  # so handlers can call msg.edit_text() etc.
            self.sent.append(msg)
            return msg
        return True

    async def stream_content(self, *args, **kwargs):
        raise NotImplementedError

    async def close(self) -> None:
        pass

    def of(self, kind: type) -> list:
        return [r for r in self.requests if isinstance(r, kind)]


class FakeLLM:
    def __init__(self) -> None:
        self.reply = "Hi **there**"
        self.error: LLMError | None = None
        self.calls: list[list[dict]] = []
        self.models: list[str] | LLMError = ["m1", "m2", "gpt-x", "gpt-y"]
        self.probe_error: LLMError | None = None
        self.probes: list[tuple[str, str, str]] = []
        # When set, generation pauses after the first word until the event is set.
        self.gate: asyncio.Event | None = None

    async def chat(self, active, messages, settings):
        self.calls.append(messages)
        if self.error:
            raise self.error
        for i, word in enumerate(self.reply.split(" ")):
            if i == 1 and self.gate is not None:
                await self.gate.wait()
            yield word + " "

    async def list_models(self, base_url, api_key):
        if isinstance(self.models, LLMError):
            raise self.models
        return list(self.models)

    async def probe(self, base_url, api_key, model):
        self.probes.append((base_url, api_key, model))
        if self.probe_error:
            raise self.probe_error

    @property
    def last_prompt(self) -> str:
        return self.calls[-1][-1]["content"]


class Harness:
    def __init__(self, dp, bot, session: RecordingSession, llm: FakeLLM, repo: Repo, acl: AccessControl) -> None:
        self.dp, self.bot, self.session, self.llm, self.repo, self.acl = dp, bot, session, llm, repo, acl
        self._ids = count(1)

    def texts(self) -> list[str]:
        return [r.text for r in self.session.of(SendMessage)]

    def last_text(self) -> str:
        return self.texts()[-1]

    def clear(self) -> None:
        self.session.requests.clear()

    async def _feed(self, **update) -> None:
        await self.dp.feed_update(self.bot, Update(update_id=next(self._ids), **update))

    def message(self, user_id: int, text: str, chat_id: int | None = None, **kw) -> Message:
        chat_id = user_id if chat_id is None else chat_id
        entities = kw.pop("entities", None)
        if entities is None and f"@{BOT_NAME}" in text:
            entities = [MessageEntity(type="mention", offset=text.index(f"@{BOT_NAME}"), length=len(BOT_NAME) + 1)]
        user = BOT_USER if user_id == BOT_ID else User(id=user_id, is_bot=False, first_name=f"user{user_id}")
        return Message(
            message_id=next(self._ids),
            date=datetime.now(),
            chat=chat_for(chat_id, title=None if chat_id > 0 else "Test Group"),
            from_user=user,
            text=text,
            entities=entities,
            **kw,
        )

    async def send(self, user_id: int, text: str, chat_id: int | None = None, **kw) -> Message:
        msg = self.message(user_id, text, chat_id, **kw)
        await self._feed(message=msg)
        return msg

    async def click(self, user_id: int, data: str, chat_id: int | None = None) -> None:
        chat = chat_for(user_id if chat_id is None else chat_id)
        source = Message(message_id=1, date=datetime.now(), chat=chat, from_user=BOT_USER, text="x")
        query = CallbackQuery(
            id="cb", from_user=User(id=user_id, is_bot=False, first_name="u"), chat_instance="c", data=data,
            message=source,
        )
        await self._feed(callback_query=query)

    async def bot_added(self, by_user: int, chat_id: int) -> None:
        event = ChatMemberUpdated(
            chat=chat_for(chat_id, title="New Group"),
            from_user=User(id=by_user, is_bot=False, first_name="u"),
            date=datetime.now(),
            old_chat_member=ChatMemberLeft(user=BOT_USER),
            new_chat_member=ChatMemberMember(user=BOT_USER),
        )
        await self._feed(my_chat_member=event)


def make_config(tmp_path, **overrides) -> Config:
    values = dict(
        bot_token=FAKE_TOKEN,
        admin_ids=str(OWNER),
        secret_key="test-secret-123456",
        default_base_url="https://api.test",
        default_api_key=API_KEY,
        default_model="m1",
        data_dir=tmp_path,
        _env_file=None,
    )
    values.update(overrides)
    return Config(**values)
