import json
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from digital_souls_core.api import create_app
from digital_souls_core.application import CoreError, Inference
from digital_souls_core.character import Character, Profile
from digital_souls_core.provider import LiteLLMProvider, litellm

from .support import TOOL, character

pytestmark = pytest.mark.it1


def ollama_character(think: bool | None = False) -> Character:
    sample = character()
    profile = Profile.model_validate(
        sample.config.profile.model_dump(exclude_unset=True)
        | {"model": "ollama_chat/gemma4:12b", "ollama_think": think}
    )
    return Character(
        sample.config.model_copy(update={"profile": profile}),
        sample.card,
        card_sha256=sample.card_sha256,
    )


def native_response(message: dict[str, Any], reason: str = "stop") -> dict[str, Any]:
    return {
        "model": "gemma4:12b",
        "created_at": "2026-10-03T00:00:00Z",
        "message": {"role": "assistant", **message},
        "done": True,
        "done_reason": reason,
        "prompt_eval_count": 10,
        "eval_count": 4,
    }


async def install_transport(monkeypatch: pytest.MonkeyPatch, http: httpx.AsyncClient) -> None:
    from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler

    sdk = AsyncHTTPHandler()
    await sdk.client.aclose()
    sdk.client = http
    original = litellm.acompletion

    async def call(**kwargs: Any) -> Any:
        assert kwargs["drop_params"] is False and kwargs["fallbacks"] == []
        assert kwargs["num_retries"] == kwargs["max_retries"] == 0
        return await original(**kwargs, client=sdk)

    monkeypatch.setattr(litellm, "acompletion", call)
    monkeypatch.setattr(litellm, "add_function_to_prompt", False)


@pytest.mark.parametrize("think", [False, True, None])
async def test_native_tools_roundtrip_keeps_ids_arguments_and_result(
    monkeypatch: pytest.MonkeyPatch,
    think: bool | None,
) -> None:
    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/chat"
        body = json.loads(request.content)
        sent.append(body)
        assert body["model"] == "gemma4:12b" and body.get("think") is think
        assert "SYNTHETIC_PRIVATE_REASONING" not in request.content.decode()
        assert body["tools"] == [TOOL]
        assert "format" not in body
        assert body["options"]["num_predict"] == 128
        assert body["messages"][0]["role"] == "system"
        if len(sent) == 1:
            message = {
                "content": "",
                "thinking": "SYNTHETIC_PRIVATE_REASONING",
                "tool_calls": [{"function": {"name": "weather", "arguments": {"city": "Tokyo"}}}],
            }
        else:
            message = {"content": "SYNTHETIC_RESULT"}
        return httpx.Response(200, json=native_response(message))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        await install_transport(monkeypatch, http)
        app = create_app(Inference((ollama_character(think),), LiteLLMProvider()))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client:
            messages: list[dict[str, Any]] = [
                {"role": "user", "content": "Synthetic weather request"}
            ]
            payload = {
                "model": "miori-alias",
                "messages": messages,
                "tools": [TOOL],
                "max_tokens": 128,
            }
            first = await client.post("/v1/chat/completions", json=payload)
            assert first.status_code == 200
            assert "SYNTHETIC_PRIVATE_REASONING" not in first.text
            assert "reasoning_content" not in first.text
            choice = first.json()["choices"][0]
            assert choice["finish_reason"] == "tool_calls"
            call = choice["message"]["tool_calls"][0]
            assert call["id"] and json.loads(call["function"]["arguments"]) == {"city": "Tokyo"}
            messages.extend(
                [
                    choice["message"],
                    {"role": "tool", "tool_call_id": call["id"], "content": "SYNTHETIC_RESULT"},
                ]
            )
            second = await client.post("/v1/chat/completions", json=payload)
            assert second.status_code == 200
            assert second.json()["choices"][0]["message"]["content"] == "SYNTHETIC_RESULT"
    assert len(sent) == 2
    assert sent[1]["messages"][-1]["tool_call_id"] == call["id"]
    assert sent[1]["messages"][-1]["content"] == "SYNTHETIC_RESULT"
    assert sent[1]["messages"][-2]["tool_calls"][0]["function"]["arguments"] == {"city": "Tokyo"}
    assert litellm.add_function_to_prompt is False


