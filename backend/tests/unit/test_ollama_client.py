from unittest.mock import MagicMock, patch
import asyncio

import httpx
import pytest


def _mock_response(content: str) -> MagicMock:
    response = MagicMock()
    response.json.return_value = {
        "model": "gemma4:e4b",
        "message": {"role": "assistant", "content": content},
        "done": True,
    }
    response.raise_for_status.return_value = None
    return response


def _built_prompt():
    from tests.prompt_test_support import prompt_build_input, prompt_builder

    return prompt_builder().build(prompt_build_input())


def _ollama_client():
    from app.llm.ollama_client import OllamaClient

    return OllamaClient(model_name="gemma4:e4b", context_tokens=8192)


_PATCH_HTTPX_POST = "app.llm.ollama_client.httpx.post"


class TestOllamaClientGenerate:
    def test_sends_injected_model_context_and_generation_limit(self):
        from app.llm.ollama_client import OllamaClient

        with patch(_PATCH_HTTPX_POST, return_value=_mock_response("ok")) as mock_post:
            OllamaClient(
                model_name="custom-chat:9b",
                context_tokens=12288,
            ).generate(_built_prompt(), max_output_tokens=1536)

        payload = mock_post.call_args.kwargs["json"]
        assert payload["model"] == "custom-chat:9b"
        assert payload["options"] == {
            "num_ctx": 12288,
            "num_predict": 1536,
        }

    def test_token_count_uses_same_injected_model_and_context(self):
        from app.llm.ollama_client import OllamaClient
        from app.prompting import PromptMessage, PromptRole

        response = _mock_response("ok")
        response.json.return_value["prompt_eval_count"] = 7
        with patch(_PATCH_HTTPX_POST, return_value=response) as mock_post:
            result = OllamaClient(
                model_name="custom-chat:9b",
                context_tokens=12288,
            ).count_input_tokens((PromptMessage(PromptRole.USER, "hello"),))

        assert result == 7
        payload = mock_post.call_args.kwargs["json"]
        assert payload["model"] == "custom-chat:9b"
        assert payload["options"] == {
            "num_ctx": 12288,
            "num_predict": 1,
        }

    def test_sends_built_prompt_messages_without_reassembling_them(self):
        from app.llm.ollama_client import OllamaClient

        built_prompt = _built_prompt()
        expected_messages = [
            {"role": message.role.value, "content": message.content}
            for message in built_prompt.messages
        ]

        with patch(_PATCH_HTTPX_POST, return_value=_mock_response("ok")) as mock_post:
            _ollama_client().generate(built_prompt, max_output_tokens=512)

        payload = mock_post.call_args.kwargs["json"]
        assert payload["messages"] == expected_messages

    def test_sends_post_to_api_chat_path(self):
        from app.llm.ollama_client import OllamaClient

        with patch(_PATCH_HTTPX_POST, return_value=_mock_response("ok")) as mock_post:
            _ollama_client().generate(_built_prompt(), max_output_tokens=512)

        called_url: str = mock_post.call_args.args[0]
        assert called_url.endswith("/api/chat")

    def test_uses_default_base_url_when_env_not_set(self, monkeypatch):
        monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)

        from app.llm.ollama_client import OllamaClient

        with patch(_PATCH_HTTPX_POST, return_value=_mock_response("ok")) as mock_post:
            _ollama_client().generate(_built_prompt(), max_output_tokens=512)

        called_url: str = mock_post.call_args.args[0]
        assert called_url.startswith("http://localhost:11434")

    def test_uses_custom_base_url_from_env(self, monkeypatch):
        monkeypatch.setenv("OLLAMA_BASE_URL", "http://custom-host:9999")

        from app.llm.ollama_client import OllamaClient

        with patch(_PATCH_HTTPX_POST, return_value=_mock_response("ok")) as mock_post:
            _ollama_client().generate(_built_prompt(), max_output_tokens=512)

        called_url: str = mock_post.call_args.args[0]
        assert called_url.startswith("http://custom-host:9999")

    def test_payload_uses_injected_model(self):
        with patch(_PATCH_HTTPX_POST, return_value=_mock_response("ok")) as mock_post:
            _ollama_client().generate(_built_prompt(), max_output_tokens=512)

        payload = mock_post.call_args.kwargs["json"]
        assert payload["model"] == "gemma4:e4b"

    def test_payload_disables_streaming(self):
        from app.llm.ollama_client import OllamaClient

        with patch(_PATCH_HTTPX_POST, return_value=_mock_response("ok")) as mock_post:
            _ollama_client().generate(_built_prompt(), max_output_tokens=512)

        payload = mock_post.call_args.kwargs["json"]
        assert payload["stream"] is False

    def test_returns_message_content_from_ollama_response(self):
        from app.llm.ollama_client import OllamaClient

        expected = "光織です。よろしくお願いします。"
        with patch(_PATCH_HTTPX_POST, return_value=_mock_response(expected)):
            result = _ollama_client().generate(
                _built_prompt(), max_output_tokens=512
            )

        assert result == expected

    def test_passes_explicit_timeout_to_httpx_post(self):
        from app.llm.ollama_client import OllamaClient

        with patch(_PATCH_HTTPX_POST, return_value=_mock_response("ok")) as mock_post:
            _ollama_client().generate(_built_prompt(), max_output_tokens=512)

        timeout = mock_post.call_args.kwargs["timeout"]
        assert isinstance(timeout, httpx.Timeout)
        assert timeout.read == 30.0

    def test_raises_http_status_error_before_reading_response_body(self):
        from app.llm.ollama_client import OllamaClient

        response = _mock_response("should not be read")
        request = httpx.Request("POST", "http://localhost:11434/api/chat")
        response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "server error",
            request=request,
            response=httpx.Response(500, request=request),
        )

        with patch(_PATCH_HTTPX_POST, return_value=response):
            with pytest.raises(httpx.HTTPStatusError):
                _ollama_client().generate(
                    _built_prompt(), max_output_tokens=512
                )

        response.json.assert_not_called()


