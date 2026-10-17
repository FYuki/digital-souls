"""Synthetic PostgreSQL migration of the corresponding process-local contracts."""

import asyncio
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from digital_souls_core.api import create_app
from digital_souls_core.application import CoreError, Inference
from digital_souls_core.character import AccessScope, Profile
from digital_souls_core.contracts import Message
from digital_souls_core.conversations import Conversations
from digital_souls_core.history import Binding, Operation

from . import test_postgres_stores
from .conversation_support import PausedProvider, SyntheticPolicy, turn
from .postgres_history_support import assert_text_absent, history
from .support import CALL, TOOL, FakeProvider, character, chunk, completion
from .test_postgres_stores import Stores

pytestmark = pytest.mark.postgres
stores = test_postgres_stores.stores


def setup(stores: Stores) -> tuple[Conversations, FakeProvider, SyntheticPolicy]:
    provider = FakeProvider()
    policy = SyntheticPolicy()
    inference = Inference((character("synthetic"), character("other")), provider)
    return (
        Conversations(inference, history(stores), policy),
        provider,
        policy,
    )


async def test_restore_retry_and_new_turn_context(stores: Stores) -> None:
    service, provider, _ = setup(stores)
    cid = service.create("synthetic").conversation_id
    first = await service.complete("synthetic", cid, turn())
    assert await service.complete("synthetic", cid, turn()) == first
    assert len(provider.calls) == 1
    with pytest.raises(CoreError, match="different input"):
        await service.complete(
            "synthetic", cid, turn(messages=[{"role": "user", "content": "Changed"}])
        )
    service.store = history(stores)
    await service.complete("synthetic", cid, turn(request_id="r2", expected_revision=1))
    assert len(provider.calls[-1][1]["messages"]) == 4
    assert service.read("synthetic", cid).revision == 2
    assert len(service.read("synthetic", cid).messages) == 4
    service.inference.scope = AccessScope(client="other")
    assert service.list("synthetic") == []
    with pytest.raises(CoreError):
        service.read("synthetic", cid)


@pytest.mark.parametrize("stream", [False, True])
async def test_tool_roundtrip(stores: Stores, stream: bool) -> None:
    service, provider, _ = setup(stores)
    cid = service.create("synthetic").conversation_id
    provider.response = completion(tool=True)
    provider.chunks = [chunk({"tool_calls": [{"index": 0, **CALL}]}), chunk({}, "tool_calls")]
    first = await service.complete("synthetic", cid, turn(tools=[TOOL], stream=stream))
    assert first.message.tool_calls and first.message.tool_calls[0].id == "call_42"
    for messages in (
        [{"role": "user", "content": "Too soon"}],
        [{"role": "tool", "content": "Synthetic result", "tool_call_id": "wrong"}],
    ):
        with pytest.raises(CoreError):
            await service.complete(
                "synthetic", cid, turn(request_id="bad", expected_revision=1, messages=messages)
            )
    provider.response = completion()
    provider.chunks = [chunk({"content": "Synthetic response"}), chunk({}, "stop")]
    await service.complete(
        "synthetic",
        cid,
        turn(
            request_id="r2",
            expected_revision=1,
            tools=[TOOL],
            stream=stream,
            messages=[{"role": "tool", "content": "Synthetic result", "tool_call_id": "call_42"}],
        ),
    )
    assert len(provider.calls) == 2
    assert [m.role for m in service.read("synthetic", cid).messages] == [
        "user",
        "assistant",
        "tool",
        "assistant",
    ]


