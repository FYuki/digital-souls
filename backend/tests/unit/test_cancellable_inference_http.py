"""実TCPで中断時の切断を確認する。LLMの品質試験ではない。"""

import asyncio
import json

import httpx
import pytest

from app.inference.adapters.ollama import OllamaAdapter
from app.inference.cancellation import cancellation_scope
from app.inference.contracts import (
    EmbeddingRequest, InferenceCancellationToken, InferenceMessage,
    StructuredGenerationRequest, TextGenerationRequest, TokenEstimateRequest,
)
from app.inference.errors import InferenceError, InferenceErrorCategory


@pytest.mark.anyio
@pytest.mark.parametrize("phase", ["generation", "estimate", "embedding", "metadata"])
async def test_cancellation_closes_only_the_background_http_and_foreground_can_continue(phase):
    started, disconnected = asyncio.Event(), asyncio.Event()
    requests = []

    async def handle(reader, writer):
        try:
            headers = await reader.readuntil(b"\r\n\r\n")
            length = next(int(line.split(b":", 1)[1]) for line in headers.split(b"\r\n")
                          if line.lower().startswith(b"content-length:"))
            requests.append(json.loads(await reader.readexactly(length)))
            if len(requests) == 1:
                started.set()
                assert await reader.read() == b""
                disconnected.set()
            else:
                body = json.dumps({"message": {"content": "ok"}, "prompt_eval_count": 3, "eval_count": 1}).encode()
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                             + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    client = httpx.Client(trust_env=False)
    adapter = OllamaAdapter(base_url=f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}", http_client=client)
    common = dict(messages=(InferenceMessage("user", "fixture"),), model_id="fixture", options={},
                  max_input_tokens=128, timeout_seconds=10)
    token = InferenceCancellationToken()
    calls = {
        "generation": lambda: adapter.generate_structured(StructuredGenerationRequest(
            **common, max_output_tokens=16, response_schema={"type": "object"})),
        "estimate": lambda: adapter.estimate_input_tokens(TokenEstimateRequest(**common)),
        "embedding": lambda: adapter.embed(EmbeddingRequest(inputs=("fixture",), model_id="fixture",
                                                            options={}, max_input_tokens=128, timeout_seconds=10)),
        "metadata": lambda: adapter.resolve_model_digest("fixture", timeout_seconds=10),
    }
    try:
        with cancellation_scope(token):
            pending = asyncio.create_task(asyncio.to_thread(calls[phase]))
        await asyncio.wait_for(started.wait(), 2)
        token.cancel()
        with pytest.raises(InferenceError) as error:
            await asyncio.wait_for(pending, 2)
        assert error.value.category is InferenceErrorCategory.CANCELLED
        await asyncio.wait_for(disconnected.wait(), 2)
        assert not client.is_closed
        result = await asyncio.to_thread(adapter.generate_text, TextGenerationRequest(**common, max_output_tokens=16))
        assert result.text == "ok"
        assert len(requests) == 2
        assert requests[1]["options"]["num_predict"] == 16
        assert "cancellation_token" not in requests[0]
    finally:
        client.close()
        server.close()
        await server.wait_closed()


@pytest.mark.anyio
@pytest.mark.parametrize("cancel_outer", [False, True])
async def test_explicit_router_token_reaches_http_and_preserves_outer_cancellation(cancel_outer):
    from app.inference.adapters.cancellable_http import post
    from app.inference.authorization import InferenceCaller
    from app.inference.contracts import InferenceTarget, ProviderTextResult
    from tests.unit.test_inference_core import _FakeAdapter, _router
    entered, closed = asyncio.Event(), asyncio.Event()
    async def response(request):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            closed.set()
    async_transport = httpx.MockTransport(response)
    client = httpx.Client(trust_env=False)
    class Adapter(_FakeAdapter):
        def generate_structured(self, request):
            post(client, "http://fixture/", json={}, timeout=httpx.Timeout(10),
                 async_client_factory=lambda: httpx.AsyncClient(transport=async_transport))
            return ProviderTextResult('{}')
    router = _router(Adapter())
    explicit, outer = InferenceCancellationToken(), InferenceCancellationToken()
    try:
        with cancellation_scope(outer):
            pending = asyncio.create_task(asyncio.to_thread(router.generate_structured,
                caller=InferenceCaller.MEMORY_EXTRACTION, target=InferenceTarget.MEMORY_EXTRACTION,
                messages=(InferenceMessage('user', 'fixture'),), response_schema={'type': 'object'},
                cancellation_token=explicit))
        # MockTransportのhandlerは別スレッドのloopで動くためeventはpollする。
        for _ in range(200):
            if entered.is_set():
                break
            await asyncio.sleep(.005)
        assert entered.is_set()
        (outer if cancel_outer else explicit).cancel()
        with pytest.raises(InferenceError) as error:
            await asyncio.wait_for(pending, 2)
        assert error.value.category is InferenceErrorCategory.CANCELLED
        assert closed.is_set()
        assert not client.is_closed
    finally:
        client.close()


@pytest.mark.parametrize('provider', ['ollama', 'openai'])
def test_scoped_adapters_use_injected_async_transport_auth_and_hooks(provider):
    from app.inference.adapters.openai_api import OpenAIAPIAdapter
    seen = []
    async def on_request(request):
        seen.append(request.headers.get('X-Fixture-Auth'))
    def response(request):
        if provider == 'ollama':
            return httpx.Response(200, json={'message': {'content': 'ok'}})
        return httpx.Response(200, json={'status': 'completed', 'output': [{'type': 'message', 'content': [
            {'type': 'output_text', 'text': 'ok'}]}]})
    def factory(seconds):
        return httpx.AsyncClient(transport=httpx.MockTransport(response),
            auth=lambda request: _authorize(request), event_hooks={'request': [on_request]}, timeout=seconds)
    def _authorize(request):
        request.headers['X-Fixture-Auth'] = 'injected'
        return request
    adapter = (OllamaAdapter(base_url='http://fixture', async_client_factory=factory) if provider == 'ollama'
               else OpenAIAPIAdapter(api_key='fixture', async_client_factory=factory))
    try:
        with cancellation_scope(InferenceCancellationToken()):
            result = adapter.generate_text(TextGenerationRequest(messages=(InferenceMessage('user', 'fixture'),),
                model_id='fixture', options={}, max_input_tokens=128, max_output_tokens=16, timeout_seconds=2))
        assert result.text == 'ok'
        assert seen == ['injected']
    finally:
        adapter.close()
