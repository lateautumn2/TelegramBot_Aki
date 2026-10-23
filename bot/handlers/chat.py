import asyncio
import contextlib
import html
import itertools
import logging
import re
from dataclasses import dataclass

from aiogram import Bot, F, Router
from aiogram.enums import ChatAction, ChatType, MessageEntityType
from aiogram.filters import BaseFilter, Command, CommandObject
from aiogram.filters.callback_data import CallbackData
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    MessageGenerationStopped,
)

from bot.db.repo import Repo
from bot.services.context import build_messages, real_reply, scope_key
from bot.services.llm import LLMError, LLMService
from bot.services.responder import StreamResponder

log = logging.getLogger(__name__)

# Scopes with a reply in progress; one request at a time per conversation.
_busy: set[str] = set()
# Telegram message ids of the latest bot reply per scope. Replying to one of
# these continues the conversation as-is; replying to anything else quotes it.
_last_reply: dict[str, set[int]] = {}

# Seconds between visible stream updates when editing a message.
_EDIT_INTERVAL_PRIVATE = 1.0
_EDIT_INTERVAL_GROUP = 3.0  # groups allow ~20 bot messages/edits per minute
_QUOTE_LIMIT = 2000
_GROUPS = {ChatType.GROUP, ChatType.SUPERGROUP}


class GroupTrigger(BaseFilter):
    """Group message addressed to the bot: @mention or a reply to its message,
    each subject to the `group_triggers` setting.

    Passes the text without the mention to the handler as `prompt`.
    """

    async def __call__(self, message: Message, bot: Bot, repo: Repo) -> bool | dict:
        if message.chat.type not in _GROUPS or not message.text or message.text.startswith("/"):
            return False
        triggers = (await repo.get_settings()).group_triggers
        me = await bot.me()
        mention = f"@{me.username}".lower()
        mentioned = "mention" in triggers and any(
            (e.type == MessageEntityType.MENTION and e.extract_from(message.text).lower() == mention)
            or (e.type == MessageEntityType.TEXT_MENTION and e.user is not None and e.user.id == me.id)
            for e in message.entities or []
        )
        reply = real_reply(message)
        replied_to_bot = (
            "reply" in triggers and reply is not None and reply.from_user is not None and reply.from_user.id == me.id
        )
        if not (mentioned or replied_to_bot):
            return False
        prompt = re.sub(rf"@{re.escape(me.username)}\b", "", message.text, flags=re.IGNORECASE).strip()
        return {"prompt": prompt}


async def _keep_typing(bot: Bot, chat_id: int, thread_id: int | None) -> None:
    """Telegram clears the typing status after ~5s; refresh until cancelled."""
    while True:
        with contextlib.suppress(Exception):
            await bot.send_chat_action(chat_id, ChatAction.TYPING, message_thread_id=thread_id)
        await asyncio.sleep(4)


def _with_quote(message: Message, prompt: str, key: str, bot_id: int) -> str:
    reply = real_reply(message)
    if reply is None:
        return prompt
    from_bot = reply.from_user is not None and reply.from_user.id == bot_id
    if from_bot and reply.message_id in _last_reply.get(key, ()):
        return prompt
    quoted = (reply.text or reply.caption or "").strip()
    if not quoted:
        return prompt
    if len(quoted) > _QUOTE_LIMIT:
        quoted = quoted[:_QUOTE_LIMIT] + "…"
    return f"引用的消息：\n{quoted}\n\n{prompt}"


async def on_private_text(message: Message, bot: Bot, repo: Repo, llm: LLMService, is_admin: bool) -> None:
    await _handle(message, message.text, bot, repo, llm, is_admin)


async def on_group_text(message: Message, prompt: str, bot: Bot, repo: Repo, llm: LLMService, is_admin: bool) -> None:
    await _handle(message, prompt, bot, repo, llm, is_admin)


async def on_ask(
    message: Message, command: CommandObject, bot: Bot, repo: Repo, llm: LLMService, is_admin: bool
) -> None:
    if message.chat.type in _GROUPS and "command" not in (await repo.get_settings()).group_triggers:
        return
    await _handle(message, (command.args or "").strip(), bot, repo, llm, is_admin)


async def on_unsupported(message: Message) -> None:
    await message.answer("暂时只支持文字消息。")


async def _handle(message: Message, prompt: str, bot: Bot, repo: Repo, llm: LLMService, is_admin: bool) -> None:
    if not prompt:
        await message.reply("请在消息里写上你的问题，例如：/ask 今天适合做什么？")
        return
    key = scope_key(message.chat, message.from_user)
    if key in _busy:
        await message.reply("⏳ 上一条消息还在生成中，请稍候。")
        return
    _busy.add(key)
    try:
        me = await bot.me()
        await _reply(message, _with_quote(message, prompt, key, me.id), bot, repo, llm, key, is_admin)
    finally:
        _busy.discard(key)