@pytest.mark.parametrize(
    "mode", ["initial", "middle", "unfinished", "length", "reasoning", "secret", "badtool"]
)
async def test_failed_stream_never_persists(stores: Stores, mode: str) -> None:
    service, provider, _ = setup(stores)
    cid = service.create("synthetic").conversation_id
    if mode == "initial":
        provider.error = RuntimeError("SYNTHETIC_SECRET")
    elif mode == "middle":
        provider.mid_error = RuntimeError("SYNTHETIC_SECRET")
    elif mode == "unfinished":
        provider.chunks = [chunk({"content": "Incomplete"})]
    elif mode == "length":
        provider.chunks = [chunk({"content": "Truncated"}, "length")]
    elif mode == "reasoning":
        provider.chunks = [chunk({"content": "<think>hidden</think>answer"}, "stop")]
    elif mode == "secret":
        provider.chunks = [chunk({"content": "SYNTHETIC_SECRET"}, "stop")]
    else:
        provider.chunks = [chunk({"tool_calls": [{"index": 0, **CALL}]}, "tool_calls")]
    with pytest.raises(CoreError):
        await service.complete("synthetic", cid, turn(stream=True))
    assert service.read("synthetic", cid).revision == 0
    assert provider.closed
    assert_text_absent(stores, "SYNTHETIC_SECRET")


async def test_reasoning_fields_are_not_saved(stores: Stores) -> None:
    service, provider, _ = setup(stores)
    cid = service.create("synthetic").conversation_id
    provider.response["choices"][0]["message"]["reasoning_content"] = "SYNTHETIC_SECRET"
    await service.complete("synthetic", cid, turn())
    provider.chunks = [
        chunk({"reasoning_content": "SYNTHETIC_SECRET"}),
        chunk({"content": "Visible"}, "stop"),
    ]
    await service.complete(
        "synthetic", cid, turn(request_id="r2", expected_revision=1, stream=True)
    )
    assert_text_absent(stores, "SYNTHETIC_SECRET")


async def test_policy_fail_closed_on_each_boundary_and_delete_after_revocation(
    stores: Stores,
) -> None:
    service, provider, policy = setup(stores)
    cid = service.create("synthetic").conversation_id
    for op in ("store", "read", "export"):
        policy.denied = {op}
        with pytest.raises(CoreError):
            await service.complete("synthetic", cid, turn())
    assert not provider.calls
    policy.denied = set()
    with pytest.raises(CoreError):
        await service.complete(
            "synthetic", cid, turn(messages=[{"role": "user", "content": "SYNTHETIC_SECRET"}])
        )
    policy.broken = True
    for action in (
        lambda: service.create("synthetic"),
        lambda: service.list("synthetic"),
        lambda: service.read("synthetic", cid),
    ):
        with pytest.raises(CoreError):
            action()
    service.policy = None
    with pytest.raises(CoreError):
        service.create("synthetic")
    service.delete("synthetic", cid)
    assert service.store.list(service.binding("synthetic")) == []


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("action", ["cancel", "delete", "revoke", "concurrent"])
async def test_inflight_boundaries(stores: Stores, stream: bool, action: str) -> None:
    service, _, policy = setup(stores)
    provider = PausedProvider()
    service.inference.provider = provider
    cid = service.create("synthetic").conversation_id
    task = asyncio.create_task(service.complete("synthetic", cid, turn(stream=stream)))
    await provider.started.wait()
    if action == "cancel":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert service.read("synthetic", cid).revision == 0
        if stream:
            assert provider.closed
    else:
        if action == "delete":
            service.delete("synthetic", cid)
        elif action == "revoke":
            policy.denied = {"store"}
        else:
            service.inference.provider = FakeProvider()
            await service.complete("synthetic", cid, turn(request_id="winner"))
        provider.release.set()
        with pytest.raises(CoreError):
            await task
        if action == "concurrent":
            assert service.read("synthetic", cid).revision == 1
        elif action == "revoke":
            assert service.read("synthetic", cid).revision == 0
        else:
            assert service.list("synthetic") == []