class _AsyncStreamResponse:
    def __init__(self, lines: list[str]) -> None:
        self.lines = lines
        self.closed = False

    def raise_for_status(self) -> None:
        return None

    async def aiter_lines(self):
        try:
            for line in self.lines:
                yield line
                await asyncio.sleep(0)
        finally:
            self.closed = True


class _AsyncContext:
    def __init__(self, value) -> None:
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *_args):
        self.value.closed = True


class _AsyncClient:
    def __init__(self, response: _AsyncStreamResponse, calls: list[dict]) -> None:
        self.response = response
        self.calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    def stream(self, method: str, url: str, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        return _AsyncContext(self.response)


def test_ollama_stream_emits_provider_independent_text_deltas(monkeypatch) -> None:
    from app.llm import ollama_client

    response = _AsyncStreamResponse([
        '{"message":{"content":"光"},"done":false}',
        '{"message":{"content":"織"},"done":false}',
        '{"message":{"content":""},"done":true,"done_reason":"stop"}',
    ])
    calls: list[dict] = []
    monkeypatch.setattr(
        ollama_client.httpx,
        "AsyncClient",
        lambda **_kwargs: _AsyncClient(response, calls),
    )

    async def exercise() -> None:
        deltas = [
            delta
            async for delta in _ollama_client().stream_generate(
                _built_prompt(), max_output_tokens=32
            )
        ]
        assert deltas == ["光", "織"]

    asyncio.run(exercise())
    assert calls[0]["json"]["stream"] is True
    assert response.closed is True


def test_ollama_stream_uses_configured_timeout(monkeypatch) -> None:
    from app.llm import ollama_client

    response = _AsyncStreamResponse([
        '{"message":{"content":"ok"},"done":false}',
        '{"message":{"content":""},"done":true,"done_reason":"stop"}',
    ])
    constructor_calls: list[dict] = []

    def async_client(**kwargs):
        constructor_calls.append(kwargs)
        return _AsyncClient(response, [])

    monkeypatch.setattr(ollama_client.httpx, "AsyncClient", async_client)

    async def exercise() -> None:
        assert [
            delta
            async for delta in _ollama_client().stream_generate(
                _built_prompt(), max_output_tokens=32
            )
        ] == ["ok"]

    asyncio.run(exercise())
    timeout = constructor_calls[0]["timeout"]
    assert isinstance(timeout, httpx.Timeout)
    assert timeout.read == 30.0


@pytest.mark.parametrize(
    ("lines", "message"),
    [
        (["{\"message\":{\"content\":\"途中\"},\"done\":false}"], "terminal"),
        (["{\"message\":{\"content\":\"\"},\"done\":true}"], "empty"),
    ],
)
def test_ollama_stream_rejects_incomplete_or_empty_response(
    monkeypatch, lines: list[str], message: str
) -> None:
    from app.llm import ollama_client

    response = _AsyncStreamResponse(lines)
    monkeypatch.setattr(
        ollama_client.httpx,
        "AsyncClient",
        lambda **_kwargs: _AsyncClient(response, []),
    )

    async def exercise() -> None:
        with pytest.raises(ValueError, match=message):
            _ = [
                delta
                async for delta in _ollama_client().stream_generate(
                    _built_prompt(), max_output_tokens=32
                )
            ]

    asyncio.run(exercise())
    assert response.closed is True


def test_ollama_stream_cancellation_closes_http_stream(monkeypatch) -> None:
    from app.llm import ollama_client

    class BlockingResponse(_AsyncStreamResponse):
        def __init__(self) -> None:
            super().__init__([])
            self.blocked = asyncio.Event()

        async def aiter_lines(self):
            try:
                yield '{"message":{"content":"最初"},"done":false}'
                self.blocked.set()
                await asyncio.Event().wait()
            finally:
                self.closed = True

    response = BlockingResponse()
    monkeypatch.setattr(
        ollama_client.httpx,
        "AsyncClient",
        lambda **_kwargs: _AsyncClient(response, []),
    )

    async def exercise() -> None:
        async def consume() -> None:
            _ = [
                delta
                async for delta in _ollama_client().stream_generate(
                    _built_prompt(), max_output_tokens=32
                )
            ]

        task = asyncio.create_task(consume())
        await asyncio.wait_for(response.blocked.wait(), timeout=0.5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(exercise())
    assert response.closed is True


@pytest.mark.parametrize(
    "line",
    [
        "not-json",
        '{"message":{"content":"text"}}',
        '{"message":{"content":1},"done":false}',
    ],
)
def test_ollama_stream_rejects_malformed_chunks(monkeypatch, line: str) -> None:
    from app.llm import ollama_client

    response = _AsyncStreamResponse([line])
    monkeypatch.setattr(
        ollama_client.httpx,
        "AsyncClient",
        lambda **_kwargs: _AsyncClient(response, []),
    )

    async def exercise() -> None:
        with pytest.raises(ValueError):
            _ = [
                delta
                async for delta in _ollama_client().stream_generate(
                    _built_prompt(), max_output_tokens=32
                )
            ]

    asyncio.run(exercise())