@dataclass
class _Generation:
    """A reply being generated; `task` is cancelled to stop it."""

    id: int
    key: str
    asker_id: int
    task: asyncio.Task | None = None
    stopped: bool = False

    def stop(self) -> None:
        self.stopped = True
        if self.task is not None:
            self.task.cancel()


class StopCb(CallbackData, prefix="stop"):
    gen: int


_gen_ids = itertools.count(1)
_generations: dict[int, _Generation] = {}  # by id, for the group stop button
_running: dict[str, _Generation] = {}  # by scope key, for the private-chat draft stop


async def _reply(
    message: Message, user_text: str, bot: Bot, repo: Repo, llm: LLMService, key: str, is_admin: bool
) -> None:
    active = await repo.get_active_model()
    if active is None:
        hint = "请用 /provider 添加接入配置并启用模型。" if is_admin else "请联系管理员。"
        await message.reply(f"⚠️ 尚未配置 AI 模型，{hint}")
        return

    settings = await repo.get_settings()
    session = await repo.get_or_create_session(key, settings.session_timeout_min)
    history = await repo.load_history(session.id, settings.history_limit)
    messages = build_messages(settings.system_prompt, history, user_text, settings.context_token_budget)

    gen = _Generation(id=next(_gen_ids), key=key, asker_id=message.from_user.id)
    stop_button = InlineKeyboardButton(text="⏹ 停止", callback_data=StopCb(gen=gen.id).pack())
    private = message.chat.type == ChatType.PRIVATE
    thread_id = message.message_thread_id if message.is_topic_message else None
    responder = StreamResponder(
        bot,
        message.chat.id,
        use_draft=private,
        edit_interval=_EDIT_INTERVAL_PRIVATE if private else _EDIT_INTERVAL_GROUP,
        reply_to=None if private else message.message_id,
        thread_id=thread_id,
        stop_markup=InlineKeyboardMarkup(inline_keyboard=[[stop_button]]),
    )
    typing = asyncio.create_task(_keep_typing(bot, message.chat.id, thread_id))
    text = ""

    async def generate() -> None:
        nonlocal text
        async for piece in llm.chat(active, messages, settings):
            typing.cancel()
            text += piece
            await responder.push(text)

    gen.task = asyncio.create_task(generate())
    _generations[gen.id] = _running[key] = gen
    error: LLMError | None = None
    try:
        await gen.task
    except asyncio.CancelledError:
        if not gen.stopped:
            raise  # the handler itself is being cancelled (shutdown)
    except LLMError as err:
        error = err
    finally:
        typing.cancel()
        _generations.pop(gen.id, None)
        _running.pop(key, None)

    text = text.strip()
    if error is not None:
        await repo.log_request(key, active.model, "error", error.user_message)
        notice = f"⚠️ {error.user_message}"
        if is_admin and error.detail:
            notice += f"\n\n<code>{html.escape(error.detail[:500])}</code>"
        if text:
            await responder.finish(text)
        await message.reply(notice)
        return

    if gen.stopped:
        await repo.log_request(key, active.model, "stopped")
        if not text:
            await responder.finish("⏹ 已停止生成。")
            return
        await responder.finish(f"{text}\n\n⏹ 已停止")
    else:
        await repo.log_request(key, active.model, "ok")
        await responder.finish(text)
    _last_reply[key] = {m.message_id for m in responder.messages}
    # A stopped reply is kept as-is, so a follow-up question sees what was said.
    await repo.append_messages(session.id, [("user", user_text), ("assistant", text)])


async def on_stop_button(query: CallbackQuery, callback_data: StopCb, is_admin: bool) -> None:
    gen = _generations.get(callback_data.gen)
    if gen is None:
        await query.answer("这条回复已经结束了。")
        return
    if query.from_user.id != gen.asker_id and not is_admin:
        await query.answer("只有提问的人或管理员可以停止。", show_alert=True)
        return
    gen.stop()
    await query.answer("已停止")


async def on_draft_stopped(event: MessageGenerationStopped) -> None:
    """The user pressed the native stop button on a private-chat draft."""
    gen = _running.get(f"p:{event.chat.id}")
    if gen is not None:
        gen.stop()


def build() -> Router:
    router = Router(name="chat")
    private = F.chat.type == ChatType.PRIVATE
    router.message.register(on_ask, Command("ask"))
    router.message.register(on_private_text, private, F.text, ~F.text.startswith("/"))
    router.message.register(on_unsupported, private, ~F.text)
    router.message.register(on_group_text, GroupTrigger())
    router.callback_query.register(on_stop_button, StopCb.filter())
    router.stopped_message_generation.register(on_draft_stopped)
    return router
