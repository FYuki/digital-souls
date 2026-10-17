import asyncio
import json
import warnings
from dataclasses import dataclass, field
from typing import Any

import anyio
import httpcore
import httpx
import pytest
from pydantic import ValidationError

from digital_souls_core.application import CoreError
from digital_souls_core.local_embedding import LocalEmbedding, LocalEmbeddingProfile

pytestmark = pytest.mark.it1


def profile(**changes: Any) -> LocalEmbeddingProfile:
    return LocalEmbeddingProfile.model_validate(
        {
            "profile_id": "synthetic-local-embedding",
            "model": "synthetic-embedding",
            "model_digest": "synthetic-revision-v1",
            "dimensions": 2,
            "api_base": "http://127.0.0.1:18082/v1",
            "enabled": True,
        }
        | changes
    )


def embedding_response() -> dict[str, Any]:
    return {
        "object": "list",
        "model": "synthetic-embedding",
        "data": [{"object": "embedding", "index": 0, "embedding": [1.0, 2.0]}],
        "usage": {"prompt_tokens": 1, "total_tokens": 1},
    }


@dataclass
class Wire:
    body: Any = field(default_factory=embedding_response)
    status: int = 200
    exception: Exception | None = None
    block: bool = False
    raw: bytes | None = None
    waiting: asyncio.Event = field(default_factory=asyncio.Event)
    requests: list[httpx.Request] = field(default_factory=list)
    transports: list[httpx.AsyncHTTPTransport] = field(default_factory=list)
    closed: list[httpx.AsyncHTTPTransport] = field(default_factory=list)


@pytest.fixture
def wire(monkeypatch: pytest.MonkeyPatch) -> Wire:
    result = Wire()

    async def send(transport: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        # Keep SDK construction, URL routing, and response parsing. Only replace
        # socket I/O, so an inherited proxy would expose a different pool type.
        assert type(transport._pool) is httpcore.AsyncConnectionPool
        result.requests.append(request)
        result.transports.append(transport)
        if result.exception is not None:
            raise result.exception
        if result.block:
            result.waiting.set()
            await asyncio.Event().wait()
        return httpx.Response(
            result.status,
            headers={
                "content-type": "application/json",
                "location": "https://synthetic-exfil.invalid/",
            },
            content=result.raw if result.raw is not None else json.dumps(result.body).encode(),
        )

    original_close = httpx.AsyncHTTPTransport.aclose

    async def close(transport: httpx.AsyncHTTPTransport) -> None:
        # A real checkpoint makes cancellation-shielded cleanup observable.
        await asyncio.sleep(0)
        await original_close(transport)
        result.closed.append(transport)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", send)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "aclose", close)
    return result


def assert_failed(error: CoreError, *, timeout: bool = False) -> None:
    assert error.status == (504 if timeout else 502)
    assert error.code == ("memory_embedding_timeout" if timeout else "memory_embedding_failed")
    assert "SYNTHETIC_PRIVATE_CONTENT" not in str(error)
    assert "SYNTHETIC_PRIVATE_CONTENT" not in error.message
    assert error.__cause__ is None


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://127.0.0.1:18082/v1",
        "http://example.invalid:18082/v1",
        "http://localhost:18082/v1",
        "http://[::1]:18082/v1",
        "http://127.0.0.1/v1",
        "http://127.0.0.1:0/v1",
        "http://127.0.0.1:65536/v1",
        "http://user:synthetic@127.0.0.1:18082/v1",
        "http://127.0.0.1:18082/other",
        "http://127.0.0.1:18082/v1?token=synthetic",
        "http://127.0.0.1:18082/v1#synthetic",
        "http://127.0.0.1:18082/v1/../v1",
        "http://127.0.0.1:18082/v1\n",
    ],
)
def test_profile_rejects_unverified_endpoint(endpoint: str) -> None:
    with pytest.raises(ValidationError):
        profile(api_base=endpoint)


