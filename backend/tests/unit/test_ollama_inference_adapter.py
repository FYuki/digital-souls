from __future__ import annotations

import asyncio
import base64
from collections.abc import AsyncIterator, Mapping
from unittest.mock import MagicMock

import httpx
import pytest

from app.inference.adapters.ollama import OllamaAdapter
from app.inference.contracts import (
    EmbeddingRequest,
    InferenceCapability,
    InferenceImagePart,
    InferenceMessage,
    InferenceTextPart,
    StructuredGenerationRequest,
    TextGenerationRequest,
    TokenEstimateAccuracy,
    TokenEstimateRequest,
)
from app.inference.errors import InferenceError, InferenceErrorCategory


def _response(body: object, *, status_code: int = 200) -> MagicMock:
    response = MagicMock(spec=httpx.Response)
    response.status_code = status_code
    response.json.return_value = body
    response.raise_for_status.return_value = None
    return response


def _text_request() -> TextGenerationRequest:
    return TextGenerationRequest(
        messages=(InferenceMessage("system", "system"), InferenceMessage("user", "hi")),
        model_id="gemma4:e4b",
        options={"temperature": 0.2},
        max_input_tokens=7168,
        max_output_tokens=1024,
        timeout_seconds=4.0,
    )


def test_probe_uses_model_metadata_without_generation() -> None:
    client = MagicMock(spec=httpx.Client)
    client.post.return_value = _response(
        {
            "digest": "sha256:" + "a" * 64,
            "capabilities": ["completion", "vision"],
        }
    )
    adapter = OllamaAdapter(base_url="http://127.0.0.1:11434", http_client=client)

    result = adapter.probe("gemma4:e4b", timeout_seconds=3.0)

    call = client.post.call_args
    assert call.args[0] == "http://127.0.0.1:11434/api/show"
    assert call.kwargs["json"] == {"model": "gemma4:e4b"}
    assert result.capabilities == frozenset({InferenceCapability.IMAGE_INPUT})


def test_probe_distinguishes_known_non_vision_model() -> None:
    client = MagicMock(spec=httpx.Client)
    client.post.return_value = _response(
        {
            "digest": "sha256:" + "b" * 64,
            "capabilities": ["completion"],
        }
    )

    result = OllamaAdapter(
        base_url="http://127.0.0.1:11434", http_client=client
    ).probe("text-only:latest", timeout_seconds=3.0)

    assert result.capabilities == frozenset()


def test_multimodal_mapping_base64_encodes_only_inside_adapter() -> None:
    client = MagicMock(spec=httpx.Client)
    client.post.return_value = _response({"message": {"content": '{"ok":true}'}})
    adapter = OllamaAdapter(base_url="http://127.0.0.1:11434", http_client=client)
    image = b"synthetic-image"
    request = StructuredGenerationRequest(
        messages=(
            InferenceMessage(
                "user",
                (
                    InferenceTextPart("画面を読んで"),
                    InferenceImagePart(image, "image/png", 1, 1),
                ),
            ),
        ),
        model_id="gemma4:e4b",
        options={},
        max_input_tokens=7168,
        max_output_tokens=1024,
        timeout_seconds=3.0,
        response_schema={"type": "object"},
    )

    adapter.generate_structured(request)

    message = client.post.call_args.kwargs["json"]["messages"][0]
    assert message == {
        "role": "user",
        "content": "画面を読んで",
        "images": [base64.b64encode(image).decode("ascii")],
    }


def test_multimodal_estimate_is_local_conservative_and_not_exact() -> None:
    client = MagicMock(spec=httpx.Client)
    adapter = OllamaAdapter(base_url="http://127.0.0.1:11434", http_client=client)

    result = adapter.estimate_input_tokens(
        TokenEstimateRequest(
            messages=(
                InferenceMessage(
                    "user",
                    (
                        InferenceTextPart("画面を読んで"),
                        InferenceImagePart(b"private", "image/png", 1, 1),
                    ),
                ),
            ),
            model_id="gemma4:e4b",
            options={},
            max_input_tokens=7168,
            timeout_seconds=3.0,
            response_schema={"type": "object"},
        )
    )

    assert result.count > 1_120
    assert result.accuracy is TokenEstimateAccuracy.ESTIMATED
    assert "1120_per_image" in result.method
    client.post.assert_not_called()


