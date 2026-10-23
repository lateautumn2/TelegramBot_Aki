import re

from aiogram.enums import ChatType
from aiogram.types import Chat, Message, User

_WIDE_CHARS = re.compile(r"[⺀-鿿가-힯豈-﫿＀-￯]")


def estimate_tokens(text: str) -> int:
    """Rough token count: CJK ~1 token/char, other text ~4 chars/token."""
    wide = len(_WIDE_CHARS.findall(text))
    return wide + (len(text) - wide + 3) // 4 + 4  # +4 per-message overhead


def scope_key(chat: Chat, user: User) -> str:
    """Conversation isolation: one session per private chat, per user within a group."""
    if chat.type == ChatType.PRIVATE:
        return f"p:{user.id}"
    return f"g:{chat.id}:{user.id}"


def real_reply(message: Message) -> Message | None:
    """The message this one replies to, ignoring the implicit reply to a forum
    topic's root that Telegram attaches to every message in a topic."""
    reply = message.reply_to_message
    if reply is None:
        return None
    if message.is_topic_message and reply.message_id == message.message_thread_id:
        return None
    return reply


def build_messages(
    system_prompt: str,
    history: list[tuple[str, str]],
    user_text: str,
    token_budget: int,
) -> list[dict]:
    """System prompt + as much recent history as fits the budget + the new message."""
    budget = token_budget - estimate_tokens(system_prompt) - estimate_tokens(user_text)
    kept: list[tuple[str, str]] = []
    for role, content in reversed(history):
        cost = estimate_tokens(content)
        if cost > budget:
            break
        budget -= cost
        kept.append((role, content))
    kept.reverse()
    # A context starting with an orphaned assistant reply confuses some models.
    while kept and kept[0][0] != "user":
        kept.pop(0)

    messages = [{"role": "system", "content": system_prompt}] if system_prompt else []
    messages += [{"role": role, "content": content} for role, content in kept]
    messages.append({"role": "user", "content": user_text})
    return messages