@pytest.mark.parametrize("think", [False, True, None])
async def test_operator_thinking_and_length_are_preserved(
    monkeypatch: pytest.MonkeyPatch, think: bool | None
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/chat"
        body = json.loads(request.content)
        assert (body.get("think") is think) and (("think" in body) == (think is not None))
        return httpx.Response(
            200, json=native_response({"content": "", "thinking": "synthetic reasoning"}, "length")
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        await install_transport(monkeypatch, http)
        result = await LiteLLMProvider().complete(
            ollama_character(think).config.profile,
            {"messages": [{"role": "user", "content": "synthetic"}], "max_completion_tokens": 4},
        )
    assert result["choices"][0]["finish_reason"] == "length"
    assert result["choices"][0]["message"]["content"] in (None, "")
    assert "synthetic reasoning" not in json.dumps(result)
    from digital_souls_core.contracts import Message

    Message.model_validate(result["choices"][0]["message"])


@pytest.mark.parametrize(
    "choice", ["auto", "none", "required", {"type": "function", "function": {"name": "weather"}}]
)
async def test_tool_choice_is_rejected_before_sdk(
    monkeypatch: pytest.MonkeyPatch, choice: Any
) -> None:
    async def forbidden(**kwargs: Any) -> Any:
        pytest.fail("Unsupported tool_choice must not reach SDK")

    monkeypatch.setattr(litellm, "acompletion", forbidden)
    with pytest.raises(CoreError) as info:
        await LiteLLMProvider().complete(
            ollama_character().config.profile, {"tool_choice": choice, "tools": [TOOL]}
        )
    assert info.value.status == 400 and info.value.code == "unsupported_parameter"


@pytest.mark.parametrize("value", ["false", 0, 1])
def test_operator_think_requires_boolean(value: Any) -> None:
    with pytest.raises(ValidationError):
        Profile(profile_id="test", model="ollama_chat/synthetic", ollama_think=value)


@pytest.mark.parametrize("model", ["openai/synthetic", "anthropic/synthetic", "ollama/synthetic"])
@pytest.mark.parametrize("think", [False, True, None])
def test_operator_think_requires_native_ollama_route(model: str, think: bool | None) -> None:
    with pytest.raises(ValidationError):
        Profile(profile_id="test", model=model, ollama_think=think)


@pytest.mark.parametrize(
    "model",
    ["openai/synthetic", "anthropic/synthetic", "ollama/synthetic", "ollama_chat/synthetic"],
)
def test_omitted_operator_think_is_allowed(model: str) -> None:
    profile = Profile(profile_id="test", model=model)
    assert profile.ollama_think is None and "ollama_think" not in profile.model_fields_set


async def test_legacy_ollama_route_is_rejected_before_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    async def forbidden(**kwargs: Any) -> Any:
        pytest.fail("Legacy Ollama adapter must not reach SDK")

    monkeypatch.setattr(litellm, "acompletion", forbidden)
    profile = Profile(profile_id="test", model="ollama/synthetic", external_send_allowed=True)
    with pytest.raises(CoreError) as info:
        await LiteLLMProvider().complete(profile, {})
    assert info.value.code == "unsupported_provider_route"


@pytest.mark.parametrize("field", ["think", "ollama_think", "reasoning_effort", "profile"])
async def test_caller_cannot_override_operator_thinking(field: str) -> None:
    from .support import FakeProvider

    fake = FakeProvider()
    app = create_app(Inference((ollama_character(),), fake))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
    ) as client:
        result = await client.post(
            "/v1/character/completions",
            json={
                "character_id": "miori",
                "messages": [{"role": "user", "content": "synthetic"}],
                field: False,
            },
        )
    assert result.status_code == 400 and not fake.calls


async def test_native_server_failure_does_not_retry_or_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        assert "format" not in json.loads(request.content)
        return httpx.Response(400, json={"error": "SYNTHETIC_SECRET unsupported tools"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        await install_transport(monkeypatch, http)
        with pytest.raises(CoreError) as info:
            await LiteLLMProvider().complete(
                ollama_character().config.profile,
                {"messages": [{"role": "user", "content": "synthetic"}], "tools": [TOOL]},
            )
    assert info.value.status == 502 and "SYNTHETIC" not in info.value.message
    assert paths == ["/api/chat"] and litellm.add_function_to_prompt is False
