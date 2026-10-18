import asyncio
from collections.abc import AsyncGenerator
from typing import Any

import anyio
import httpx
import pytest
from openai import APIError, AsyncOpenAI, OpenAI
from starlette.testclient import TestClient

from digital_souls_core.api import create_app
from digital_souls_core.application import CoreError, Inference
from digital_souls_core.provider import LiteLLMProvider, litellm

from .support import FakeProvider, character, completion

pytestmark = pytest.mark.it1


async def test_litellm_actual_sdk_with_mock_http(monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=completion("miori-test-model"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as transport:
        sdk = AsyncOpenAI(api_key="synthetic-not-a-credential", http_client=transport)
        original = litellm.acompletion

        async def call(**kwargs: Any) -> Any:
            assert kwargs["model"] == "openai/miori-test-model"
            assert kwargs["drop_params"] is False and kwargs["fallbacks"] == []
            assert kwargs["num_retries"] == kwargs["max_retries"] == 0
            return await original(**kwargs, client=sdk)

        monkeypatch.setattr(litellm, "acompletion", call)
        result = await LiteLLMProvider().complete(
            character().config.profile,
            {"messages": [{"role": "user", "content": "synthetic input"}], "stream": False},
        )
    assert result["choices"][0]["message"]["content"] == "こんにちは"
    assert result["usage"]["total_tokens"] == 8
    assert len(requests) == 1
    assert b'"model":"miori-test-model"' in requests[0].content


@pytest.mark.parametrize(
    "kind,status,code",
    [
        ("rate", 429, "provider_rate_limit"),
        ("timeout", 504, "provider_timeout"),
        ("unsupported", 400, "unsupported_parameter"),
        ("other", 502, "provider_error"),
    ],
)
async def test_sdk_errors_are_sanitized(
    monkeypatch: pytest.MonkeyPatch, kind: str, status: int, code: str
) -> None:
    errors = {
        "rate": litellm.RateLimitError("SYNTHETIC_SECRET", "openai", "test"),
        "timeout": litellm.Timeout("SYNTHETIC_SECRET", "test", "openai"),
        "unsupported": litellm.UnsupportedParamsError("SYNTHETIC_SECRET", "openai", "test"),
        "other": RuntimeError("SYNTHETIC_SECRET"),
    }

    async def call(**kwargs: Any) -> Any:
        raise errors[kind]

    monkeypatch.setattr(litellm, "acompletion", call)
    provider = LiteLLMProvider()
    with pytest.raises(CoreError) as info:
        await provider.complete(character().config.profile, {})
    assert info.value.status == status and info.value.code == code
    assert "SYNTHETIC_SECRET" not in str(info.value)
    with pytest.raises(CoreError):
        await anext(provider.stream(character(native=True).config.profile, {"stream": True}))


async def test_cancellation_closes_sdk_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    entered = asyncio.Event()
    closed = asyncio.Event()

    class Stream:
        def __aiter__(self) -> AsyncGenerator[Any]:
            return self.events()

        async def events(self) -> AsyncGenerator[Any]:
            entered.set()
            await asyncio.Event().wait()
            yield None

        async def close(self) -> None:
            closed.set()

    async def call(**kwargs: Any) -> Any:
        return Stream()

    monkeypatch.setattr(litellm, "acompletion", call)
    iterator = LiteLLMProvider().stream(character(native=True).config.profile, {"stream": True})
    task = asyncio.create_task(anext(iterator))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed.is_set()


async def test_anyio_disconnect_scope_allows_async_close(monkeypatch: pytest.MonkeyPatch) -> None:
    closed = False

    class Stream:
        def __aiter__(self) -> AsyncGenerator[Any]:
            return self.events()

        async def events(self) -> AsyncGenerator[Any]:
            await anyio.sleep_forever()
            yield None

        async def close(self) -> None:
            nonlocal closed
            await anyio.lowlevel.checkpoint()
            closed = True

    async def call(**kwargs: Any) -> Any:
        return Stream()

    monkeypatch.setattr(litellm, "acompletion", call)
    with anyio.CancelScope() as scope:
        scope.cancel()
        await anext(
            LiteLLMProvider().stream(character(native=True).config.profile, {"stream": True})
        )
    assert closed


def test_openai_client_normal_usage_and_stream_error_is_not_success() -> None:
    fake = FakeProvider()
    server = TestClient(create_app(Inference((character(),), fake)), base_url="http://127.0.0.1")
    with OpenAI(api_key="synthetic", base_url="http://127.0.0.1/v1", http_client=server) as client:
        response = client.chat.completions.create(
            model="miori-alias", messages=[{"role": "user", "content": "hi"}]
        )
        assert response.usage is not None and response.usage.total_tokens == 8
        fake.mid_error = CoreError(502, "provider_error", "safe")
        stream = client.chat.completions.create(
            model="miori-alias", messages=[{"role": "user", "content": "hi"}], stream=True
        )
        with pytest.raises(APIError):
            list(stream)
        assert fake.closed
