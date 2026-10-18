import asyncio

import httpx
import pytest

from digital_souls_core.application import CoreError
from digital_souls_core.local_embedding import LocalEmbedding, LocalEmbeddingProfile

pytestmark = pytest.mark.it1


def profile() -> LocalEmbeddingProfile:
    return LocalEmbeddingProfile(
        profile_id="synthetic-embedding",
        model="synthetic-embedding",
        model_digest="fixture-v1",
        dimensions=2,
        api_base="http://127.0.0.1:18181/v1",
        enabled=True,
    )


def response(texts: list[str]) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "object": "list",
            "model": "synthetic-embedding",
            "data": [
                {
                    "object": "embedding",
                    "index": i,
                    "embedding": [1.0, 0.0] if "tea" in text or "beverage" in text else [0.0, 1.0],
                }
                for i, text in enumerate(texts)
            ],
            "usage": {"prompt_tokens": 1, "total_tokens": 1},
        },
    )


async def test_shared_chat_client_rejects_environment_custom_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from digital_souls_core.provider import LiteLLMProvider

    from .test_llamacpp_provider import local_character

    monkeypatch.setenv("OPENAI_CUSTOM_HEADERS", "Authorization: SYNTHETIC_PRIVATE_CONTENT")

    async def forbidden(
        transport: httpx.AsyncHTTPTransport, request: httpx.Request
    ) -> httpx.Response:
        pytest.fail("Environment headers must not reach any local SDK route")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden)
    with pytest.raises(CoreError) as caught:
        await LiteLLMProvider().complete(
            local_character().config.profile,
            {"messages": [{"role": "user", "content": "synthetic"}]},
        )
    assert caught.value.code == "provider_error"
    assert "SYNTHETIC_PRIVATE_CONTENT" not in str(caught.value)


async def test_local_sdk_never_inherits_unused_environment_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from digital_souls_core.local_sdk import local_openai_client

    for name in ("OPENAI_ADMIN_KEY", "OPENAI_WEBHOOK_SECRET", "OPENAI_ORG_ID", "OPENAI_PROJECT_ID"):
        monkeypatch.setenv(name, "SYNTHETIC_PRIVATE_CONTENT")
    async with local_openai_client("http://127.0.0.1:18181/v1", 15) as client:
        assert client.admin_api_key == "" and client.webhook_secret == ""
        assert client.organization is None and client.project is None
        assert "SYNTHETIC_PRIVATE_CONTENT" not in repr(client.default_headers)


@pytest.mark.parametrize("setting", ["environment", "global"])
async def test_local_chat_rejects_litellm_organization_override(
    monkeypatch: pytest.MonkeyPatch, setting: str
) -> None:
    from digital_souls_core.provider import LiteLLMProvider, litellm

    from .test_llamacpp_provider import local_character

    if setting == "environment":
        monkeypatch.setenv("OPENAI_ORGANIZATION", "SYNTHETIC_PRIVATE_ORG")
    else:
        monkeypatch.setattr(litellm, "organization", "SYNTHETIC_PRIVATE_ORG")

    async def forbidden(
        transport: httpx.AsyncHTTPTransport, request: httpx.Request
    ) -> httpx.Response:
        pytest.fail("LiteLLM organization must not override the managed local SDK client")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden)
    with pytest.raises(CoreError) as caught:
        await LiteLLMProvider().complete(
            local_character().config.profile,
            {"messages": [{"role": "user", "content": "synthetic"}]},
        )
    assert caught.value.code == "local_configuration_denied"
    assert "SYNTHETIC_PRIVATE_ORG" not in str(caught.value)


@pytest.mark.parametrize("outer_delay,inner_delay", [(0.05, 0.5), (0.5, 0.3)])
async def test_outer_deadline_retires_inner_timer_before_transport_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    outer_delay: float,
    inner_delay: float,
) -> None:
    closed: list[httpx.AsyncHTTPTransport] = []
    original_close = httpx.AsyncHTTPTransport.aclose

    async def close(transport: httpx.AsyncHTTPTransport) -> None:
        # Deliberately outlast the inner deadline after outer cancellation.
        await asyncio.sleep(0.6)
        await original_close(transport)
        closed.append(transport)

    async def send(transport: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        outer.reschedule(asyncio.get_running_loop().time() + outer_delay)
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "aclose", close)
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(None) as outer:
            await LocalEmbedding(
                profile().model_copy(update={"timeout_seconds": inner_delay})
            ).embed(("synthetic",))
    assert len(closed) == 1


async def test_repeated_task_cancellation_waits_for_sdk_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered, closing, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    closed: list[httpx.AsyncHTTPTransport] = []
    original_close = httpx.AsyncHTTPTransport.aclose

    async def close(transport: httpx.AsyncHTTPTransport) -> None:
        closing.set()
        await release.wait()
        await original_close(transport)
        closed.append(transport)

    async def send(transport: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        entered.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "aclose", close)
    task = asyncio.create_task(LocalEmbedding(profile()).embed(("synthetic",)))
    await entered.wait()
    task.cancel()
    await closing.wait()
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(closed) == 1