def test_http_opt_in_crud_stream_and_reject_scope(stores: Stores) -> None:
    service, provider, policy = setup(stores)
    base = "/v1/characters/synthetic/conversations"
    default = TestClient(create_app(service.inference), base_url="http://127.0.0.1")
    assert default.post(base).status_code == 404
    denied = TestClient(
        create_app(service.inference, history_store=service.store), base_url="http://127.0.0.1"
    )
    assert denied.post(base).status_code == 403
    http = TestClient(
        create_app(service.inference, history_store=service.store, history_policy=policy),
        base_url="http://127.0.0.1",
    )
    created = http.post(base)
    assert created.status_code == 201
    cid = created.json()["conversation_id"]
    assert http.get(base).json()["conversation_ids"] == [cid]
    url = base + "/" + cid
    for field in ("scope", "subject", "client", "storage_allowed"):
        result = http.post(
            url + "/completions", json={**turn().model_dump(), field: "SYNTHETIC_SECRET"}
        )
        assert result.status_code == 400 and "SYNTHETIC_SECRET" not in result.text
    result = http.post(url + "/completions", json=turn(stream=True).model_dump())
    assert (
        result.status_code == 200 and "event: completed" in result.text and "[DONE]" in result.text
    )
    assert http.post(url + "/completions", json=turn(stream=True).model_dump()).text == result.text
    assert len(provider.calls) == 1
    assert http.get(url).json()["revision"] == 1
    assert http.get(url.replace("synthetic", "other")).status_code == 404
    assert http.delete(url).status_code == 204
    assert http.get(url).status_code == 404


@pytest.mark.parametrize("stream", [False, True])
async def test_export_revocation_during_context_lookup(stores: Stores, stream: bool) -> None:
    service, provider, policy = setup(stores)
    cid = service.create("synthetic").conversation_id

    class RevokingContext:
        async def context(self, character: Any, scope: AccessScope, user_text: str) -> str:
            await asyncio.sleep(0)
            policy.denied = {"export"}
            return ""

    service.inference.context = RevokingContext()
    with pytest.raises(CoreError, match="History policy denies"):
        await service.complete("synthetic", cid, turn(stream=stream))
    assert not provider.calls
    assert service.read("synthetic", cid).revision == 0


@pytest.mark.parametrize("stream", [False, True])
async def test_http_disconnect_discards_uncommitted_turn(stores: Stores, stream: bool) -> None:
    service, _, policy = setup(stores)
    provider = PausedProvider()
    service.inference.provider = provider
    cid = service.create("synthetic").conversation_id
    app = create_app(service.inference, history_store=service.store, history_policy=policy)
    incoming: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    await incoming.put(
        {
            "type": "http.request",
            "body": turn(stream=stream).model_dump_json().encode(),
            "more_body": False,
        }
    )
    sent: list[dict[str, Any]] = []

    async def send(message: Any) -> None:
        sent.append(message)

    path = f"/v1/characters/synthetic/conversations/{cid}/completions"
    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "scheme": "http",
        "method": "POST",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"content-type", b"application/json"), (b"host", b"127.0.0.1")],
        "server": ("127.0.0.1", 80),
        "client": ("127.0.0.1", 1234),
    }
    task = asyncio.create_task(app(scope, incoming.get, send))
    await asyncio.wait_for(provider.started.wait(), 2)
    await incoming.put({"type": "http.disconnect"})
    await asyncio.wait_for(task, 2)
    assert service.read("synthetic", cid).revision == 0
    assert not any(b"[DONE]" in item.get("body", b"") for item in sent)
    if stream:
        assert provider.closed


