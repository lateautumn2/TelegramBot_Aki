import json
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from sqlalchemy import delete, event, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from bot.db.models import (
    AccessEntry,
    Base,
    ChatSession,
    Message,
    Model,
    Provider,
    RequestLog,
    Setting,
    utcnow,
)
from bot.services.crypto import SecretBox
from bot.services.settings import BotSettings


@dataclass(frozen=True)
class ActiveModel:
    model_id: int
    model: str
    provider_id: int
    provider_name: str
    base_url: str
    api_key: str


class Repo:
    def __init__(self, db_url: str, secrets: SecretBox) -> None:
        self.engine: AsyncEngine = create_async_engine(db_url)
        self._sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self._secrets = secrets
        self._settings: BotSettings | None = None

        @event.listens_for(self.engine.sync_engine, "connect")
        def _enable_fk(dbapi_conn, _record) -> None:
            dbapi_conn.execute("PRAGMA foreign_keys=ON")

    async def init(self) -> None:
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def close(self) -> None:
        await self.engine.dispose()

    # ---- settings ----

    async def get_settings(self) -> BotSettings:
        """Cached; read on every message. Returns a copy safe to mutate."""
        if self._settings is None:
            async with self._sessions() as s:
                rows = (await s.scalars(select(Setting))).all()
            values = {r.key: json.loads(r.value_json) for r in rows if r.key in BotSettings.keys()}
            self._settings = BotSettings(**values)
        return replace(self._settings)

    async def set_setting(self, key: str, value: object) -> None:
        if key not in BotSettings.keys():
            raise KeyError(key)
        async with self._sessions.begin() as s:
            await s.merge(Setting(key=key, value_json=json.dumps(value, ensure_ascii=False)))
        self._settings = None

    async def mark_once(self, name: str) -> bool:
        """True the first time it is called for `name`, False ever after."""
        key = f"_once:{name}"
        async with self._sessions.begin() as s:
            if await s.get(Setting, key) is not None:
                return False
            s.add(Setting(key=key, value_json="true"))
        return True

    # ---- access control ----

    async def list_access(self) -> list[AccessEntry]:
        async with self._sessions() as s:
            return list((await s.scalars(select(AccessEntry).order_by(AccessEntry.created_at))).all())

    async def add_access(self, kind: str, target_id: int, name: str, added_by: int | None) -> bool:
        """Insert or rename an entry; True if it was new."""
        async with self._sessions.begin() as s:
            entry = await s.get(AccessEntry, (kind, target_id))
            if entry is not None:
                if name:
                    entry.name = name
                return False
            s.add(AccessEntry(kind=kind, target_id=target_id, name=name, added_by=added_by))
        return True

    async def remove_access(self, kind: str, target_id: int) -> bool:
        async with self._sessions.begin() as s:
            result = await s.execute(
                delete(AccessEntry).where(AccessEntry.kind == kind, AccessEntry.target_id == target_id)
            )
        return result.rowcount > 0

    # ---- providers / models ----

    async def count_providers(self) -> int:
        async with self._sessions() as s:
            return await s.scalar(select(func.count()).select_from(Provider)) or 0

    async def add_provider(self, name: str, base_url: str, api_key: str) -> Provider:
        async with self._sessions.begin() as s:
            provider = Provider(name=name, base_url=base_url, api_key_enc=self._secrets.encrypt(api_key))
            s.add(provider)
        return provider

    async def add_model(self, provider_id: int, name: str) -> Model:
        """Enable a model for a provider; returns the existing row if already enabled."""
        async with self._sessions.begin() as s:
            model = await s.scalar(select(Model).where(Model.provider_id == provider_id, Model.name == name))
            if model is None:
                model = Model(provider_id=provider_id, name=name)
                s.add(model)
        return model

    async def remove_model(self, provider_id: int, name: str) -> bool:
        async with self._sessions.begin() as s:
            result = await s.execute(delete(Model).where(Model.provider_id == provider_id, Model.name == name))
        return result.rowcount > 0

    async def get_model(self, model_id: int) -> Model | None:
        async with self._sessions() as s:
            return await s.get(Model, model_id)

    async def list_models(self, provider_id: int) -> list[Model]:
        async with self._sessions() as s:
            return list(
                (await s.scalars(select(Model).where(Model.provider_id == provider_id).order_by(Model.name))).all()
            )

    async def list_providers(self) -> list[tuple[Provider, list[Model]]]:
        """All providers with their enabled models, in creation order."""
        async with self._sessions() as s:
            providers = (await s.scalars(select(Provider).order_by(Provider.id))).all()
            models = (await s.scalars(select(Model).order_by(Model.name))).all()
        by_provider: dict[int, list[Model]] = {p.id: [] for p in providers}
        for model in models:
            by_provider.setdefault(model.provider_id, []).append(model)
        return [(p, by_provider[p.id]) for p in providers]

    async def get_provider(self, provider_id: int) -> Provider | None:
        async with self._sessions() as s:
            return await s.get(Provider, provider_id)

    def provider_key(self, provider: Provider) -> str:
        return self._secrets.decrypt(provider.api_key_enc)

    async def provider_name_taken(self, name: str, exclude_id: int | None = None) -> bool:
        query = select(Provider.id).where(func.lower(Provider.name) == name.lower())
        if exclude_id is not None:
            query = query.where(Provider.id != exclude_id)
        async with self._sessions() as s:
            return await s.scalar(query) is not None

    async def update_provider(
        self, provider_id: int, *, name: str | None = None, base_url: str | None = None, api_key: str | None = None
    ) -> None:
        async with self._sessions.begin() as s:
            provider = await s.get(Provider, provider_id)
            if provider is None:
                raise KeyError(provider_id)
            if name is not None:
                provider.name = name
            if base_url is not None:
                provider.base_url = base_url
            if api_key is not None:
                provider.api_key_enc = self._secrets.encrypt(api_key)

    async def delete_provider(self, provider_id: int) -> None:
        async with self._sessions.begin() as s:
            await s.execute(delete(Provider).where(Provider.id == provider_id))

    async def get_active_model(self) -> ActiveModel | None:
        """The configured current model, or the first enabled one as a fallback."""
        settings = await self.get_settings()
        query = (
            select(Model, Provider)
            .join(Provider, Model.provider_id == Provider.id)
            .where(Model.enabled, Provider.enabled)
        )
        async with self._sessions() as s:
            row = None
            if settings.current_model_id is not None:
                row = (await s.execute(query.where(Model.id == settings.current_model_id))).first()
            if row is None:
                row = (await s.execute(query.order_by(Model.id).limit(1))).first()
        if row is None:
            return None
        model, provider = row
        return ActiveModel(
            model_id=model.id,
            model=model.name,
            provider_id=provider.id,
            provider_name=provider.name,
            base_url=provider.base_url,
            api_key=self._secrets.decrypt(provider.api_key_enc),
        )

    # ---- conversation ----

    async def get_or_create_session(self, scope_key: str, timeout_min: int) -> ChatSession:
        """Latest session for the scope, or a fresh one if it has been idle too long."""
        async with self._sessions.begin() as s:
            current = await s.scalar(
                select(ChatSession).where(ChatSession.scope_key == scope_key).order_by(ChatSession.id.desc()).limit(1)
            )
            expired = (
                current is not None
                and timeout_min > 0
                and utcnow() - current.last_active_at > timedelta(minutes=timeout_min)
            )
            if current is None or expired:
                current = ChatSession(scope_key=scope_key)
                s.add(current)
        return current

    async def new_session(self, scope_key: str) -> ChatSession:
        async with self._sessions.begin() as s:
            session = ChatSession(scope_key=scope_key)
            s.add(session)
        return session

    async def load_history(self, session_id: int, limit: int) -> list[tuple[str, str]]:
        """Most recent `limit` messages of a session, oldest first."""
        async with self._sessions() as s:
            rows = (
                await s.execute(
                    select(Message.role, Message.content)
                    .where(Message.session_id == session_id)
                    .order_by(Message.id.desc())
                    .limit(limit)
                )
            ).all()
        return [(role, content) for role, content in reversed(rows)]

    async def append_messages(self, session_id: int, messages: list[tuple[str, str]]) -> None:
        async with self._sessions.begin() as s:
            s.add_all(Message(session_id=session_id, role=role, content=content) for role, content in messages)
            session = await s.get(ChatSession, session_id)
            if session is not None:
                session.last_active_at = utcnow()

    # ---- request log ----

    async def log_request(self, scope_key: str, model: str, status: str, error: str = "") -> None:
        async with self._sessions.begin() as s:
            s.add(RequestLog(scope_key=scope_key, model=model, status=status, error=error))

    async def request_counts(self, since: datetime) -> dict[str, int]:
        """Requests per status (ok / stopped / error) created at or after `since` (naive UTC)."""
        async with self._sessions() as s:
            rows = await s.execute(
                select(RequestLog.status, func.count())
                .where(RequestLog.created_at >= since)
                .group_by(RequestLog.status)
            )
            return dict(rows.all())

    async def last_error(self) -> RequestLog | None:
        async with self._sessions() as s:
            return await s.scalar(
                select(RequestLog).where(RequestLog.status == "error").order_by(RequestLog.id.desc()).limit(1)
            )
