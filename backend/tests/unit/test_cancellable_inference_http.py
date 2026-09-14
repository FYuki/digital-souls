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
