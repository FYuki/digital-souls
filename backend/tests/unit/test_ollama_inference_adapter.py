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
