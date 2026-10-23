from pathlib import Path
from typing import Annotated

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Config(BaseSettings):
    """Startup configuration from environment / .env.

    Only bootstrap values live here; everything an admin can change at
    runtime is stored in the database (see bot.services.settings).
    """

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bot_token: str
    admin_ids: Annotated[frozenset[int], NoDecode] = frozenset()
    secret_key: str

    # Imported into the database whitelist on first start only.
    allowed_user_ids: Annotated[frozenset[int], NoDecode] = frozenset()
    allowed_chat_ids: Annotated[frozenset[int], NoDecode] = frozenset()

    default_base_url: str = ""
    default_api_key: str = ""
    default_model: str = ""

    proxy_url: str = ""
    data_dir: Path = Path("./data")
    log_level: str = "INFO"

    @field_validator("admin_ids", "allowed_user_ids", "allowed_chat_ids", mode="before")
    @classmethod
    def _parse_ids(cls, value: object) -> object:
        if isinstance(value, str):
            return frozenset(int(part) for part in value.replace(" ", "").split(",") if part)
        return value

    @field_validator("secret_key")
    @classmethod
    def _check_secret(cls, value: str) -> str:
        if len(value) < 16:
            raise ValueError("SECRET_KEY must be at least 16 characters")
        return value

    @property
    def db_url(self) -> str:
        return f"sqlite+aiosqlite:///{(self.data_dir / 'bot.db').as_posix()}"
