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


@pytest.mark.ut
async def test_region_envelope_costs_limits_and_cache() -> None:
    from digital_souls_core.prompt_builder import PromptBudgetExceeded, PromptInput, split_messages

    class StrictCounter:
        def __init__(self) -> None:
            self.calls: list[tuple[Message, ...]] = []

        @staticmethod
        def region_messages(messages: Sequence[Message]) -> tuple[Message, ...]:
            return (Message(role="user", content=""), *messages, Message(role="user", content=""))

        def __call__(self, messages: Sequence[Message], *, tools: Sequence[Tool] = ()) -> int:
            assert any(message.role == "user" for message in messages)
            assert tuple(messages) not in self.calls
            self.calls.append(tuple(messages))
            return 7 + 10 * len(messages) + sum(len(message.content or "") for message in messages)

    messages = (Message(role="assistant", content="greeting"), Message(role="user", content="now"))
    separated = split_messages(messages)
    data = PromptInput(history=separated.history, current_input=separated.current_input)
    counter = StrictCounter()
    result = await PromptBuilder(counter).build(data, budgets())
    assert result.messages == messages
    assert result.usage.total_tokens == 38  # Original prompt, including its fixed overhead.
    assert result.usage.tokens["history"] == 18
    assert result.usage.tokens["current_input"] == 13
    baseline = StrictCounter.region_messages(())
    assert counter.calls.count(baseline) == 1
    assert len(counter.calls) == 5  # Required/full totals, two regions, shared baseline.
    limited = await PromptBuilder(StrictCounter()).build(data, budgets(history=17))
    assert limited.usage.dropped["history"] == 1
    assert limited.messages == messages[1:]
    with pytest.raises(PromptBudgetExceeded) as caught:
        await PromptBuilder(StrictCounter()).build(data, budgets(current_input=12))
    assert caught.value.region == "current_input"


@pytest.mark.it1
@pytest.mark.parametrize(
    "roles, status",
    [
        (["assistant"], 500),
        (["system", "assistant"], 500),
        (["system"], 200),
        (["user", "assistant"], 200),
        (["assistant", "user"], 200),
        (["system", "assistant", "user"], 200),
        (["user", "user"], 200),
    ],
)
async def test_gemma_like_fixture_rejects_userless_dialogue(roles: list[str], status: int) -> None:
    from .fixture_server import create_token_fixture_app

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_token_fixture_app())
    ) as client:
        response = await client.post(
            "http://fixture/apply-template",
            json={
                "messages": [{"role": role, "content": "synthetic"} for role in roles],
            },
        )
    assert response.status_code == status


