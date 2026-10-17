import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from openai import AsyncOpenAI

from digital_souls_core.api import create_app
from digital_souls_core.application import CoreError, Inference
from digital_souls_core.provider import LiteLLMProvider, litellm, provider_error

from .support import TOOL, character, chunk

pytestmark = pytest.mark.it1


class TrackedBytes(httpx.AsyncByteStream):
    def __init__(
        self, events: list[bytes], *, block: bool = False, error: Exception | None = None
    ) -> None:
        self.events = events
        self.block = block
        self.waiting = asyncio.Event()
        self.closed = False
        self.error = error

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for event in self.events:
            yield event
        if self.error is not None:
            raise self.error
        if self.block:
            self.waiting.set()
            await asyncio.Event().wait()

    async def aclose(self) -> None:
        await asyncio.sleep(0)
        self.closed = True


@pytest.mark.parametrize("mode", ["eof", "consumer_close", "cancel"])
async def test_real_litellm_openai_sdk_owns_and_closes_http_response(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    values = [
        chunk(
            {
                "role": "assistant",
                "content": "first",
                "reasoning_content": "SYNTHETIC_PRIVATE_REASONING",
            }
        )
    ]
    events = [("data: " + json.dumps(value) + "\n\n").encode() for value in values]
    if mode == "eof":
        events.extend(
            [("data: " + json.dumps(chunk({}, "stop")) + "\n\n").encode(), b"data: [DONE]\n\n"]
        )
    wire = TrackedBytes(events, block=mode != "eof")
    responses: list[httpx.Response] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/chat/completions"
        assert json.loads(request.content)["tools"] == [TOOL]
        response = httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=wire)
        responses.append(response)
        return response

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        sdk = AsyncOpenAI(api_key="synthetic", http_client=http)
        original = litellm.acompletion
        wrappers: list[Any] = []

        async def call(**kwargs: Any) -> Any:
            result = await original(**kwargs, client=sdk)
            wrappers.append(result)
            return result

        monkeypatch.setattr(litellm, "acompletion", call)
        stream = LiteLLMProvider().stream(
            character(native=True).config.profile,
            {
                "messages": [{"role": "user", "content": "synthetic"}],
                "stream": True,
                "tools": [TOOL],
            },
        )
        first = await anext(stream)
        assert first["choices"][0]["delta"]["content"] == "first"
        assert "SYNTHETIC_PRIVATE_REASONING" not in json.dumps(first)
        assert "reasoning_content" not in first["choices"][0]["delta"]
        assert wrappers[0].completion_stream.response is responses[0]
        assert not responses[0].is_closed
        if mode == "eof":
            rest = [event async for event in stream]
            assert any(event["choices"][0].get("finish_reason") == "stop" for event in rest)
        elif mode == "consumer_close":
            await stream.aclose()
        else:
            task = asyncio.create_task(anext(stream))
            await asyncio.wait_for(wire.waiting.wait(), 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert responses[0].is_closed
        assert wire.closed


async def test_pinned_anthropic_iterator_does_not_own_http_response_close() -> None:
    from litellm.llms.anthropic.chat.handler import ModelResponseIterator

    wire = TrackedBytes([b"event: message_start\n\n"], block=True)
    response = httpx.Response(200, stream=wire)
    iterator = ModelResponseIterator(streaming_response=response.aiter_lines(), sync_stream=False)
    try:
        await anext(aiter(iterator))
        assert not hasattr(iterator, "aclose") and not hasattr(iterator, "close")
        # Closing the exposed line generator alone still does not close Response.
        await iterator.streaming_response.aclose()
        assert not response.is_closed and not wire.closed
    finally:
        await response.aclose()
    assert wire.closed


@pytest.mark.parametrize(
    "provider", ["anthropic", "bedrock", "openrouter", "ollama", "ollama_chat"]
)
async def test_unverified_stream_adapter_rejected_before_any_sdk_call(
    monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    async def forbidden(**kwargs: Any) -> Any:
        pytest.fail("unsupported transport must be rejected before network acquisition")

    monkeypatch.setattr(litellm, "acompletion", forbidden)
    profile = character().config.profile.model_copy(update={"model": f"{provider}/synthetic"})
    with pytest.raises(CoreError) as info:
        await anext(LiteLLMProvider().stream(profile, {"stream": True}))
    assert info.value.status == 400
    assert info.value.code == "unsupported_stream_provider"


@pytest.mark.parametrize("after_first", [False, True])
async def test_real_sdk_read_timeout_before_and_after_first_chunk_is_safe(
    monkeypatch: pytest.MonkeyPatch, after_first: bool
) -> None:
    events = (
        [("data: " + json.dumps(chunk({"content": "first"})) + "\n\n").encode()]
        if after_first
        else []
    )
    wire = TrackedBytes(events, error=httpx.ReadTimeout("SYNTHETIC_SECRET"))
    response = httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=wire)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: response)) as http:
        sdk = AsyncOpenAI(api_key="synthetic", http_client=http)
        original = litellm.acompletion

        async def call(**kwargs: Any) -> Any:
            return await original(**kwargs, client=sdk)

        monkeypatch.setattr(litellm, "acompletion", call)
        app = create_app(Inference((character(native=True),), LiteLLMProvider()))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client:
            result = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "miori-alias",
                    "messages": [{"role": "user", "content": "synthetic"}],
                    "stream": True,
                },
            )
    assert result.status_code == (200 if after_first else 504)
    assert "provider_timeout" in result.text
    assert "SYNTHETIC_SECRET" not in result.text
    assert "[DONE]" not in result.text
    if after_first:
        assert "event: error" in result.text and "first" in result.text
    assert response.is_closed and wire.closed


