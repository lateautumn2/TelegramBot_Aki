import asyncio
import logging
from datetime import datetime

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramUnauthorizedError

from bot.commands import setup_commands
from bot.config import Config
from bot.db.repo import Repo
from bot.handlers import build_router
from bot.handlers.admin_status import app_version
from bot.logging_setup import setup_logging
from bot.middlewares.auth import AccessMiddleware
from bot.services.acl import AccessControl, Kind
from bot.services.crypto import SecretBox
from bot.services.llm import LLMService, normalize_base_url

log = logging.getLogger("bot")


async def bootstrap_provider(repo: Repo, config: Config) -> None:
    """Create the initial provider from DEFAULT_* env vars on first start."""
    if await repo.count_providers():
        return
    if not (config.default_base_url and config.default_api_key and config.default_model):
        log.warning("No AI provider configured; set DEFAULT_BASE_URL / DEFAULT_API_KEY / DEFAULT_MODEL")
        return
    provider = await repo.add_provider("default", normalize_base_url(config.default_base_url), config.default_api_key)
    model = await repo.add_model(provider.id, config.default_model)
    await repo.set_setting("current_model_id", model.id)
    log.info("Created provider %r with model %r", provider.name, model.name)


async def bootstrap_access(acl: AccessControl, repo: Repo, config: Config) -> None:
    """Import ALLOWED_USER_IDS / ALLOWED_CHAT_IDS once, so later removals in the bot stick."""
    await acl.load()
    if not await repo.mark_once("seed_access"):
        return
    for user_id in config.allowed_user_ids:
        await acl.add(Kind.USER, user_id)
    for chat_id in config.allowed_chat_ids:
        await acl.add(Kind.CHAT, chat_id)
    if config.allowed_user_ids or config.allowed_chat_ids:
        log.info(
            "Imported %d users and %d groups into the whitelist",
            len(config.allowed_user_ids),
            len(config.allowed_chat_ids),
        )


def build_dispatcher(config: Config, repo: Repo, llm: LLMService, acl: AccessControl) -> Dispatcher:
    dp = Dispatcher(repo=repo, llm=llm, config=config, acl=acl, started_at=datetime.now())
    gate = AccessMiddleware(acl, repo)
    dp.message.middleware(gate)
    dp.callback_query.middleware(gate)
    dp.include_router(build_router())
    return dp


async def main() -> None:
    config = Config()
    setup_logging(config.log_level, config.data_dir)
    if not config.admin_ids:
        log.warning("ADMIN_IDS is empty: nobody can manage the bot")

    repo = Repo(config.db_url, SecretBox(config.secret_key))
    await repo.init()
    await bootstrap_provider(repo, config)
    acl = AccessControl(repo, config.admin_ids)
    await bootstrap_access(acl, repo, config)
    llm = LLMService(config.proxy_url)

    session = AiohttpSession(proxy=config.proxy_url) if config.proxy_url else None
    bot = Bot(config.bot_token, session=session, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = build_dispatcher(config, repo, llm, acl)

    try:
        me = await bot.get_me()
        await setup_commands(bot, acl)
        log.info("Starting @%s v%s (long polling)", me.username, app_version())
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    except TelegramUnauthorizedError:
        log.error("Telegram rejected BOT_TOKEN; check the token from @BotFather")
        raise SystemExit(1) from None
    finally:
        await llm.close()
        await repo.close()
        await bot.session.close()


def run() -> None:
    asyncio.run(main())
