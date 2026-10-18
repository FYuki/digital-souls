import asyncio
import json
from typing import Any

import anyio
import httpcore
import httpx
import pytest

from digital_souls_core.application import CoreError
from digital_souls_core.provider import LiteLLMProvider

from .support import character, chunk, completion
from .test_llamacpp_provider import local_character
from .test_sdk_stream_ownership import TrackedBytes

pytestmark = pytest.mark.it1


@pytest.mark.parametrize("aiohttp", [False, True])
@pytest.mark.parametrize(
    "mode", ["text", "redirect", "error", "eof", "close", "cancel", "scope_cancel", "timeout"]
)
async def test_production_local_client_ignores_proxy_and_owns_transport(
    monkeypatch: pytest.MonkeyPatch, aiohttp: bool, mode: str
) -> None:
    for name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        monkeypatch.setenv(name, "http://synthetic-proxy.invalid:9876")
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.setenv(name, "")
    monkeypatch.setenv("DISABLE_AIOHTTP_TRANSPORT", "false" if aiohttp else "true")
    monkeypatch.setenv("AIOHTTP_TRUST_ENV", "true")
    transports: list[httpx.AsyncHTTPTransport] = []
    closed: list[httpx.AsyncHTTPTransport] = []
    events = [("data: " + json.dumps(chunk({"content": "synthetic"})) + "\n\n").encode()]
    if mode == "eof":
        events.append(b"data: [DONE]\n\n")
    wire = TrackedBytes(
        events,
        block=mode in {"close", "cancel", "scope_cancel"},
        error=httpx.ReadTimeout("synthetic") if mode == "timeout" else None,
    )

    async def send(transport: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        # Keep real AsyncClient/SDK construction and URL transport selection. Replace
        # only socket I/O; a proxy route is observable as an AsyncHTTPProxy pool.
        assert type(transport._pool) is httpcore.AsyncConnectionPool
        assert str(request.url) == "http://127.0.0.1:18081/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer local-no-auth"
        transports.append(transport)
        if mode == "redirect":
            return httpx.Response(307, headers={"location": "https://synthetic-exfil.invalid/"})
        if mode == "error":
            raise httpx.ConnectError("synthetic")
        if mode == "text":
            return httpx.Response(200, json=completion("gemma4-12b"))
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=wire)

    original_close = httpx.AsyncHTTPTransport.aclose

    async def close(transport: httpx.AsyncHTTPTransport) -> None:
        await asyncio.sleep(0)
        await original_close(transport)
        closed.append(transport)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "aclose", close)
    provider = LiteLLMProvider()
    profile = local_character().config.profile
    payload: dict[str, Any] = {"messages": [{"role": "user", "content": "synthetic"}]}
    if mode == "text":
        assert (await provider.complete(profile, payload))["choices"]
        assert (await provider.complete(profile, payload))["choices"]
    elif mode in {"redirect", "error"}:
        with pytest.raises(CoreError) as error:
            await provider.complete(profile, payload)
        assert error.value.code == "provider_error"
    else:
        iterator = provider.stream(profile, payload | {"stream": True})
        assert (await anext(iterator))["choices"][0]["delta"]["content"] == "synthetic"
        assert not closed
        if mode == "eof":
            _ = [item async for item in iterator]
        elif mode == "close":
            await iterator.aclose()
        elif mode == "scope_cancel":
            with anyio.CancelScope() as scope:
                scope.cancel()
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
        assert wire.closed
    assert len(transports) == (2 if mode == "text" else 1)
    assert len({id(transport) for transport in transports}) == len(transports)
    assert closed == transports


async def test_other_profiles_do_not_acquire_local_client(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Ordinary SDK profile must retain its existing transport ownership")

    monkeypatch.setattr(httpx.AsyncClient, "__init__", forbidden)
    async with LiteLLMProvider._client(character(native=True).config.profile) as client:
        assert client is None
