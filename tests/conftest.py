import pytest
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from helpers import FAKE_TOKEN, FakeLLM, Harness, RecordingSession, make_config

from bot.db.repo import Repo
from bot.handlers import chat as chat_handlers
from bot.main import bootstrap_access, bootstrap_provider, build_dispatcher
from bot.services.acl import AccessControl
from bot.services.crypto import SecretBox


@pytest.fixture
async def h(tmp_path):
    chat_handlers._last_reply.clear()
    config = make_config(tmp_path)
    repo = Repo(config.db_url, SecretBox(config.secret_key))
    await repo.init()
    await bootstrap_provider(repo, config)
    acl = AccessControl(repo, config.admin_ids)
    await bootstrap_access(acl, repo, config)
    llm, session = FakeLLM(), RecordingSession()
    bot = Bot(FAKE_TOKEN, session=session, default=DefaultBotProperties(parse_mode="HTML"))
    yield Harness(build_dispatcher(config, repo, llm, acl), bot, session, llm, repo, acl)
    await repo.close()