def test_generate_text_applies_target_limits_and_returns_provider_usage() -> None:
    client = MagicMock(spec=httpx.Client)
    client.post.return_value = _response(
        {
            "message": {"content": "reply"},
            "prompt_eval_count": 12,
            "eval_count": 4,
        }
    )
    adapter = OllamaAdapter(base_url="http://127.0.0.1:11434", http_client=client)

    result = adapter.generate_text(_text_request())

    assert result.text == "reply"
    assert result.usage is not None
    assert result.usage.total_tokens == 16
    payload = client.post.call_args.kwargs["json"]
    assert payload["model"] == "gemma4:e4b"
    assert payload["options"] == {
        "temperature": 0.2,
        "num_ctx": 8192,
        "num_predict": 1024,
    }
    assert "format" not in payload


def test_structured_generation_forwards_schema_without_domain_types() -> None:
    client = MagicMock(spec=httpx.Client)
    client.post.return_value = _response({"message": {"content": '{"ok":true}'}})
    adapter = OllamaAdapter(base_url="http://127.0.0.1:11434", http_client=client)
    schema: Mapping[str, object] = {
        "type": "object",
        "properties": {"ok": {"type": "boolean"}},
        "required": ["ok"],
    }
    request = StructuredGenerationRequest(
        **_text_request().__dict__,
        response_schema=schema,
    )

    adapter.generate_structured(request)

    payload = client.post.call_args.kwargs["json"]
    assert payload["format"] == schema
    assert payload["think"] is False


def test_structured_generation_removes_only_unsupported_grammar_constraints() -> None:
    client = MagicMock(spec=httpx.Client)
    client.post.return_value = _response({"message": {"content": '{"items":[]}'}})
    adapter = OllamaAdapter(base_url="http://127.0.0.1:11434", http_client=client)
    schema: Mapping[str, object] = {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "minItems": 1,
                "maxItems": 5,
                "items": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 200,
                    "enum": ["kept"],
                },
            }
        },
        "required": ["items"],
        "additionalProperties": False,
    }
    request = StructuredGenerationRequest(
        **_text_request().__dict__,
        response_schema=schema,
    )

    adapter.generate_structured(request)

    payload_schema = client.post.call_args.kwargs["json"]["format"]
    assert payload_schema == {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {"type": "string", "enum": ["kept"]},
            }
        },
        "required": ["items"],
        "additionalProperties": False,
    }
    assert schema["properties"] == {
        "items": {
            "type": "array",
            "minItems": 1,
            "maxItems": 5,
            "items": {
                "type": "string",
                "minLength": 1,
                "maxLength": 200,
                "enum": ["kept"],
            },
        }
    }


@pytest.mark.parametrize(
    ("status", "category", "retryable"),
    [
        (401, InferenceErrorCategory.AUTHENTICATION_FAILED, False),
        (403, InferenceErrorCategory.PERMISSION_DENIED, False),
        (404, InferenceErrorCategory.MODEL_NOT_FOUND, False),
        (429, InferenceErrorCategory.RATE_LIMITED, True),
        (503, InferenceErrorCategory.UNAVAILABLE, True),
    ],
)
def test_http_errors_are_normalized_without_raw_payload(
    status: int,
    category: InferenceErrorCategory,
    retryable: bool,
) -> None:
    client = MagicMock(spec=httpx.Client)
    request = httpx.Request("POST", "http://127.0.0.1:11434/api/chat")
    response = httpx.Response(status, request=request, text="private-response")
    client.post.return_value = response

    with pytest.raises(InferenceError) as exc_info:
        OllamaAdapter(
            base_url="http://127.0.0.1:11434", http_client=client
        ).generate_text(_text_request())

    assert exc_info.value.category is category
    assert exc_info.value.retryable is retryable
    assert "private-response" not in str(exc_info.value)


def test_estimate_includes_structured_schema_with_conservative_margin() -> None:
    client = MagicMock(spec=httpx.Client)
    client.post.return_value = _response(
        {"message": {"content": "ignored"}, "prompt_eval_count": 10}
    )
    adapter = OllamaAdapter(base_url="http://127.0.0.1:11434", http_client=client)

    result = adapter.estimate_input_tokens(
        TokenEstimateRequest(
            messages=(InferenceMessage("user", "hi"),),
            model_id="gemma4:e4b",
            options={},
            max_input_tokens=1024,
            timeout_seconds=2.0,
            response_schema={"type": "object"},
        )
    )

    assert result.count > 10
    assert result.accuracy is TokenEstimateAccuracy.ESTIMATED
    assert "margin10pct" in result.method