@pytest.mark.parametrize(
    "changes",
    [
        {"dimensions": 0},
        {"dimensions": 4097},
        {"dimensions": True},
        {"dimensions": "2"},
        {"model": ""},
        {"model": " synthetic-embedding"},
        {"model": "password=SYNTHETIC_PRIVATE_CONTENT"},
        {"model_digest": ""},
        {"model_digest": " synthetic-revision"},
        {"model_digest": "password=SYNTHETIC_PRIVATE_CONTENT"},
        {"profile_id": ""},
        {"profile_id": "password=SYNTHETIC_PRIVATE_CONTENT"},
        {"timeout_seconds": 0},
        {"timeout_seconds": 15.1},
        {"timeout_seconds": float("nan")},
        {"timeout_seconds": float("inf")},
        {"enabled": "true"},
        {"api_key": "SYNTHETIC_PRIVATE_CONTENT"},
    ],
)
def test_profile_is_strict_and_bounded(changes: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        profile(**changes)


def test_profile_is_frozen_and_disabled_by_default() -> None:
    values = profile().model_dump(exclude={"enabled", "timeout_seconds"})
    configured = LocalEmbeddingProfile.model_validate(values)
    assert configured.enabled is False
    assert configured.timeout_seconds == 15
    with pytest.raises(ValidationError):
        configured.enabled = True


def test_embedding_space_pins_profile_endpoint_and_model_revision() -> None:
    current = LocalEmbedding(profile()).space
    assert current.model == "synthetic-embedding"
    assert current.revision == "synthetic-revision-v1"
    assert current.dimensions == 2
    assert current == LocalEmbedding(profile()).space
    assert current.configuration != "in-process"
    for changes in (
        {"profile_id": "other-synthetic-profile"},
        {"api_base": "http://127.0.0.1:18083/v1"},
        {"model": "other-synthetic-embedding"},
        {"model_digest": "other-synthetic-revision"},
        {"dimensions": 3},
        {"timeout_seconds": 3.0},
        {"enabled": False},
    ):
        assert LocalEmbedding(profile(**changes)).space != current


async def test_sdk_request_has_exact_body_and_isolates_operator_environment(
    monkeypatch: pytest.MonkeyPatch, wire: Wire
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
    monkeypatch.setenv("OPENAI_API_KEY", "SYNTHETIC_PRIVATE_CONTENT")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://synthetic-cloud.invalid/v1")
    monkeypatch.setenv("OPENAI_ORG_ID", "SYNTHETIC_PRIVATE_CONTENT")
    monkeypatch.setenv("OPENAI_PROJECT_ID", "SYNTHETIC_PRIVATE_CONTENT")
    adapter = LocalEmbedding(profile())
    assert await adapter.embed(("合成クエリ",)) == ((1.0, 2.0),)
    assert await adapter.embed(("synthetic candidate",)) == ((1.0, 2.0),)
    assert len(wire.requests) == 2
    for request, expected in zip(wire.requests, ("合成クエリ", "synthetic candidate"), strict=True):
        assert str(request.url) == "http://127.0.0.1:18082/v1/embeddings"
        assert request.method == "POST"
        assert request.headers["authorization"] == "Bearer local-no-auth"
        assert "SYNTHETIC_PRIVATE_CONTENT" not in str(request.headers)
        assert not request.headers.get("openai-organization")
        assert not request.headers.get("openai-project")
        assert json.loads(request.content) == {
            "model": "synthetic-embedding",
            "input": [expected],
            "encoding_format": "float",
        }
        assert request.extensions["timeout"] == {
            "connect": 15.0,
            "read": 15.0,
            "write": 15.0,
            "pool": 15.0,
        }
    assert len({id(transport) for transport in wire.transports}) == 2
    assert wire.closed == wire.transports


@pytest.mark.parametrize(
    "headers",
    [
        "Authorization: Bearer SYNTHETIC_PRIVATE_CONTENT",
        "Host: synthetic-exfil.invalid",
        "X-Synthetic: SYNTHETIC_PRIVATE_CONTENT",
        " \n",
    ],
)
async def test_custom_header_environment_fails_before_client_creation(
    monkeypatch: pytest.MonkeyPatch, wire: Wire, headers: str
) -> None:
    monkeypatch.setenv("OPENAI_CUSTOM_HEADERS", headers)
    with pytest.raises(CoreError) as error:
        await LocalEmbedding(profile()).embed(("synthetic",))
    assert_failed(error.value)
    assert wire.requests == []
    assert wire.closed == []


async def test_empty_custom_header_environment_preserves_local_request(
    monkeypatch: pytest.MonkeyPatch, wire: Wire
) -> None:
    monkeypatch.setenv("OPENAI_CUSTOM_HEADERS", "")
    assert await LocalEmbedding(profile()).embed(("synthetic",)) == ((1.0, 2.0),)
    assert len(wire.requests) == 1
    assert wire.requests[0].headers["authorization"] == "Bearer local-no-auth"
    assert wire.closed == wire.transports


async def test_valid_response_reorders_by_index_without_normalizing_vectors(wire: Wire) -> None:
    wire.body["data"] = [
        {"object": "embedding", "index": 1, "embedding": [0, 4]},
        {"object": "embedding", "index": 0, "embedding": [3, 0]},
    ]
    assert await LocalEmbedding(profile()).embed(("query", "candidate")) == (
        (3.0, 0.0),
        (0.0, 4.0),
    )
    assert len(wire.requests) == 1
    assert wire.closed == wire.transports


async def test_empty_input_returns_empty_without_transport(wire: Wire) -> None:
    assert await LocalEmbedding(profile()).embed(()) == ()
    assert await LocalEmbedding(profile(enabled=False)).embed(()) == ()
    assert wire.requests == []


@pytest.mark.parametrize(
    "texts",
    [
        ("",),
        (" \t\n",),
        ("synthetic", ""),
        ["synthetic"],
        "synthetic",
        (None,),
        (True,),
        ("synthetic",) * 1002,
        ("あ" * 87723,),
        ("あ" * 50000, "あ" * 50000),
        ("\ud800",),
        ("password=SYNTHETIC_PRIVATE_CONTENT",),
        ('{"ambiguous": "one", "ambiguous": "two"}',),
        ('{"truncated":',),
    ],
)
async def test_invalid_or_secret_input_fails_before_transport(wire: Wire, texts: Any) -> None:
    with pytest.raises(CoreError) as error:
        await LocalEmbedding(profile()).embed(texts)
    assert_failed(error.value)
    assert wire.requests == []


async def test_disabled_profile_never_sends(wire: Wire) -> None:
    with pytest.raises(CoreError) as error:
        await LocalEmbedding(profile(enabled=False)).embed(("synthetic",))
    assert_failed(error.value)
    assert wire.requests == []


@pytest.mark.parametrize("boundary", ["count", "utf8"])
async def test_maximum_input_boundaries_are_accepted(wire: Wire, boundary: str) -> None:
    texts = ("synthetic",) * 1001 if boundary == "count" else ("あ" * 87722 + "ab",)
    wire.body["data"] = [
        {"object": "embedding", "index": index, "embedding": [1.0, 2.0]}
        for index in range(len(texts))
    ]
    assert len(await LocalEmbedding(profile()).embed(texts)) == len(texts)
    assert len(wire.requests) == 1
    assert wire.closed == wire.transports


@pytest.mark.parametrize(
    "changes",
    [
        {"object": "embedding"},
        {"model": "other-model"},
        {"model": None},
        {"data": []},
        {"data": None},
        {"data": {"object": "embedding", "index": 0, "embedding": [1.0, 2.0]}},
        {"data": [{"object": "other", "index": 0, "embedding": [1.0, 2.0]}]},
        {"data": [{"object": "embedding", "index": 0, "embedding": []}]},
        {"data": [{"object": "embedding", "index": 0, "embedding": [1.0]}]},
        {"data": [{"object": "embedding", "index": 0, "embedding": [1.0, 2.0, 3.0]}]},
        {"data": [{"object": "embedding", "index": 0, "embedding": [0.0, 0.0]}]},
        {"data": [{"object": "embedding", "index": 0, "embedding": [True, 2.0]}]},
        {"data": [{"object": "embedding", "index": 0, "embedding": ["1.0", 2.0]}]},
        {"data": [{"object": "embedding", "index": 0, "embedding": [None, 2.0]}]},
        {"data": [{"object": "embedding", "index": 0, "embedding": [float("nan"), 2.0]}]},
        {"data": [{"object": "embedding", "index": 0, "embedding": [float("inf"), 2.0]}]},
        {"data": [{"object": "embedding", "index": 0, "embedding": "AQID"}]},
        {"data": [{"object": "embedding", "index": 1, "embedding": [1.0, 2.0]}]},
        {"data": [{"object": "embedding", "index": -1, "embedding": [1.0, 2.0]}]},
        {"data": [{"object": "embedding", "index": True, "embedding": [1.0, 2.0]}]},
        {"data": [{"object": "embedding", "index": "0", "embedding": [1.0, 2.0]}]},
        {"data": [{"object": "embedding", "embedding": [1.0, 2.0]}]},
        {"data": [{"object": "embedding", "index": 0}]},
    ],
)
async def test_invalid_sdk_response_is_rejected(wire: Wire, changes: dict[str, Any]) -> None:
    wire.body.update(changes)
    with pytest.raises(CoreError) as error:
        await LocalEmbedding(profile()).embed(("SYNTHETIC_PRIVATE_CONTENT",))
    assert_failed(error.value)
    assert len(wire.requests) == 1
    assert wire.closed == wire.transports


@pytest.mark.parametrize("mode", ["duplicate", "missing", "extra"])
async def test_response_indexes_exactly_cover_all_inputs(wire: Wire, mode: str) -> None:
    indexes = {"duplicate": [0, 0], "missing": [0], "extra": [0, 1, 2]}[mode]
    wire.body["data"] = [
        {"object": "embedding", "index": index, "embedding": [1.0, 2.0]} for index in indexes
    ]
    with pytest.raises(CoreError) as error:
        await LocalEmbedding(profile()).embed(("synthetic query", "synthetic candidate"))
    assert_failed(error.value)
    assert len(wire.requests) == 1
    assert wire.closed == wire.transports


@pytest.mark.parametrize("raw", [b"null", b"[]", b"{}", b"{malformed"])
async def test_malformed_response_fails_closed(wire: Wire, raw: bytes) -> None:
    wire.raw = raw
    with pytest.raises(CoreError) as error:
        await LocalEmbedding(profile()).embed(("synthetic",))
    assert_failed(error.value)
    assert len(wire.requests) == 1
    assert wire.closed == wire.transports


async def test_invalid_response_does_not_leak_through_serializer_warnings(wire: Wire) -> None:
    wire.body["data"][0]["embedding"] = ["SYNTHETIC_PRIVATE_CONTENT", 2.0]
    with warnings.catch_warnings(record=True) as observed:
        warnings.simplefilter("always")
        with pytest.raises(CoreError) as error:
            await LocalEmbedding(profile()).embed(("synthetic",))
    assert_failed(error.value)
    assert all("SYNTHETIC_PRIVATE_CONTENT" not in str(item.message) for item in observed)
    assert len(wire.requests) == 1
    assert wire.closed == wire.transports


@pytest.mark.parametrize("status", [302, 307, 308, 400, 401, 429, 500, 503])
async def test_redirect_and_http_errors_never_retry_or_expose_content(
    wire: Wire, status: int
) -> None:
    wire.status = status
    wire.body = {"error": {"message": "SYNTHETIC_PRIVATE_CONTENT"}}
    with pytest.raises(CoreError) as error:
        await LocalEmbedding(profile()).embed(("SYNTHETIC_PRIVATE_CONTENT",))
    assert_failed(error.value)
    assert len(wire.requests) == 1
    assert str(wire.requests[0].url) == "http://127.0.0.1:18082/v1/embeddings"
    assert wire.closed == wire.transports


@pytest.mark.parametrize("timed_out", [False, True])
async def test_transport_errors_are_sanitized_and_closed(wire: Wire, timed_out: bool) -> None:
    wire.exception = (
        httpx.ReadTimeout("SYNTHETIC_PRIVATE_CONTENT")
        if timed_out
        else httpx.ConnectError("SYNTHETIC_PRIVATE_CONTENT")
    )
    with pytest.raises(CoreError) as error:
        await LocalEmbedding(profile()).embed(("synthetic",))
    assert_failed(error.value, timeout=timed_out)
    assert len(wire.requests) == 1
    assert wire.closed == wire.transports


async def test_wall_clock_deadline_cancels_request_and_closes_transport(wire: Wire) -> None:
    wire.block = True
    with pytest.raises(CoreError) as error:
        await LocalEmbedding(profile(timeout_seconds=0.5)).embed(("synthetic",))
    assert_failed(error.value, timeout=True)
    assert len(wire.requests) == 1
    assert wire.closed == wire.transports


async def test_asyncio_cancellation_propagates_and_closes_transport(wire: Wire) -> None:
    wire.block = True
    task = asyncio.create_task(LocalEmbedding(profile()).embed(("synthetic",)))
    await asyncio.wait_for(wire.waiting.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(wire.requests) == 1
    assert wire.closed == wire.transports


async def test_anyio_scope_cancellation_propagates_and_shields_cleanup(wire: Wire) -> None:
    wire.block = True
    with anyio.CancelScope() as scope:

        async def cancel_when_sending() -> None:
            await wire.waiting.wait()
            scope.cancel()

        task = asyncio.create_task(cancel_when_sending())
        try:
            await LocalEmbedding(profile()).embed(("synthetic",))
            pytest.fail("Cancellation must propagate to its owning scope")
        finally:
            await task
    assert scope.cancelled_caught
    assert len(wire.requests) == 1
    assert wire.closed == wire.transports
