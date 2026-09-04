from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from typing import cast
from unittest.mock import MagicMock

import httpx
import pytest

from app.inference.adapters.openai_api import (
    OPENAI_API_BASE_URL,
    OpenAIAPIAdapter,
)
from app.inference.contracts import (
    EmbeddingRequest,
    InferenceMessage,
    StructuredGenerationRequest,
    TextGenerationRequest,
    TokenEstimateAccuracy,
    TokenEstimateRequest,
)
from app.inference.errors import InferenceError, InferenceErrorCategory


def _response(body: object, *, status_code: int = 200) -> MagicMock:
    response = MagicMock(spec=httpx.Response)
    response.status_code = status_code
    response.is_error = status_code >= 400
    response.json.return_value = body
    return response


def _text_request() -> TextGenerationRequest:
    return TextGenerationRequest(
        messages=(
            InferenceMessage("system", "system"),
            InferenceMessage("user", "hello"),
        ),
        model_id="gpt-5.6-sol",
        options={"reasoning_effort": "medium"},
        max_input_tokens=8_192,
        max_output_tokens=1_024,
        timeout_seconds=4.0,
    )


def test_probe_uses_non_billable_model_lookup() -> None:
    client = MagicMock(spec=httpx.Client)
    client.get.return_value = _response({"id": "organization/model"})
    adapter = OpenAIAPIAdapter(api_key="test-api-key", http_client=client)

    adapter.probe("organization/model", timeout_seconds=3.0)

    call = client.get.call_args
    assert call.args[0] == f"{OPENAI_API_BASE_URL}/models/organization%2Fmodel"
    assert call.kwargs["headers"]["Authorization"] == "Bearer test-api-key"
    assert client.post.call_count == 0


def test_generate_text_uses_official_endpoint_and_reports_usage() -> None:
    client = MagicMock(spec=httpx.Client)
    client.post.return_value = _response(
        {
            "status": "completed",
            "output_text": "reply",
            "usage": {
                "input_tokens": 12,
                "output_tokens": 4,
                "total_tokens": 16,
            },
        }
    )
    adapter = OpenAIAPIAdapter(api_key="test-api-key", http_client=client)

    result = adapter.generate_text(_text_request())

    assert result.text == "reply"
    assert result.usage is not None and result.usage.total_tokens == 16
    assert client.post.call_count == 1
    call = client.post.call_args
    assert call.args[0] == f"{OPENAI_API_BASE_URL}/responses"
    assert call.kwargs["headers"]["Authorization"] == "Bearer test-api-key"
    assert call.kwargs["json"] == {
        "model": "gpt-5.6-sol",
        "input": [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "hello"},
        ],
        "max_output_tokens": 1_024,
        "store": False,
        "reasoning": {"effort": "medium"},
    }


def test_structured_generation_forwards_json_schema() -> None:
    client = MagicMock(spec=httpx.Client)
    client.post.return_value = _response(
        {"status": "completed", "output_text": '{"ok":true}'}
    )
    adapter = OpenAIAPIAdapter(api_key="test-api-key", http_client=client)
    schema: Mapping[str, object] = {
        "type": "object",
        "properties": {"ok": {"type": "boolean"}},
        "required": ["ok"],
        "additionalProperties": False,
    }

    adapter.generate_structured(
        StructuredGenerationRequest(
            **_text_request().__dict__,
            response_schema=schema,
        )
    )

    assert client.post.call_args.kwargs["json"]["text"] == {
        "format": {
            "type": "json_schema",
            "name": "inference_response",
            "schema": schema,
            "strict": True,
        }
    }


@pytest.mark.parametrize(
    ("status", "category", "retryable"),
    [
        (400, InferenceErrorCategory.INVALID_REQUEST, False),
        (401, InferenceErrorCategory.AUTHENTICATION_FAILED, False),
        (403, InferenceErrorCategory.PERMISSION_DENIED, False),
        (404, InferenceErrorCategory.MODEL_NOT_FOUND, False),
        (429, InferenceErrorCategory.RATE_LIMITED, True),
        (503, InferenceErrorCategory.UNAVAILABLE, True),
    ],
)
def test_http_errors_are_normalized_without_raw_provider_details(
    status: int,
    category: InferenceErrorCategory,
    retryable: bool,
) -> None:
    client = MagicMock(spec=httpx.Client)
    client.post.return_value = _response(
        {"error": {"code": "synthetic", "message": "private-provider-value"}},
        status_code=status,
    )

    with pytest.raises(InferenceError) as exc_info:
        OpenAIAPIAdapter(api_key="test-api-key", http_client=client).generate_text(
            _text_request()
        )

    assert exc_info.value.category is category
    assert exc_info.value.retryable is retryable
    assert "private-provider-value" not in str(exc_info.value)
    assert "test-api-key" not in str(exc_info.value)


def test_model_not_found_error_code_on_bad_request_is_normalized() -> None:
    client = MagicMock(spec=httpx.Client)
    client.post.return_value = _response(
        {"error": {"code": "model_not_found", "message": "private-model"}},
        status_code=400,
    )

    with pytest.raises(InferenceError) as exc_info:
        OpenAIAPIAdapter(api_key="test-api-key", http_client=client).generate_text(
            _text_request()
        )

    assert exc_info.value.category is InferenceErrorCategory.MODEL_NOT_FOUND
    assert "private-model" not in str(exc_info.value)