def test_embedding_validates_batch_shape_and_usage() -> None:
    client = MagicMock(spec=httpx.Client)
    client.post.return_value = _response(
        {
            "embeddings": [[0.1, 0.2], [0.3, 0.4]],
            "prompt_eval_count": 8,
        }
    )
    adapter = OllamaAdapter(base_url="http://127.0.0.1:11434", http_client=client)
    request = EmbeddingRequest(
        inputs=("first", "second"),
        model_id="nomic-embed-text:latest",
        options={},
        max_input_tokens=8192,
        timeout_seconds=3.0,
    )

    result = adapter.embed(request)

    assert result.vectors == ((0.1, 0.2), (0.3, 0.4))
    assert result.usage is not None
    assert result.usage.input_tokens == 8
    assert client.post.call_args.args[0].endswith("/api/embed")


@pytest.mark.parametrize(
    "inputs",
    [(), ("",)],
)
def test_embedding_rejects_empty_input_before_http(
    inputs: tuple[str, ...],
) -> None:
    client = MagicMock(spec=httpx.Client)
    adapter = OllamaAdapter(base_url="http://127.0.0.1:11434", http_client=client)

    with pytest.raises(InferenceError) as exc_info:
        adapter.embed(
            EmbeddingRequest(
                inputs=inputs,
                model_id="nomic-embed-text:latest",
                options={},
                max_input_tokens=8192,
                timeout_seconds=3.0,
            )
        )

    assert exc_info.value.category is InferenceErrorCategory.INVALID_REQUEST
    client.post.assert_not_called()


class _StreamResponse:
    def __init__(self, lines: tuple[str, ...]) -> None:
        self._lines = lines
        self.closed = False

    async def __aenter__(self) -> "_StreamResponse":
        return self

    async def __aexit__(self, *_args: object) -> None:
        self.closed = True

    def raise_for_status(self) -> None:
        return None

    async def aiter_lines(self) -> AsyncIterator[str]:
        for line in self._lines:
            yield line


class _AsyncClient:
    def __init__(self, response: _StreamResponse) -> None:
        self._response = response

    async def __aenter__(self) -> "_AsyncClient":
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    def stream(self, *_args: object, **_kwargs: object) -> _StreamResponse:
        return self._response


def test_streaming_emits_each_delta_once_and_closes_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = _StreamResponse(
        (
            '{"message":{"content":"a"},"done":false}',
            '{"message":{"content":"b"},"done":false}',
            '{"message":{"content":""},"done":true}',
        )
    )
    monkeypatch.setattr(
        "app.inference.adapters.ollama.httpx.AsyncClient",
        lambda **_kwargs: _AsyncClient(response),
    )
    adapter = OllamaAdapter(
        base_url="http://127.0.0.1:11434",
        http_client=MagicMock(spec=httpx.Client),
    )

    async def consume() -> list[str]:
        return [delta async for delta in adapter.stream_text(_text_request())]

    assert asyncio.run(consume()) == ["a", "b"]
    assert response.closed is True


def test_token_count_uses_generation_context_without_generating_full_reply() -> None:
    client = MagicMock(spec=httpx.Client)
    client.post.return_value = _response({
        "message": {"content": "ok"}, "prompt_eval_count": 99,
    })
    adapter = OllamaAdapter(base_url="http://127.0.0.1:11434", http_client=client)
    request = _text_request()
    estimate = adapter.estimate_input_tokens(TokenEstimateRequest(
        messages=request.messages, model_id=request.model_id,
        options=request.options, max_input_tokens=request.max_input_tokens,
        timeout_seconds=request.timeout_seconds,
        context_window_tokens=request.max_input_tokens + request.max_output_tokens,
    ))
    estimate_payload = client.post.call_args.kwargs["json"]
    adapter.generate_text(request)
    generation_payload = client.post.call_args.kwargs["json"]

    assert estimate.count == 99
    assert estimate.accuracy is TokenEstimateAccuracy.EXACT
    assert estimate_payload["messages"] == generation_payload["messages"]
    assert estimate_payload["model"] == generation_payload["model"]
    assert estimate_payload["options"] == {"temperature": 0.2, "num_ctx": 8192, "num_predict": 1}
    assert generation_payload["options"] == {"temperature": 0.2, "num_ctx": 8192, "num_predict": 1024}


@pytest.mark.parametrize("context_window", [7168, 0, -1, True])
def test_token_count_rejects_context_smaller_than_input_budget(context_window: int) -> None:
    client = MagicMock(spec=httpx.Client)
    adapter = OllamaAdapter(base_url="http://127.0.0.1:11434", http_client=client)
    request = _text_request()
    with pytest.raises(InferenceError) as error:
        adapter.estimate_input_tokens(TokenEstimateRequest(
            messages=request.messages, model_id=request.model_id,
            options=request.options, max_input_tokens=request.max_input_tokens,
            timeout_seconds=request.timeout_seconds,
            context_window_tokens=context_window,
        ))
    assert error.value.category is InferenceErrorCategory.INVALID_REQUEST
    client.post.assert_not_called()


