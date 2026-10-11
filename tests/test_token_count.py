"""Synthetic token endpoint contracts; no real model or external sockets."""

import asyncio
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from digital_souls_core.application import CoreError
from digital_souls_core.character import Profile
from digital_souls_core.contracts import Message, Tool
from digital_souls_core.prompt_builder import PromptBuilder
from digital_souls_core.token_count import LlamaCppTokenCounter

from .support import TOOL
from .test_prompt_builder import UnitCounter, budgets, sample


@pytest.mark.ut
@pytest.mark.parametrize("value", ["llamacpp", "unknown", None, True])
def test_sdk_rejects_non_estimate_counter(value: object) -> None:
    with pytest.raises(ValidationError):
        Profile.model_validate(
            {"profile_id": "test", "model": "openai/test", "token_counter": value}
        )


@pytest.mark.ut
def test_counter_defaults_and_explicit_local_choice() -> None:
    assert Profile(profile_id="test", model="openai/test").token_counter == "estimate"
    assert (
        Profile(
            profile_id="test",
            model="openai/gemma4-12b",
            transport="llamacpp_chat",
            api_base="http://127.0.0.1:18081/v1",
            token_counter="llamacpp",
        ).token_counter
        == "llamacpp"
    )


@pytest.mark.ut
@pytest.mark.parametrize("total", [21, 19, 17, 14, 11, 9, 6, 4])
async def test_async_counter_matches_sync_reductions_and_caches(total: int) -> None:
    calls: list[str] = []

    async def counter(messages: Sequence[Message], *, tools: Sequence[Tool] = ()) -> int:
        await asyncio.sleep(0)
        key = json.dumps([m.model_dump() for m in messages])
        assert key not in calls
        calls.append(key)
        return UnitCounter()(messages, tools=tools)

    expected = await PromptBuilder(UnitCounter()).build(sample(), budgets(total))
    builder = PromptBuilder(counter)
    assert await builder.build(sample(), budgets(total)) == expected
    first_calls = len(calls)
    calls.clear()
    assert await builder.build(sample(), budgets(total)) == expected
    assert len(calls) == first_calls  # cache lifetime is one build