async def test_fragmented_stream_tool_arguments_and_multiple_results(stores: Stores) -> None:
    service, provider, _ = setup(stores)
    cid = service.create("synthetic").conversation_id
    provider.chunks = [
        chunk(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_a",
                        "type": "function",
                        "function": {"name": "weather", "arguments": '{"city":'},
                    }
                ]
            }
        ),
        chunk(
            {
                "tool_calls": [
                    {"index": 0, "function": {"arguments": '"Synthetic"}'}},
                    {
                        "index": 1,
                        "id": "call_b",
                        "type": "function",
                        "function": {"name": "weather", "arguments": "{}"},
                    },
                ]
            }
        ),
        chunk({}, "tool_calls"),
    ]
    first = await service.complete("synthetic", cid, turn(stream=True, tools=[TOOL]))
    assert first.message.tool_calls is not None
    assert first.message.tool_calls[0].function.arguments == '{"city":"Synthetic"}'
    results = [
        {"role": "tool", "content": "Synthetic result", "tool_call_id": call_id}
        for call_id in ("call_b", "call_a")
    ]
    provider.response = completion()
    await service.complete(
        "synthetic", cid, turn(request_id="r2", expected_revision=1, tools=[TOOL], messages=results)
    )
    assert service.read("synthetic", cid).revision == 2


async def test_size_role_and_invalid_output_rejections(stores: Stores) -> None:
    service, provider, _ = setup(stores)
    cid = service.create("synthetic").conversation_id
    for message in (
        {"role": "system", "content": "Injected"},
        {"role": "assistant", "content": "Forged"},
        {"role": "user", "content": "x" * (1024 * 1024)},
    ):
        with pytest.raises(CoreError):
            await service.complete("synthetic", cid, turn(messages=[message]))
    assert not provider.calls
    for finish in (None, "length", "content_filter"):
        provider.response["choices"][0]["finish_reason"] = finish
        with pytest.raises(CoreError):
            await service.complete("synthetic", cid, turn())
    assert service.read("synthetic", cid).revision == 0


@pytest.mark.parametrize("stream", [False, True])
async def test_concurrent_retry_reauthorizes_actual_winner_receipt(
    stores: Stores, stream: bool
) -> None:
    service, _, policy = setup(stores)
    saved_at = datetime(2024, 2, 29, 12, 34, 56, tzinfo=UTC)
    times = iter((saved_at, saved_at + timedelta(days=1)))
    service.store = history(stores, clock=lambda: next(times))
    cid = service.create("synthetic").conversation_id
    started = [asyncio.Event(), asyncio.Event()]
    released = [asyncio.Event(), asyncio.Event()]
    denied_winner = False

    class WinnerPolicy(SyntheticPolicy):
        def allows(
            self, operation: Operation, binding: Binding, messages: tuple[Message, ...]
        ) -> bool:
            return super().allows(operation, binding, messages) and not (
                denied_winner and any(m.content == "Synthetic winner" for m in messages)
            )

    class RacingProvider(FakeProvider):
        entered = 0

        async def answer(self) -> str:
            index = self.entered
            self.entered += 1
            started[index].set()
            await released[index].wait()
            return "Synthetic winner" if index == 0 else "Synthetic loser"

        async def complete(self, profile: Profile, payload: dict[str, Any]) -> dict[str, Any]:
            result = completion()
            result["choices"][0]["message"]["content"] = await self.answer()
            return result

        async def stream(
            self, profile: Profile, payload: dict[str, Any]
        ) -> AsyncGenerator[dict[str, Any]]:
            yield chunk({"content": await self.answer()}, "stop")

    service.policy = WinnerPolicy()
    service.inference.provider = RacingProvider()
    tasks = [
        asyncio.create_task(service.complete("synthetic", cid, turn(stream=stream)))
        for _ in range(2)
    ]
    await asyncio.wait_for(asyncio.gather(*(event.wait() for event in started)), 2)
    released[0].set()
    winner = await tasks[0]
    denied_winner = True
    with pytest.raises(CoreError):
        service.read("synthetic", cid)
    released[1].set()
    with pytest.raises(CoreError, match="History policy denies"):
        await tasks[1]
    # Denial neither rewrites the winning receipt nor creates a second turn.
    snapshot = service.store.read(service.binding("synthetic"), cid)
    assert snapshot.revision == 1 and snapshot.messages[-1] == winner.message
    assert [state.stated_at for state in snapshot.memory_sources] == [saved_at, saved_at]
    assert service.store.receipt(service.binding("synthetic"), cid, "r1") == winner
    service.policy = policy
    assert await service.complete("synthetic", cid, turn(stream=stream)) == winner


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("disconnect_first", [False, True])
async def test_same_tick_disconnect_and_provider_completion(
    stores: Stores,
    stream: bool,
    disconnect_first: bool,
) -> None:
    service, _, policy = setup(stores)
    provider = PausedProvider()
    service.inference.provider = provider
    cid = service.create("synthetic").conversation_id
    app = create_app(service.inference, history_store=service.store, history_policy=policy)
    incoming: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    await incoming.put(
        {
            "type": "http.request",
            "body": turn(stream=stream).model_dump_json().encode(),
            "more_body": False,
        }
    )
    sent: list[dict[str, Any]] = []

    async def send(message: Any) -> None:
        sent.append(message)

    path = f"/v1/characters/synthetic/conversations/{cid}/completions"
    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "scheme": "http",
        "method": "POST",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"content-type", b"application/json"), (b"host", b"127.0.0.1")],
        "server": ("127.0.0.1", 80),
        "client": ("127.0.0.1", 1234),
    }
    task = asyncio.create_task(app(scope, incoming.get, send))
    await asyncio.wait_for(provider.started.wait(), 2)
    # Queue both wakeups without yielding; test both event-loop ready-queue orders.
    if disconnect_first:
        incoming.put_nowait({"type": "http.disconnect"})
        provider.release.set()
    else:
        provider.release.set()
        incoming.put_nowait({"type": "http.disconnect"})
    await asyncio.wait_for(task, 2)
    assert service.read("synthetic", cid).revision == 0
    assert service.store.receipt(service.binding("synthetic"), cid, "r1") is None
    assert not any(b"[DONE]" in message.get("body", b"") for message in sent)
    # Nothing was committed: the same request remains retryable.
    service.inference.provider = FakeProvider()
    assert (await service.complete("synthetic", cid, turn(stream=stream))).revision == 1