@pytest.mark.parametrize("thinking", [False, True])
def test_thinking_option_is_a_top_level_chat_field(thinking: bool) -> None:
    from dataclasses import replace

    request = replace(_text_request(), options={"temperature": 0.2, "think": thinking})
    payload = OllamaAdapter._chat_payload(request, stream=True)
    assert payload["think"] is thinking
    assert payload["options"] == {"temperature": 0.2, "num_ctx": 8192, "num_predict": 1024}
    assert "think" not in OllamaAdapter._chat_payload(_text_request(), stream=True)
    # 構造化出力の既存の思考無効化は維持する。
    assert OllamaAdapter._chat_payload(request, stream=False, response_schema={"type": "object"})["think"] is False


def test_exact_count_cache_checks_model_digest_and_keeps_request_variants_separate() -> None:
    from dataclasses import replace

    digest = "a" * 64
    chat_calls = 0
    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal chat_calls
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "gemma4:e4b", "digest": digest}]})
        chat_calls += 1
        return httpx.Response(200, json={"prompt_eval_count": 10 + chat_calls})
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        adapter = OllamaAdapter(base_url="http://127.0.0.1:11434", http_client=client)
        request = TokenEstimateRequest(
            messages=(InferenceMessage("user", "private input"),), model_id="gemma4:e4b",
            options={}, max_input_tokens=7168, timeout_seconds=3,
            context_window_tokens=8192, allow_cached_exact_result=True,
        )
        first = adapter.estimate_input_tokens(request)
        repeated = adapter.estimate_input_tokens(request)
        assert first.count == repeated.count == 11
        assert first.external_request_count == 3
        assert repeated.external_request_count == 1
        assert chat_calls == 1
        assert "private input" not in repr(adapter._token_counts.__dict__)

        digest = "b" * 64
        assert adapter.estimate_input_tokens(request).count == 12
        assert adapter.estimate_input_tokens(replace(request, options={"think": False})).count == 13
        assert adapter.estimate_input_tokens(replace(request, context_window_tokens=16384)).count == 14
        assert adapter.estimate_input_tokens(replace(request, messages=(InferenceMessage("user", "changed"),))).count == 15
        adapter.close()
        assert adapter.estimate_input_tokens(request).count == 16


def test_model_change_during_count_is_not_cached_and_metadata_failure_bypasses_cache() -> None:
    digest = "a" * 64
    broken_metadata = False
    chat_calls = 0
    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal digest, chat_calls
        if request.url.path == "/api/tags":
            if broken_metadata:
                return httpx.Response(503)
            return httpx.Response(200, json={"models": [{"name": "gemma4:e4b", "digest": digest}]})
        chat_calls += 1
        digest = "b" * 64
        return httpx.Response(200, json={"prompt_eval_count": 10 + chat_calls})
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        adapter = OllamaAdapter(base_url="http://127.0.0.1:11434", http_client=client)
        request = TokenEstimateRequest(
            messages=(InferenceMessage("user", "private"),), model_id="gemma4:e4b",
            options={}, max_input_tokens=7168, timeout_seconds=3,
            context_window_tokens=8192, allow_cached_exact_result=True,
        )
        adapter.estimate_input_tokens(request)
        digest = "a" * 64
        adapter.estimate_input_tokens(request)
        assert chat_calls == 2
        adapter.estimate_input_tokens(request)
        assert chat_calls == 3
        adapter.estimate_input_tokens(request)
        assert chat_calls == 3
        broken_metadata = True
        assert adapter.estimate_input_tokens(request).count == 14
        assert chat_calls == 4


def test_exact_count_cache_is_bounded_and_does_not_share_keys_between_instances() -> None:
    from app.inference.token_estimate_cache import ExactTokenEstimateCache

    cache = ExactTokenEstimateCache(capacity=2)
    keys = [cache.key(text) for text in (b"first", b"second", b"third")]
    assert ExactTokenEstimateCache().key(b"first") != keys[0]
    cache.put(keys[0], 1)
    cache.put(keys[1], 2)
    assert cache.get(keys[0]) == 1
    cache.put(keys[2], 3)
    assert cache.get(keys[1]) is None
    assert cache.get(keys[0]) == 1
