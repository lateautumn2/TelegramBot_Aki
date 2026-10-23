"""Progressive delivery of a streamed LLM reply to Telegram.

Private chats use the native `sendMessageDraft` preview; groups (and private
chats where drafts fail) send one message and keep editing it. Text longer than
one message is split; earlier pieces are frozen as soon as they are complete.

With `stop_markup`, drafts show Telegram's native stop button and edited
messages carry `stop_markup` on the last piece until the reply is finished.
"""

import asyncio
import itertools
import logging
import time

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.types import InlineKeyboardMarkup, Message, ReplyParameters

from bot.services.render import md_to_html, split_markdown

log = logging.getLogger(__name__)

_draft_ids = itertools.count(int(time.time()) % 1_000_000 * 1000 + 1)


def _is_parse_error(exc: TelegramBadRequest) -> bool:
    return "can't parse entities" in exc.message or "unsupported start tag" in exc.message


class StreamResponder:
    def __init__(
        self,
        bot: Bot,
        chat_id: int,
        *,
        use_draft: bool,
        edit_interval: float,
        draft_interval: float = 0.6,
        reply_to: int | None = None,
        thread_id: int | None = None,
        stop_markup: InlineKeyboardMarkup | None = None,
    ) -> None:
        self.bot = bot
        self.chat_id = chat_id
        self.use_draft = use_draft
        self.edit_interval = edit_interval
        self.draft_interval = draft_interval
        self.reply_to = reply_to
        self.thread_id = thread_id
        self.stop_markup = stop_markup
        self.messages: list[Message] = []
        # What each of self.messages currently shows: (text, has stop button).
        self._shown: list[tuple[str, bool]] = []
        self._draft_id = next(_draft_ids)
        self._next_push = 0.0

    async def push(self, text: str) -> None:
        """Show partial text; calls inside the throttle interval are dropped."""
        now = time.monotonic()
        if now < self._next_push:
            return
        self._next_push = now + (self.draft_interval if self.use_draft else self.edit_interval)
        try:
            await self._sync(text, final=False)
        except TelegramRetryAfter as exc:
            self._next_push = time.monotonic() + exc.retry_after
        except TelegramBadRequest as exc:
            log.warning("stream update failed: %s", exc.message)

    async def finish(self, text: str) -> None:
        """Deliver the complete text; retries through flood limits."""
        if not text.strip():
            text = "（空回复）"
        for _ in range(3):
            try:
                await self._sync(text, final=True)
                return
            except TelegramRetryAfter as exc:
                await asyncio.sleep(exc.retry_after)
        await self._sync(text, final=True)

    async def _sync(self, text: str, final: bool) -> None:
        pieces = [p for p in split_markdown(text) if p.strip()]
        if not pieces:
            return
        if self.use_draft:
            done = pieces if final else pieces[:-1]
            for piece in done[len(self.messages) :]:
                await self._send(piece)
                self._draft_id = next(_draft_ids)
            if final:
                return
            try:
                await self._draft(pieces[-1])
                return
            except TelegramBadRequest as exc:
                log.info("drafts unavailable in chat %s (%s); falling back to edits", self.chat_id, exc.message)
                self.use_draft = False

        for i, piece in enumerate(pieces):
            # Only the piece still being written carries the stop button.
            markup = self.stop_markup if not final and i == len(pieces) - 1 else None
            if i < len(self.messages):
                if self._shown[i] != (piece, markup is not None):
                    await self._edit(i, piece, markup)
            else:
                await self._send(piece, markup)
        if final:
            # The final split can come out shorter than an intermediate one.
            for msg in self.messages[len(pieces) :]:
                await msg.delete()
            del self.messages[len(pieces) :], self._shown[len(pieces) :]

    async def _send(self, piece: str, markup: InlineKeyboardMarkup | None = None) -> None:
        reply = None
        if self.reply_to is not None and not self.messages:
            reply = ReplyParameters(message_id=self.reply_to, allow_sending_without_reply=True)
        kwargs = dict(
            chat_id=self.chat_id, message_thread_id=self.thread_id, reply_parameters=reply, reply_markup=markup
        )
        try:
            msg = await self.bot.send_message(text=md_to_html(piece), parse_mode=ParseMode.HTML, **kwargs)
        except TelegramBadRequest as exc:
            if not _is_parse_error(exc):
                raise
            msg = await self.bot.send_message(text=piece, parse_mode=None, **kwargs)
        self.messages.append(msg)
        self._shown.append((piece, markup is not None))

    async def _edit(self, index: int, piece: str, markup: InlineKeyboardMarkup | None = None) -> None:
        msg = self.messages[index]
        # Omitting reply_markup removes an existing keyboard.
        kwargs = dict(chat_id=self.chat_id, message_id=msg.message_id, reply_markup=markup)
        try:
            await self.bot.edit_message_text(text=md_to_html(piece), parse_mode=ParseMode.HTML, **kwargs)
        except TelegramBadRequest as exc:
            if "message is not modified" in exc.message:
                pass
            elif _is_parse_error(exc):
                await self.bot.edit_message_text(text=piece, parse_mode=None, **kwargs)
            else:
                raise
        self._shown[index] = (piece, markup is not None)

    async def _draft(self, piece: str) -> None:
        kwargs = dict(
            chat_id=self.chat_id,
            draft_id=self._draft_id,
            message_thread_id=self.thread_id,
            can_stop=True if self.stop_markup is not None else None,
        )
        try:
            await self.bot.send_message_draft(text=md_to_html(piece), parse_mode=ParseMode.HTML, **kwargs)
        except TelegramBadRequest as exc:
            if not _is_parse_error(exc):
                raise
            await self.bot.send_message_draft(text=piece, parse_mode=None, **kwargs)