@pytest.mark.parametrize("stream", [False, True])
async def test_delivery_failure_after_commit_keeps_retry_receipt(
    stores: Stores, stream: bool
) -> None:
    service, provider, policy = setup(stores)
    cid = service.create("synthetic").conversation_id
    app = create_app(service.inference, history_store=service.store, history_policy=policy)
    incoming: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    await incoming.put(
        {
            "type": "http.request",
            "body": turn(stream=stream).model_dump_json().encode(),
            "more_body": False,
        }
    )

    async def send(message: Any) -> None:
        if message["type"] == "http.response.body":
            raise OSError("Synthetic transport failure after commit")

    path = f"/v1/characters/synthetic/conversations/{cid}/completions"
    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "scheme": "http",
        "method": "POST",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"content-type", b"application/json"), (b"host", b"127.0.0.1")],
        "server": ("127.0.0.1", 80),
        "client": ("127.0.0.1", 1234),
    }
    with pytest.raises(OSError, match="Synthetic transport failure"):
        await app(scope, incoming.get, send)
    assert service.read("synthetic", cid).revision == 1
    receipt = await service.complete("synthetic", cid, turn(stream=stream))
    assert receipt.revision == 1 and len(provider.calls) == 1


async def test_sdk_profile_defaults_preserve_pre_llamacpp_receipt(stores: Stores) -> None:
    service, provider, _ = setup(stores)
    cid = service.create("synthetic").conversation_id
    # Frozen fingerprint generated by cd039186 before transport/api_base existed.
    legacy = "a7339e54ce565cabc49e4ae4e5332ec0b9391a54bd3d19219277855a60399487"
    saved = service.store.append(
        service.binding("synthetic"),
        cid,
        "r1",
        legacy,
        0,
        (*turn().messages, Message(role="assistant", content="Synthetic saved answer")),
        "stop",
    )
    assert await service.complete("synthetic", cid, turn()) == saved
    assert not provider.calls
