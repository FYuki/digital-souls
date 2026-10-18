import asyncio
import json
from collections.abc import AsyncGenerator
from typing import Any

import pytest
from starlette.types import Message

from digital_souls_core.api import create_app
from digital_souls_core.application import Inference
from digital_souls_core.character import Profile

from .support import FakeProvider, character, chunk

pytestmark = pytest.mark.it1


@pytest.mark.parametrize("after_first", [False, True])
async def test_http_disconnect_closes_upstream_before_and_after_first_chunk(
    after_first: bool,
) -> None:
    entered = asyncio.Event()
    closed = asyncio.Event()

    class BlockingProvider(FakeProvider):
        async def stream(
            self, profile: Profile, payload: dict[str, Any]
        ) -> AsyncGenerator[dict[str, Any]]:
            try:
                entered.set()
                if after_first:
                    yield chunk({"content": "first"})
                await asyncio.Event().wait()
            finally:
                closed.set()

    app = create_app(Inference((character(),), BlockingProvider()))
    incoming: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    await incoming.put(
        {
            "type": "http.request",
            "body": json.dumps(
                {
                    "model": "miori-alias",
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": True,
                }
            ).encode(),
            "more_body": False,
        }
    )
    sent_chunk = asyncio.Event()

    async def send(message: Message) -> None:
        if message["type"] == "http.response.body" and b"first" in message.get("body", b""):
            sent_chunk.set()

    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "scheme": "http",
        "method": "POST",
        "path": "/v1/chat/completions",
        "raw_path": b"/v1/chat/completions",
        "query_string": b"",
        "root_path": "",
        "headers": [(b"content-type", b"application/json"), (b"host", b"127.0.0.1:8000")],
        "server": ("127.0.0.1", 8000),
        "client": ("127.0.0.1", 1234),
    }
    task = asyncio.create_task(app(scope, incoming.get, send))
    await asyncio.wait_for(entered.wait(), 2)
    if after_first:
        await asyncio.wait_for(sent_chunk.wait(), 2)
    await incoming.put({"type": "http.disconnect"})
    await asyncio.wait_for(task, 2)
    assert closed.is_set()


@pytest.mark.parametrize("explicit", [False, True])
async def test_queued_disconnect_closes_completed_stateless_prefetch(explicit: bool) -> None:
    provider = FakeProvider()
    app = create_app(Inference((character(),), provider))
    path = "/v1/character/completions" if explicit else "/v1/chat/completions"
    selector = {"character_id": "miori"} if explicit else {"model": "miori-alias"}
    incoming: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    incoming.put_nowait(
        {
            "type": "http.request",
            "body": json.dumps(
                {
                    **selector,
                    "messages": [{"role": "user", "content": "Synthetic"}],
                    "stream": True,
                }
            ).encode(),
            "more_body": False,
        }
    )
    incoming.put_nowait({"type": "http.disconnect"})
    sent: list[Message] = []

    async def send(message: Message) -> None:
        sent.append(message)

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
    await asyncio.wait_for(app(scope, incoming.get, send), 2)
    assert len(provider.calls) == 1
    assert provider.closed
    assert next(item["status"] for item in sent if item["type"] == "http.response.start") == 499


@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize("cancellations", [0, 1, 2])
async def test_request_cancel_during_watcher_cleanup_preserves_stream_ownership(
    explicit: bool,
    cancellations: int,
) -> None:
    cleanup_entered = asyncio.Event()
    cleanup_release = asyncio.Event()
    cleanup_exited = asyncio.Event()
    close_entered = asyncio.Event()
    close_release = asyncio.Event()
    if cancellations != 2:
        close_release.set()
    close_count = 0

    class CountingProvider(FakeProvider):
        async def stream(
            self, profile: Profile, payload: dict[str, Any]
        ) -> AsyncGenerator[dict[str, Any]]:
            nonlocal close_count
            try:
                yield chunk({"content": "Synthetic first"})
                yield chunk({}, "stop")
            finally:
                close_entered.set()
                await close_release.wait()
                close_count += 1
                self.closed = True

    provider = CountingProvider()
    app = create_app(Inference((character(),), provider))
    path = "/v1/character/completions" if explicit else "/v1/chat/completions"
    selector = {"character_id": "miori"} if explicit else {"model": "miori-alias"}
    first_receive = True

    async def receive() -> Message:
        nonlocal first_receive
        if first_receive:
            first_receive = False
            return {
                "type": "http.request",
                "body": json.dumps(
                    {
                        **selector,
                        "messages": [{"role": "user", "content": "Synthetic"}],
                        "stream": True,
                    }
                ).encode(),
                "more_body": False,
            }
        try:
            await asyncio.Event().wait()
        finally:
            cleanup_entered.set()
            await cleanup_release.wait()
            cleanup_exited.set()
        raise AssertionError("receive was expected to be cancelled")

    sent: list[Message] = []

    async def send(message: Message) -> None:
        if message["type"] == "http.response.start":
            # Normal handoff must leave the upstream open for ASGI to consume.
            assert not provider.closed
        sent.append(message)

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
    request_task = asyncio.create_task(app(scope, receive, send))
    await asyncio.wait_for(cleanup_entered.wait(), 2)
    assert not provider.closed and not sent
    if cancellations:
        request_task.cancel()
    cleanup_release.set()
    if cancellations == 2:
        await asyncio.wait_for(close_entered.wait(), 2)
        request_task.cancel()
        close_release.set()
    if cancellations:
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(request_task, 2)
        assert not sent
    else:
        await asyncio.wait_for(request_task, 2)
        assert any(b"[DONE]" in message.get("body", b"") for message in sent)
    assert cleanup_exited.is_set()
    assert provider.closed and close_count == 1