@pytest.mark.it1
async def test_template_tokenize_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = json.loads(request.content)
        if request.url.path == "/apply-template":
            assert body == {
                "messages": [{"role": "user", "content": "synthetic"}],
                "tools": [TOOL],
                "tool_choice": "required",
            }
            return httpx.Response(200, json={"prompt": "<bos>synthetic<assistant>"})
        assert request.url.path == "/tokenize"
        assert body == {
            "content": "<bos>synthetic<assistant>",
            "add_special": True,
            "parse_special": True,
            "with_pieces": False,
        }
        return httpx.Response(200, json={"tokens": [1, 2, 3]})

    original = httpx.AsyncClient

    def client(**kwargs: Any) -> httpx.AsyncClient:
        assert kwargs["trust_env"] is False and kwargs["follow_redirects"] is False
        return original(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    monkeypatch.setenv("HTTP_PROXY", "http://synthetic.invalid:1")
    monkeypatch.setenv("HTTPS_PROXY", "http://synthetic.invalid:1")
    monkeypatch.setenv("ALL_PROXY", "http://synthetic.invalid:1")
    counter = LlamaCppTokenCounter("http://127.0.0.1:18081/v1", 1, tool_choice="required")
    assert (
        await counter(
            (Message(role="user", content="synthetic"),), tools=(Tool.model_validate(TOOL),)
        )
        == 3
    )
    assert len(requests) == 2


@pytest.mark.it1
@pytest.mark.parametrize("stage", ["/apply-template", "/tokenize"])
@pytest.mark.parametrize(
    "mode",
    ["connect", "timeout", "redirect", "http", "json", "shape", "negative", "bool", "cancel"],
)
async def test_counter_fails_closed_without_echo(
    monkeypatch: pytest.MonkeyPatch, stage: str, mode: str
) -> None:
    secret = "synthetic-private-body"
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path != stage:
            return httpx.Response(200, json={"prompt": secret})
        if mode == "connect":
            raise httpx.ConnectError(secret)
        if mode == "timeout":
            raise httpx.ReadTimeout(secret)
        if mode == "cancel":
            raise asyncio.CancelledError
        if mode == "redirect":
            return httpx.Response(
                307, headers={"location": "http://synthetic.invalid/"}, text=secret
            )
        if mode == "http":
            return httpx.Response(500, text=secret)
        if mode == "json":
            return httpx.Response(200, text=secret)
        response: object = {"prompt": 3} if stage == "/apply-template" else {"tokens": 3}
        if mode == "negative":
            response = {"tokens": [-1]}
        if mode == "bool":
            response = {"tokens": [True]}
        return httpx.Response(200, json=response)

    original = httpx.AsyncClient
    clients: list[httpx.AsyncClient] = []

    def client(**kwargs: Any) -> httpx.AsyncClient:
        result = original(transport=httpx.MockTransport(handler), **kwargs)
        clients.append(result)
        return result

    monkeypatch.setattr(httpx, "AsyncClient", client)
    counter = LlamaCppTokenCounter("http://127.0.0.1:18081/v1", 0.01)
    if mode == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await counter((Message(role="user", content=secret),))
    else:
        with pytest.raises(CoreError) as caught:
            await counter((Message(role="user", content=secret),))
        assert caught.value.status == (504 if mode == "timeout" else 502)
        assert caught.value.code == "token_count_failed"
        assert secret not in str(caught.value) and secret not in repr(vars(caught.value))
    assert all(client.is_closed for client in clients)
    assert calls == (
        ["/apply-template"] if stage == "/apply-template" else ["/apply-template", "/tokenize"]
    )


@pytest.mark.it1
@pytest.mark.parametrize("entry", ["chat", "history"])
def test_both_apis_use_server_counts_and_trim_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str
) -> None:
    from .fixture_server import create_token_fixture_app
    from .test_prompt_assembly_api import configured, http_setup, post, seeded_turns

    fixture = create_token_fixture_app()
    original = httpx.AsyncClient

    def client(**kwargs: Any) -> httpx.AsyncClient:
        return original(transport=httpx.ASGITransport(app=fixture), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    char = configured(tmp_path, prompt_budget={"history": 4})
    profile = Profile(
        profile_id="test",
        model="openai/gemma4-12b",
        transport="llamacpp_chat",
        api_base="http://127.0.0.1:18081/v1",
        token_counter="llamacpp",
        external_send_allowed=True,
        allowed_parameters=char.config.profile.allowed_parameters,
    )
    char = type(char)(
        char.config.model_copy(update={"profile": profile}), char.card, card_sha256=char.card_sha256
    )
    seeded = seeded_turns()
    http, provider, inference, store = http_setup(char, seeded)
    response = post(http, entry, seeded, [{"role": "user", "content": "now"}])
    assert response.status_code == 200
    assert inference.prepared.usage.tokens["history"] == 4
    assert inference.prepared.usage.dropped["history"] == 2
    assert provider.calls[0][1]["messages"][-6:-2] == [
        m.model_dump(exclude_none=True) for m in seeded[3:]
    ]
    assert len(fixture.state.templates) == len(fixture.state.tokenizations) > 0
    serialized = [json.dumps(body, sort_keys=True) for body in fixture.state.templates]
    assert len(serialized) == len(set(serialized))
    if entry == "history":
        store.append.assert_called_once()


@pytest.mark.it1
@pytest.mark.parametrize("entry", ["chat", "history"])
@pytest.mark.parametrize("mode", ["timeout", "malformed", "private_card", "revoked"])
def test_api_count_errors_never_dispatch_or_append(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str, mode: str
) -> None:
    from dataclasses import replace

    from .test_prompt_assembly_api import configured, http_setup, post, seeded_turns

    char = configured(tmp_path)
    profile = Profile(
        profile_id="test",
        model="openai/gemma4-12b",
        transport="llamacpp_chat",
        api_base="http://127.0.0.1:18081/v1",
        token_counter="llamacpp",
        external_send_allowed=True,
        allowed_parameters=char.config.profile.allowed_parameters,
    )
    char = replace(char, config=char.config.model_copy(update={"profile": profile}))
    if mode == "private_card":
        char = replace(
            char, card=replace(char.card, description="api_key=sk-synthetic-secret-value")
        )
    seeded = seeded_turns()
    http, provider, inference, store = http_setup(char, seeded)
    calls: list[str] = []
    original = httpx.AsyncClient

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if mode == "private_card":
            pytest.fail("Private assembled card must not reach tokenizer")
        if mode == "revoked":
            assert inference.privacy is not None
            inference.privacy.configure({})
            return httpx.Response(200, json={"prompt": "synthetic"})
        if mode == "timeout":
            raise httpx.ReadTimeout("synthetic-private-body")
        return httpx.Response(200, json={"prompt": 5})

    def client(**kwargs: Any) -> httpx.AsyncClient:
        return original(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    response = post(http, entry, seeded, [{"role": "user", "content": "now"}])
    assert (
        response.status_code
        == {"timeout": 504, "malformed": 502, "private_card": 403, "revoked": 403}[mode]
    )
    assert response.json()["error"]["code"] == (
        "token_count_failed"
        if mode in {"timeout", "malformed"}
        else "privacy_denied"
        if mode == "private_card"
        else "context_revoked"
    )
    assert not provider.calls and not store.append.called
    assert calls == ([] if mode == "private_card" else ["/apply-template"])


@pytest.mark.ut
async def test_cache_key_includes_tool_definitions() -> None:
    from digital_souls_core.prompt_builder import CharacterCard, PromptInput

    calls: list[tuple[int, int]] = []

    async def counter(messages: Sequence[Message], *, tools: Sequence[Tool] = ()) -> int:
        calls.append((len(messages), len(tools)))
        return len(messages) + len(tools)

    result = await PromptBuilder(counter).build(
        PromptInput(card=CharacterCard(description="persona"), tools=(Tool.model_validate(TOOL),)),
        budgets(),
    )
    assert result.usage.total_tokens == 2
    assert result.usage.tokens["character"] == 1
    assert result.usage.tokens["current_input"] == 1
    assert calls == [(1, 1), (1, 0), (0, 1)]


@pytest.mark.it1
async def test_pending_http_count_yields_loop_and_task_cancel_closes_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = asyncio.Event()
    clients: list[httpx.AsyncClient] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        started.set()
        await asyncio.Event().wait()
        raise AssertionError("Cancelled request cannot return")

    original = httpx.AsyncClient

    def client(**kwargs: Any) -> httpx.AsyncClient:
        result = original(transport=httpx.MockTransport(handler), **kwargs)
        clients.append(result)
        return result

    monkeypatch.setattr(httpx, "AsyncClient", client)
    task = asyncio.create_task(
        LlamaCppTokenCounter("http://127.0.0.1:18081/v1", 1)(
            (Message(role="user", content="synthetic"),)
        )
    )
    await asyncio.wait_for(started.wait(), 1)
    assert not task.done()  # the caller's event loop remains runnable during HTTP
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert clients[0].is_closed