@pytest.mark.parametrize(
    ("error", "category"),
    [
        (httpx.ReadTimeout("private-timeout"), InferenceErrorCategory.TIMEOUT),
        (
            httpx.ConnectError("private-connection"),
            InferenceErrorCategory.UNAVAILABLE,
        ),
    ],
)
def test_transport_errors_are_normalized_without_raw_details(
    error: Exception,
    category: InferenceErrorCategory,
) -> None:
    client = MagicMock(spec=httpx.Client)
    client.post.side_effect = error

    with pytest.raises(InferenceError) as exc_info:
        OpenAIAPIAdapter(api_key="test-api-key", http_client=client).generate_text(
            _text_request()
        )

    assert exc_info.value.category is category
    assert exc_info.value.retryable is True
    assert "private" not in str(exc_info.value)


@pytest.mark.parametrize(
    "body",
    [
        {"status": "completed"},
        {"status": "incomplete", "output_text": "partial-private-value"},
        {
            "status": "completed",
            "output_text": "reply",
            "usage": {"input_tokens": 2, "output_tokens": 3, "total_tokens": 99},
        },
    ],
)
def test_malformed_or_incomplete_response_is_rejected_without_payload(
    body: dict[str, object],
) -> None:
    client = MagicMock(spec=httpx.Client)
    client.post.return_value = _response(body)

    with pytest.raises(InferenceError) as exc_info:
        OpenAIAPIAdapter(api_key="test-api-key", http_client=client).generate_text(
            _text_request()
        )

    assert exc_info.value.category in {
        InferenceErrorCategory.INVALID_RESPONSE,
        InferenceErrorCategory.PROVIDER_ERROR,
    }
    assert "private-value" not in str(exc_info.value)


def test_embedding_orders_vectors_and_validates_usage() -> None:
    client = MagicMock(spec=httpx.Client)
    client.post.return_value = _response(
        {
            "data": [
                {"index": 1, "embedding": [0.3, 0.4]},
                {"index": 0, "embedding": [0.1, 0.2]},
            ],
            "usage": {"input_tokens": 8, "total_tokens": 8},
        }
    )
    adapter = OpenAIAPIAdapter(api_key="test-api-key", http_client=client)

    result = adapter.embed(
        EmbeddingRequest(
            inputs=("first", "second"),
            model_id="text-embedding-3-small",
            options={},
            max_input_tokens=8_192,
            timeout_seconds=3.0,
        )
    )

    assert result.vectors == ((0.1, 0.2), (0.3, 0.4))
    assert result.usage is not None and result.usage.input_tokens == 8
    assert client.post.call_args.args[0] == f"{OPENAI_API_BASE_URL}/embeddings"


def test_estimate_is_local_conservative_and_includes_schema() -> None:
    client = MagicMock(spec=httpx.Client)
    adapter = OpenAIAPIAdapter(api_key="test-api-key", http_client=client)
    base_request = TokenEstimateRequest(
        messages=(InferenceMessage("user", "hello"),),
        model_id="gpt-5.6-sol",
        options={},
        max_input_tokens=8_192,
        timeout_seconds=3.0,
    )

    without_schema = adapter.estimate_input_tokens(base_request)
    with_schema = adapter.estimate_input_tokens(
        TokenEstimateRequest(
            **{
                **base_request.__dict__,
                "response_schema": {
                    "type": "object",
                    "properties": {"ok": {"type": "boolean"}},
                },
            },
        )
    )

    assert with_schema.count > without_schema.count > 0
    assert with_schema.accuracy is TokenEstimateAccuracy.ESTIMATED
    assert "margin15pct" in with_schema.method
    client.post.assert_not_called()


class _StreamResponse:
    status_code = 200
    is_error = False

    def __init__(self, lines: tuple[str, ...]) -> None:
        self._lines = lines
        self.closed = False

    async def __aenter__(self) -> "_StreamResponse":
        return self

    async def __aexit__(self, *_args: object) -> None:
        self.closed = True

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


def test_streaming_emits_deltas_once_and_requires_completion() -> None:
    response = _StreamResponse(
        (
            "event: response.output_text.delta",
            'data: {"type":"response.output_text.delta","delta":"a"}',
            'data: {"type":"response.output_text.delta","delta":"b"}',
            'data: {"type":"response.completed","response":{}}',
        )
    )
    adapter = OpenAIAPIAdapter(
        api_key="test-api-key",
        http_client=MagicMock(spec=httpx.Client),
        async_client_factory=lambda _timeout: cast(
            httpx.AsyncClient, _AsyncClient(response)
        ),
    )

    async def consume() -> list[str]:
        return [delta async for delta in adapter.stream_text(_text_request())]

    assert asyncio.run(consume()) == ["a", "b"]
    assert response.closed is True


def test_streaming_rejects_partial_response_without_completion() -> None:
    response = _StreamResponse(
        ('data: {"type":"response.output_text.delta","delta":"partial"}',)
    )
    adapter = OpenAIAPIAdapter(
        api_key="test-api-key",
        http_client=MagicMock(spec=httpx.Client),
        async_client_factory=lambda _timeout: cast(
            httpx.AsyncClient, _AsyncClient(response)
        ),
    )

    async def consume() -> list[str]:
        return [delta async for delta in adapter.stream_text(_text_request())]

    with pytest.raises(InferenceError) as exc_info:
        asyncio.run(consume())

    assert exc_info.value.category is InferenceErrorCategory.INVALID_RESPONSE
    assert response.closed is True
