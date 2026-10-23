from datetime import timedelta

import pytest
from sqlalchemy import update

from bot.db.models import ChatSession, utcnow
from bot.db.repo import Repo
from bot.services.crypto import SecretBox


@pytest.fixture
async def repo(tmp_path):
    r = Repo(f"sqlite+aiosqlite:///{(tmp_path / 't.db').as_posix()}", SecretBox("test-secret-123456"))
    await r.init()
    yield r
    await r.close()


async def test_settings_defaults_and_roundtrip(repo):
    s = await repo.get_settings()
    assert s.history_limit == 20 and s.stream is True
    await repo.set_setting("system_prompt", "你是助手")
    await repo.set_setting("max_tokens", 512)
    s = await repo.get_settings()
    assert s.system_prompt == "你是助手" and s.max_tokens == 512
    with pytest.raises(KeyError):
        await repo.set_setting("nope", 1)


async def test_active_model_decrypts_and_falls_back(repo):
    assert await repo.get_active_model() is None
    p = await repo.add_provider("p1", "https://x.com/v1", "sk-secret")
    m1 = await repo.add_model(p.id, "m1")
    m2 = await repo.add_model(p.id, "m2")

    active = await repo.get_active_model()
    assert (active.model, active.api_key) == ("m1", "sk-secret")  # first model when unset

    await repo.set_setting("current_model_id", m2.id)
    assert (await repo.get_active_model()).model == "m2"

    await repo.set_setting("current_model_id", 9999)  # dangling id
    assert (await repo.get_active_model()).model == "m1"
    assert m1.id != m2.id


async def test_history_order_limit_and_new_session(repo):
    s = await repo.get_or_create_session("p:1", timeout_min=60)
    await repo.append_messages(s.id, [("user", "q1"), ("assistant", "a1")])
    await repo.append_messages(s.id, [("user", "q2"), ("assistant", "a2")])
    assert await repo.load_history(s.id, 3) == [("assistant", "a1"), ("user", "q2"), ("assistant", "a2")]

    assert (await repo.get_or_create_session("p:1", 60)).id == s.id
    fresh = await repo.new_session("p:1")
    assert (await repo.get_or_create_session("p:1", 60)).id == fresh.id
    assert await repo.load_history(fresh.id, 10) == []


async def test_idle_session_expires(repo):
    s = await repo.get_or_create_session("p:2", timeout_min=60)
    async with repo.engine.begin() as conn:
        await conn.execute(
            update(ChatSession).where(ChatSession.id == s.id).values(last_active_at=utcnow() - timedelta(hours=2))
        )
    assert (await repo.get_or_create_session("p:2", 60)).id != s.id
    # timeout 0 disables expiry
    s3 = await repo.new_session("p:3")
    async with repo.engine.begin() as conn:
        await conn.execute(
            update(ChatSession).where(ChatSession.id == s3.id).values(last_active_at=utcnow() - timedelta(days=9))
        )
    assert (await repo.get_or_create_session("p:3", 0)).id == s3.id
