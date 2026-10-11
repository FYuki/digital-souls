import asyncio
import json
from typing import Any

import httpx
import pytest
from openai import AsyncOpenAI
from pydantic import ValidationError

from digital_souls_core.api import create_app
from digital_souls_core.application import CoreError, Inference
from digital_souls_core.character import Character, Profile
from digital_souls_core.provider import LiteLLMProvider, litellm

from .support import CALL, TOOL, character, chunk, completion
from .test_sdk_stream_ownership import TrackedBytes

pytestmark = pytest.mark.it1


def local_character() -> Character:
    sample = character()
    profile = Profile(
        profile_id="local-llamacpp",
        model="openai/gemma4-12b",
        transport="llamacpp_chat",
        api_base="http://127.0.0.1:18081/v1",
        external_send_allowed=True,
        allowed_parameters=sample.config.profile.allowed_parameters,
    )
    return Character(
        sample.config.model_copy(update={"profile": profile}),
        sample.card,
        card_sha256=sample.card_sha256,
    )


@pytest.mark.parametrize(
    "base",
    [
        None,
        "https://example.com/v1",
        "http://localhost:18081/v1",
        "http://127.0.0.1/v1",
        "http://127.0.0.1:0/v1",
        "http://user:secret@127.0.0.1:18081/v1",
        "http://127.0.0.1:18081/v1?token=x",
        "http://127.0.0.1:18081/v1#x",
        "http://127.0.0.1:18081/other",
    ],
)
def test_local_profile_rejects_unverified_endpoint(base: str | None) -> None:
    with pytest.raises(ValidationError):
        Profile(
            profile_id="local", model="openai/gemma4-12b", transport="llamacpp_chat", api_base=base
        )


@pytest.mark.parametrize(
    "model", ["openai/gpt-5.4", "openai/responses/gemma4-12b", "ollama_chat/gemma4:12b"]
)
def test_local_profile_rejects_other_model_routes(model: str) -> None:
    with pytest.raises(ValidationError):
        Profile(
            profile_id="local",
            model=model,
            transport="llamacpp_chat",
            api_base="http://127.0.0.1:18081/v1",
        )


async def test_operator_endpoint_and_dummy_auth_with_unmodified_tool_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[dict[str, Any]] = []
    monkeypatch.setenv("OPENAI_API_KEY", "SYNTHETIC_CLOUD_SECRET")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://synthetic.invalid/v1")
    monkeypatch.setattr(litellm, "api_base", "https://synthetic.invalid/v1")

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "http://127.0.0.1:18081/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer local-no-auth"
        body = json.loads(request.content)
        sent.append(body)
        assert body["model"] == "gemma4-12b" and body["tools"] == [TOOL]
        assert body["tool_choice"] == "auto"
        return httpx.Response(200, json=completion("gemma4-12b", tool=len(sent) == 1))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        original = litellm.acompletion

        async def call(**kwargs: Any) -> Any:
            kwargs.pop("client", None)
            assert kwargs["api_key"] == "local-no-auth"
            assert kwargs["api_base"] == "http://127.0.0.1:18081/v1"
            sdk = AsyncOpenAI(
                api_key=kwargs["api_key"], base_url=kwargs["api_base"], http_client=http
            )
            return await original(**kwargs, client=sdk)

        monkeypatch.setattr(litellm, "acompletion", call)
        app = create_app(Inference((local_character(),), LiteLLMProvider()))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client:
            messages: list[dict[str, Any]] = [{"role": "user", "content": "synthetic"}]
            payload = {
                "model": "miori-alias",
                "messages": messages,
                "tools": [TOOL],
                "tool_choice": "auto",
            }
            first = await client.post("/v1/chat/completions", json=payload)
            assert first.status_code == 200
            returned = first.json()["choices"][0]["message"]
            messages.extend(
                [
                    returned,
                    {"role": "tool", "tool_call_id": CALL["id"], "content": "synthetic-result"},
                ]
            )
            second = await client.post("/v1/chat/completions", json=payload)
            assert second.status_code == 200
            denied = await client.post(
                "/v1/chat/completions", json=payload | {"api_base": "https://synthetic.invalid"}
            )
            assert denied.status_code == 400
    assert len(sent) == 2 and sent[1]["messages"][-2] == returned
    assert sent[1]["messages"][-1]["tool_call_id"] == CALL["id"]


@pytest.mark.parametrize("mode", ["eof", "close", "cancel", "timeout"])
async def test_verified_local_sdk_stream_ownership(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    events = [("data: " + json.dumps(chunk({"content": "synthetic"})) + "\n\n").encode()]
    if mode == "eof":
        events.append(b"data: [DONE]\n\n")
    wire = TrackedBytes(
        events,
        block=mode in {"close", "cancel"},
        error=httpx.ReadTimeout("SYNTHETIC_SECRET") if mode == "timeout" else None,
    )
    response = httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=wire)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: response)) as http:
        sdk = AsyncOpenAI(
            api_key="local-no-auth", base_url="http://127.0.0.1:18081/v1", http_client=http
        )
        original = litellm.acompletion

        async def call(**kwargs: Any) -> Any:
            kwargs.pop("client", None)
            return await original(**kwargs, client=sdk)

        monkeypatch.setattr(litellm, "acompletion", call)
        iterator = LiteLLMProvider().stream(
            local_character().config.profile,
            {
                "messages": [{"role": "user", "content": "synthetic"}],
                "stream": True,
                "tools": [TOOL],
            },
        )
        assert (await anext(iterator))["choices"][0]["delta"]["content"] == "synthetic"
        if mode == "eof":
            _ = [item async for item in iterator]
        elif mode == "close":
            await iterator.aclose()
        elif mode == "cancel":
            task = asyncio.create_task(anext(iterator))
            await asyncio.wait_for(wire.waiting.wait(), 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(CoreError) as error:
                await anext(iterator)
            assert error.value.code == "provider_timeout"
    assert response.is_closed and wire.closed


@pytest.mark.parametrize("stream", [False, True])
async def test_local_transport_still_denies_responses_bridge(
    monkeypatch: pytest.MonkeyPatch, stream: bool
) -> None:
    monkeypatch.setattr(litellm, "route_all_chat_openai_to_responses", True)

    async def forbidden(**kwargs: Any) -> Any:
        pytest.fail("Responses bridge must never acquire transport")

    monkeypatch.setattr(litellm, "acompletion", forbidden)
    with pytest.raises(CoreError):
        await LiteLLMProvider._call(local_character().config.profile, {"stream": stream})
