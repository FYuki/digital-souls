"""Synthetic PostgreSQL migration of the corresponding process-local contracts."""

import asyncio
import json
from typing import Any

import httpcore
import httpx
import pytest

from digital_souls_core.application import CoreError, Inference
from digital_souls_core.conversations import Conversations
from digital_souls_core.provider import LiteLLMProvider

from . import test_postgres_stores
from .conversation_support import SyntheticPolicy, turn
from .postgres_history_support import assert_text_absent, history
from .support import CALL, TOOL, chunk, completion
from .test_llamacpp_provider import local_character
from .test_postgres_stores import Stores
from .test_sdk_stream_ownership import TrackedBytes

pytestmark = pytest.mark.postgres
stores = test_postgres_stores.stores


@pytest.mark.parametrize("mode", ["text", "tools", "stream", "stream_tools", "timeout", "cancel"])
async def test_history_with_production_local_adapter(
    stores: Stores,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "SYNTHETIC_CLOUD_SECRET")
    monkeypatch.setenv("HTTPS_PROXY", "http://synthetic-proxy.invalid:9876")
    sent: list[dict[str, Any]] = []
    transports: list[httpx.AsyncHTTPTransport] = []
    closed: list[httpx.AsyncHTTPTransport] = []
    wires: list[TrackedBytes] = []
    wire_started = asyncio.Event()
    failure = mode in {"timeout", "cancel"}
    tools = mode in {"tools", "stream_tools"}
    streaming = mode not in {"text", "tools"}

    async def send(transport: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        assert type(transport._pool) is httpcore.AsyncConnectionPool
        assert str(request.url) == "http://127.0.0.1:18081/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer local-no-auth"
        body = json.loads(request.content)
        assert body["model"] == "gemma4-12b"
        sent.append(body)
        transports.append(transport)
        tool_call = tools and len(sent) == 1
        if not body.get("stream"):
            return httpx.Response(200, json=completion("gemma4-12b", tool=tool_call))
        delta: dict[str, Any] = (
            {"tool_calls": [{"index": 0, **CALL}]} if tool_call else {"content": "Synthetic"}
        )
        values = [chunk(delta)]
        if not failure:
            values.append(chunk({}, "tool_calls" if tool_call else "stop"))
        events = [("data: " + json.dumps(value) + "\n\n").encode() for value in values]
        if not failure:
            events.append(b"data: [DONE]\n\n")
        wire = TrackedBytes(
            events,
            block=mode == "cancel",
            error=httpx.ReadTimeout("SYNTHETIC_SECRET") if mode == "timeout" else None,
        )
        wires.append(wire)
        wire_started.set()
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=wire)

    original_close = httpx.AsyncHTTPTransport.aclose

    async def close(transport: httpx.AsyncHTTPTransport) -> None:
        await asyncio.sleep(0)
        await original_close(transport)
        closed.append(transport)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "aclose", close)
    service = Conversations(
        Inference((local_character(),), LiteLLMProvider()),
        history(stores),
        SyntheticPolicy(),
    )
    cid = service.create("miori").conversation_id
    body = turn(stream=streaming, **({"tools": [TOOL]} if tools else {}))
    if failure:
        task = asyncio.create_task(service.complete("miori", cid, body))
        if mode == "cancel":
            # Wait for the actual SDK response iterator to block, not a provider stub.
            await asyncio.wait_for(wire_started.wait(), 2)
            await asyncio.wait_for(wires[0].waiting.wait(), 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(CoreError) as error:
                await task
            assert error.value.code == "provider_timeout"
        assert service.read("miori", cid).revision == 0
        assert service.store.receipt(service.binding("miori"), cid, "r1") is None
    else:
        first = await service.complete("miori", cid, body)
        service.store = history(stores)
        assert await service.complete("miori", cid, body) == first
        assert len(sent) == 1
        messages = (
            [{"role": "tool", "tool_call_id": CALL["id"], "content": "Synthetic result"}]
            if tools
            else [{"role": "user", "content": "Synthetic next"}]
        )
        await service.complete(
            "miori",
            cid,
            turn(
                request_id="r2",
                expected_revision=1,
                stream=streaming,
                messages=messages,
                **({"tools": [TOOL]} if tools else {}),
            ),
        )
        assert service.read("miori", cid).revision == 2
        assert sent[-1]["messages"][2] == first.message.model_dump(exclude_none=True)
    assert closed == transports
    assert all(wire.closed for wire in wires)
    assert_text_absent(stores, "SYNTHETIC_CLOUD_SECRET")
