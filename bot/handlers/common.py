from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from bot.db.repo import Repo
from bot.services.acl import Role
from bot.services.context import scope_key

USER_HELP = (
    "私聊：直接发送消息即可与 AI 对话，我会记住上下文。\n"
    "群聊：@我、回复我的消息，或使用 /ask 提问。\n\n"
    "/new — 开启新对话（清空上下文）\n"
    "/id — 查看你的 user_id 和当前 chat_id\n"
    "/help — 显示本帮助"
)
ADMIN_HELP = (
    "\n\n<b>管理员命令</b>\n"
    "/model [模型名] — 切换模型（对所有人生效）\n"
    "/provider — 管理接入配置：Base URL、API Key、启用的模型（仅私聊）\n"
    "/settings — 系统提示词、temperature 等参数（仅私聊）\n"
    "/status — 运行状态、今日请求数（仅私聊）\n"
    "/allow_user &lt;id&gt; — 添加白名单用户（也可回复对方的消息使用）\n"
    "/deny_user &lt;id&gt; — 移除白名单用户\n"
    "/allow_group [chat_id] — 授权群组（在群内使用则授权当前群）\n"
    "/deny_group [chat_id] — 取消授权群组\n"
    "/whitelist — 查看和管理白名单（仅私聊）\n"
    "/cancel — 取消正在进行的输入"
)
OWNER_HELP = "\n/add_admin &lt;id&gt; — 添加管理员\n/remove_admin &lt;id&gt; — 移除管理员"


def help_text(role: Role) -> str:
    text = USER_HELP
    if role >= Role.ADMIN:
        text += ADMIN_HELP
    if role >= Role.OWNER:
        text += OWNER_HELP
    return text


async def cmd_start(message: Message, authorized: bool, role: Role) -> None:
    if authorized:
        await message.answer(f"你好，{message.from_user.full_name}！\n\n{help_text(role)}")
    else:
        await message.answer(f"你还没有使用权限。请把你的 ID 发给管理员：<code>{message.from_user.id}</code>")


async def cmd_help(message: Message, role: Role) -> None:
    await message.answer(help_text(role))


async def cmd_id(message: Message) -> None:
    await message.answer(f"user_id: <code>{message.from_user.id}</code>\nchat_id: <code>{message.chat.id}</code>")


async def cmd_cancel(message: Message, state: FSMContext) -> None:
    had_state = await state.get_state() is not None
    await state.clear()
    await message.reply("已取消。" if had_state else "当前没有进行中的操作。")


async def cmd_new(message: Message, repo: Repo) -> None:
    await repo.new_session(scope_key(message.chat, message.from_user))
    await message.reply("🆕 已开启新对话。")


def build() -> Router:
    router = Router(name="common")
    router.message.register(cmd_start, CommandStart(), flags={"public": True})
    router.message.register(cmd_id, Command("id"), flags={"public": True})
    router.message.register(cmd_help, Command("help"))
    router.message.register(cmd_new, Command("new", "reset"))
    router.message.register(cmd_cancel, Command("cancel"))
    return router