@pytest.mark.parametrize(
    "inner,code,status",
    [
        (httpx.ReadTimeout("SYNTHETIC_SECRET"), "provider_timeout", 504),
        (
            litellm.RateLimitError("SYNTHETIC_SECRET", "openai", "synthetic"),
            "provider_rate_limit",
            429,
        ),
        (ValueError("SYNTHETIC_SECRET"), "provider_error", 502),
    ],
)
def test_pinned_midstream_wrapper_classifies_nested_error_without_exposing_text(
    inner: Exception, code: str, status: int
) -> None:
    from litellm.exceptions import MidStreamFallbackError

    wrapped = MidStreamFallbackError(
        message="SYNTHETIC_SECRET",
        model="synthetic",
        llm_provider="openai",
        original_exception=inner,
        generated_content="SYNTHETIC_PRIVATE_CONTENT",
    )
    mapped = provider_error(wrapped)
    assert mapped.code == code and mapped.status == status
    assert "SYNTHETIC" not in mapped.message


@pytest.mark.parametrize(
    "name", ["gpt-5-codex", "o3-pro", "codex-mini-latest", "responses/gpt-4o-mini"]
)
async def test_pinned_responses_routes_are_denied_before_sdk_acquisition(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    from litellm.main import responses_api_bridge_check

    info, _ = responses_api_bridge_check(model=name, custom_llm_provider="openai")
    assert info["mode"] == "responses"

    async def forbidden(**kwargs: Any) -> Any:
        pytest.fail("Responses bridge must not acquire an upstream stream")

    monkeypatch.setattr(litellm, "acompletion", forbidden)
    profile = character(native=True).config.profile.model_copy(update={"model": f"openai/{name}"})
    with pytest.raises(CoreError) as error:
        await anext(LiteLLMProvider().stream(profile, {"stream": True}))
    assert error.value.status == 400 and error.value.code == "unsupported_stream_provider"


async def test_unknown_route_is_denied_before_sdk_acquisition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def forbidden(**kwargs: Any) -> Any:
        pytest.fail("Unknown route must fail closed")

    monkeypatch.setattr(litellm, "acompletion", forbidden)
    with pytest.raises(CoreError) as error:
        await anext(LiteLLMProvider().stream(character().config.profile, {"stream": True}))
    assert error.value.code == "unsupported_stream_provider"


@pytest.mark.parametrize("key", ["gpt-4o-mini-2024-07-18", "openai/gpt-4o-mini-2024-07-18"])
async def test_sdk_alias_cannot_redirect_verified_native_route(
    monkeypatch: pytest.MonkeyPatch, key: str
) -> None:
    monkeypatch.setattr(litellm, "model_alias_map", {key: "openai/gpt-5-codex"})

    async def forbidden(**kwargs: Any) -> Any:
        pytest.fail("SDK alias must not change the verified route")

    monkeypatch.setattr(litellm, "acompletion", forbidden)
    with pytest.raises(CoreError) as error:
        await anext(
            LiteLLMProvider().stream(character(native=True).config.profile, {"stream": True})
        )
    assert error.value.code == "unsupported_stream_provider"
