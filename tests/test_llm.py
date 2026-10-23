import json

import httpx
import openai
import pytest

from bot.db.repo import ActiveModel
from bot.services.llm import LLMError, LLMService
from bot.services.settings import BotSettings

ACTIVE = ActiveModel(
    model_id=1, model="m", provider_id=1, provider_name="p", base_url="https://api.test/v1", api_key="sk-test"
)


def service_with(handler) -> tuple[LLMService, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    svc = LLMService()
    client = openai.AsyncOpenAI(
        base_url=ACTIVE.base_url,
        api_key=ACTIVE.api_key,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(record)),
        max_retries=0,
    )
    svc._clients[(ACTIVE.base_url, ACTIVE.api_key)] = client
    return svc, seen


def sse(*chunks: dict) -> bytes:
    lines = [f"data: {json.dumps(c)}\n\n" for c in chunks] + ["data: [DONE]\n\n"]
    return "".join(lines).encode()


def delta(content: str | None) -> dict:
    return {
        "id": "c",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "m",
        "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}],
    }


async def collect(svc: LLMService, settings: BotSettings) -> str:
    return "".join([p async for p in svc.chat(ACTIVE, [{"role": "user", "content": "hi"}], settings)])


async def test_streaming_concatenates_deltas_and_skips_empty_chunks():
    usage_only = {"id": "c", "object": "chat.completion.chunk", "created": 0, "model": "m", "choices": []}
    body = sse(delta("Hel"), delta(None), usage_only, delta("lo"))
    svc, seen = service_with(lambda r: httpx.Response(200, content=body, headers={"content-type": "text/event-stream"}))
    assert await collect(svc, BotSettings(temperature=0.3, max_tokens=50)) == "Hello"

    req = seen[0]
    assert str(req.url) == "https://api.test/v1/chat/completions"
    assert req.headers["authorization"] == "Bearer sk-test"
    payload = json.loads(req.content)
    assert payload["stream"] is True and payload["model"] == "m"
    assert payload["temperature"] == 0.3 and payload["max_tokens"] == 50


async def test_non_streaming_mode():
    completion = {
        "id": "c",
        "object": "chat.completion",
        "created": 0,
        "model": "m",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "full"}, "finish_reason": "stop"}],
    }
    svc, seen = service_with(lambda r: httpx.Response(200, json=completion))
    assert await collect(svc, BotSettings(stream=False)) == "full"
    assert "max_tokens" not in json.loads(seen[0].content)


@pytest.mark.parametrize(
    ("status", "fragment"),
    [(401, "API Key 无效"), (404, "模型不存在"), (429, "限流"), (500, "HTTP 500")],
)
async def test_errors_are_translated(status, fragment):
    svc, _ = service_with(lambda r: httpx.Response(status, json={"error": {"message": "boom sk-leakedkey123"}}))
    with pytest.raises(LLMError) as info:
        await collect(svc, BotSettings())
    assert fragment in info.value.user_message
    assert "sk-leakedkey123" not in info.value.detail


async def test_list_models_sorted_and_deduplicated():
    data = [{"id": i, "object": "model", "created": 0, "owned_by": "x"} for i in ["b", "c", "a", "a"]]
    body = {"object": "list", "data": data}
    svc, seen = service_with(lambda r: httpx.Response(200, json=body))
    assert await svc.list_models(ACTIVE.base_url, ACTIVE.api_key) == ["a", "b", "c"]
    assert str(seen[0].url) == "https://api.test/v1/models"


@pytest.mark.parametrize(("status", "kind"), [(401, "auth"), (403, "auth"), (404, "not_found"), (502, "status")])
async def test_list_models_error_kinds(status, kind):
    svc, _ = service_with(lambda r: httpx.Response(status, json={"error": {"message": "x"}}))
    with pytest.raises(LLMError) as info:
        await svc.list_models(ACTIVE.base_url, ACTIVE.api_key)
    assert info.value.kind == kind


async def test_probe_sends_tiny_request():
    completion = {
        "id": "c",
        "object": "chat.completion",
        "created": 0,
        "model": "m",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}],
    }
    svc, seen = service_with(lambda r: httpx.Response(200, json=completion))
    await svc.probe(ACTIVE.base_url, ACTIVE.api_key, "some-model")
    payload = json.loads(seen[0].content)
    assert payload["model"] == "some-model" and payload["max_tokens"] == 16 and not payload.get("stream")