@pytest.mark.it1
@pytest.mark.parametrize(
    "entry, case",
    [
        ("chat", "incoming_greeting"),
        ("chat", "stored_greeting"),
        ("history", "stored_greeting"),
        ("chat", "stored_dialogue"),
        ("history", "stored_dialogue"),
    ],
)
def test_assistant_first_region_counts_succeed_in_both_apis(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str, case: str
) -> None:
    from dataclasses import replace

    from .fixture_server import create_token_fixture_app
    from .test_prompt_assembly_api import configured, http_setup, post, seeded_turns

    fixture = create_token_fixture_app()
    original = httpx.AsyncClient

    def client(**kwargs: Any) -> httpx.AsyncClient:
        return original(transport=httpx.ASGITransport(app=fixture), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
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
    greeting = Message(role="assistant", content="greeting")
    seeded = (
        ()
        if case == "incoming_greeting"
        else (greeting,)
        if case == "stored_greeting"
        else seeded_turns()
    )
    current = [{"role": "user", "content": "now"}]
    if case == "incoming_greeting":
        current.insert(0, greeting.model_dump(exclude_none=True))
    http, provider, inference, store = http_setup(char, seeded)
    response = post(http, entry, seeded, current)
    assert response.status_code == 200
    sent = provider.calls[0][1]["messages"]
    dialogue = [
        message
        for message in sent
        if message["role"] not in {"system"}
        and not (message.get("content") or "").startswith("retrieved_memory_data")
    ]
    assert dialogue == [message.model_dump(exclude_none=True) for message in seeded] + current
    # Fixture counts one per original message. Synthetic boundaries never reach inference.
    assert inference.prepared.usage.total_tokens == len(sent)
    assert {"messages": sent} in fixture.state.templates
    assert inference.prepared.usage.tokens["history"] == len(dialogue) - 1
    assert inference.prepared.usage.tokens["current_input"] == 1
    assert not any(inference.prepared.usage.dropped.values())
    # One shared baseline call per build; each count makes two HTTP requests.
    expected_counts = 7 + int(entry == "history") + int(case == "stored_dialogue")
    assert len(fixture.state.templates) == expected_counts
    assert (
        fixture.state.templates.count(
            {
                "messages": [
                    {"role": "user", "content": ""},
                    {"role": "user", "content": ""},
                ]
            }
        )
        == 1
    )
    serialized = [json.dumps(body, sort_keys=True) for body in fixture.state.templates]
    assert len(serialized) == len(set(serialized)) == len(fixture.state.tokenizations)
    assert all(
        any(m["role"] == "user" for m in body["messages"])
        or all(m["role"] == "system" for m in body["messages"])
        for body in fixture.state.templates
    )
    if entry == "history":
        store.append.assert_called_once()


@pytest.mark.it1
async def test_regional_tools_and_book_system_counts_use_exact_template(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from digital_souls_core.character_book import select_lore
    from digital_souls_core.prompt_builder import (
        CharacterCard,
        HistoryTurn,
        PromptBudgetExceeded,
        PromptInput,
    )

    from .fixture_server import create_token_fixture_app
    from .test_character_book import book, entry
    from .test_prompt_builder import tool_pair

    fixture = create_token_fixture_app()
    original = httpx.AsyncClient

    def client(**kwargs: Any) -> httpx.AsyncClient:
        return original(transport=httpx.ASGITransport(app=fixture), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    counter = LlamaCppTokenCounter("http://127.0.0.1:18081/v1", 1)
    selection = await select_lore(
        book(
            entry(content="first", priority=0), entry(content="second", priority=1), token_budget=1
        ),
        "key",
        (),
        counter,
    )
    assert selection.tokens == 1
    assert [item.content for item in selection.entries] == ["second"]
    assert selection.diagnostics[0].reason == "omitted_by_lore_budget"
    assert [
        [message["role"] for message in body["messages"]] for body in fixture.state.templates
    ] == [["system", "system"], ["system"]]
    data = PromptInput(
        card=CharacterCard(description="persona"),
        lore=selection.entries,
        history=(HistoryTurn((Message(role="assistant", content="greeting"),)),),
        current_input=(Message(role="user", content="now"), *tool_pair()),
        tools=(Tool.model_validate(TOOL),),
    )
    result = await PromptBuilder(counter).build(data, budgets(current_input=4))
    assert result.usage.total_tokens == len(result.messages) + 1
    assert result.usage.tokens["current_input"] == 4
    assert result.usage.tokens["history"] == 1
    assert {
        "messages": [m.model_dump(exclude_none=True) for m in result.messages],
        "tools": [TOOL],
    } in fixture.state.templates
    assert {
        "messages": [
            m.model_dump(exclude_none=True) for m in counter.region_messages(data.current_input)
        ],
        "tools": [TOOL],
    } in fixture.state.templates
    assert result.messages[-3:] == data.current_input
    with pytest.raises(PromptBudgetExceeded) as caught:
        await PromptBuilder(counter).build(data, budgets(current_input=3))
    assert caught.value.region == "current_input"


@pytest.mark.it1
@pytest.mark.parametrize("failure", ["region", "baseline"])
async def test_regional_template_failure_does_not_fall_back(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    from digital_souls_core.prompt_builder import PromptInput, split_messages

    from .fixture_server import create_token_fixture_app

    fixture = create_token_fixture_app()
    transport = httpx.ASGITransport(app=fixture)
    original = httpx.AsyncClient
    failed = False

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal failed
        if request.url.path == "/apply-template":
            messages = json.loads(request.content)["messages"]
            wrapped = messages[0] == messages[-1] == {"role": "user", "content": ""}
            baseline = wrapped and len(messages) == 2
            if wrapped and (
                (failure == "baseline" and baseline) or (failure == "region" and not baseline)
            ):
                failed = True
                return httpx.Response(500, text="synthetic-private-template-error")
        return await transport.handle_async_request(request)

    def client(**kwargs: Any) -> httpx.AsyncClient:
        return original(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    separated = split_messages(
        (Message(role="assistant", content="greeting"), Message(role="user", content="now"))
    )
    with pytest.raises(CoreError) as caught:
        await PromptBuilder(LlamaCppTokenCounter("http://127.0.0.1:18081/v1", 1)).build(
            PromptInput(history=separated.history, current_input=separated.current_input),
            budgets(),
        )
    assert failed
    assert caught.value.status == 502 and caught.value.code == "token_count_failed"
    assert "synthetic-private" not in str(caught.value)
