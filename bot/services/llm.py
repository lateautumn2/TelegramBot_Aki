import logging
from collections.abc import AsyncIterator
from urllib.parse import urlsplit, urlunsplit

import openai
from openai import AsyncOpenAI

from bot.db.repo import ActiveModel
from bot.logging_setup import redact
from bot.services.settings import BotSettings

log = logging.getLogger(__name__)


def normalize_base_url(url: str) -> str:
    """Accept `https://host`, `https://host/v1` or `https://host/v1/chat/completions`."""
    url = url.strip().rstrip("/")
    for suffix in ("/chat/completions", "/completions"):
        if url.endswith(suffix):
            url = url[: -len(suffix)]
            break
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError(f"invalid base URL: {url!r}")
    path = parts.path.rstrip("/") or "/v1"
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


class LLMError(Exception):
    """An upstream failure with a message that is safe to show to users.

    `kind` lets callers react to specific failures: auth, connection, timeout,
    not_found, rate_limit, bad_request, status, unknown.
    """

    def __init__(self, user_message: str, detail: str = "", kind: str = "unknown") -> None:
        super().__init__(user_message)
        self.user_message = user_message
        self.detail = redact(detail)
        self.kind = kind


def _translate(exc: Exception) -> LLMError:
    detail = f"{type(exc).__name__}: {exc}"
    if isinstance(exc, openai.APITimeoutError):
        return LLMError("请求超时，请稍后重试。", detail, "timeout")
    if isinstance(exc, openai.APIConnectionError):
        return LLMError("无法连接到 AI 服务，请检查网络或 Base URL。", detail, "connection")
    if isinstance(exc, openai.AuthenticationError):
        return LLMError("API Key 无效或已过期。", detail, "auth")
    if isinstance(exc, openai.PermissionDeniedError):
        return LLMError("API Key 没有权限访问该模型。", detail, "auth")
    if isinstance(exc, openai.RateLimitError):
        return LLMError("AI 服务限流或额度不足，请稍后重试。", detail, "rate_limit")
    if isinstance(exc, openai.NotFoundError):
        return LLMError("模型不存在或 Base URL 路径错误。", detail, "not_found")
    if isinstance(exc, openai.BadRequestError):
        return LLMError("请求被 AI 服务拒绝（可能是上下文过长或参数不支持）。", detail, "bad_request")
    if isinstance(exc, openai.APIStatusError):
        return LLMError(f"AI 服务返回错误（HTTP {exc.status_code}）。", detail, "status")
    return LLMError("AI 服务出现未知错误。", detail)


class LLMService:
    def __init__(self, proxy_url: str = "") -> None:
        self._proxy_url = proxy_url or None
        self._clients: dict[tuple[str, str], AsyncOpenAI] = {}

    def _client(self, base_url: str, api_key: str) -> AsyncOpenAI:
        key = (normalize_base_url(base_url), api_key)
        client = self._clients.get(key)
        if client is None:
            http_client = openai.DefaultAsyncHttpxClient(proxy=self._proxy_url) if self._proxy_url else None
            client = AsyncOpenAI(base_url=key[0], api_key=key[1], http_client=http_client, max_retries=1)
            self._clients[key] = client
        return client

    async def chat(self, active: ActiveModel, messages: list[dict], settings: BotSettings) -> AsyncIterator[str]:
        """Yield response text pieces; a single piece when streaming is off."""
        params: dict = {
            "model": active.model,
            "messages": messages,
            "temperature": settings.temperature,
            "timeout": settings.request_timeout_s,
        }
        if settings.max_tokens:
            params["max_tokens"] = settings.max_tokens
        client = self._client(active.base_url, active.api_key)
        try:
            if not settings.stream:
                completion = await client.chat.completions.create(**params)
                yield completion.choices[0].message.content or ""
                return
            stream = await client.chat.completions.create(**params, stream=True)
            async for chunk in stream:
                # Some gateways send keep-alive or usage-only chunks without choices.
                if chunk.choices and chunk.choices[0].delta.content:
                    yield chunk.choices[0].delta.content
        except openai.OpenAIError as exc:
            err = _translate(exc)
            log.warning("LLM request failed (%s/%s): %s", active.provider_name, active.model, err.detail)
            raise err from exc

    async def list_models(self, base_url: str, api_key: str) -> list[str]:
        """Model ids from `GET /models`, sorted. Raises LLMError."""
        try:
            page = await self._client(base_url, api_key).models.list(timeout=30)
            return sorted({m.id async for m in page})
        except openai.OpenAIError as exc:
            raise _translate(exc) from exc

    async def probe(self, base_url: str, api_key: str, model: str) -> None:
        """Smallest possible chat request, to check a model actually answers. Raises LLMError."""
        try:
            await self._client(base_url, api_key).chat.completions.create(
                model=model, messages=[{"role": "user", "content": "hi"}], max_tokens=16, timeout=60
            )
        except openai.OpenAIError as exc:
            raise _translate(exc) from exc

    async def close(self) -> None:
        for client in self._clients.values():
            await client.close()
        self._clients.clear()
