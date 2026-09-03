from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Mapping
import json
import math
from typing import NoReturn, cast

import httpx

from app.inference.contracts import (
    EmbeddingRequest,
    EmbeddingResult,
    InferenceCapability,
    InferenceMessage,
    InferenceUsage,
    JsonValue,
    ProviderTextResult,
    StructuredGenerationRequest,
    TextGenerationRequest,
    TokenEstimate,
    TokenEstimateAccuracy,
    TokenEstimateRequest,
)
from app.inference.errors import InferenceError, InferenceErrorCategory


OPENAI_API_BASE_URL = "https://api.openai.com/v1"
AsyncClientFactory = Callable[[float], httpx.AsyncClient]


class OpenAIAPIAdapter:
    """公式OpenAI API専用Adapter。ChatGPTサブスクリプション認証は扱わない。"""

    provider_id = "openai-api"
    capabilities = frozenset(InferenceCapability)

    def __init__(
        self,
        *,
        api_key: str,
        http_client: httpx.Client | None = None,
        async_client_factory: AsyncClientFactory | None = None,
    ) -> None:
        if not api_key or api_key.strip() != api_key:
            raise ValueError("OPENAI_API_KEY must be configured for openai-api")
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        self._http_client = http_client or httpx.Client(
            headers=self._headers,
            timeout=None,
            trust_env=False,
        )
        self._owns_http_client = http_client is None
        self._async_client_factory = async_client_factory or self._new_async_client

    def close(self) -> None:
        if self._owns_http_client:
            self._http_client.close()

    def generate_text(self, request: TextGenerationRequest) -> ProviderTextResult:
        body = self._post_json(
            "/responses",
            self._response_payload(request),
            timeout_seconds=request.timeout_seconds,
        )
        return self._text_result(body)

    def generate_structured(
        self, request: StructuredGenerationRequest
    ) -> ProviderTextResult:
        payload = self._response_payload(request)
        payload["text"] = {
            "format": {
                "type": "json_schema",
                "name": "inference_response",
                "schema": dict(request.response_schema),
                "strict": True,
            }
        }
        body = self._post_json(
            "/responses",
            payload,
            timeout_seconds=request.timeout_seconds,
        )
        return self._text_result(body)

    async def stream_text(self, request: TextGenerationRequest) -> AsyncIterator[str]:
        payload = self._response_payload(request)
        payload["stream"] = True
        completed = False
        emitted = False
        try:
            async with self._async_client_factory(request.timeout_seconds) as client:
                async with client.stream(
                    "POST",
                    f"{OPENAI_API_BASE_URL}/responses",
                    json=payload,
                    headers=self._headers,
                ) as response:
                    if response.is_error:
                        self._raise_http_status(response.status_code, None)
                    async for line in response.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        raw_event = line.removeprefix("data:").strip()
                        if not raw_event or raw_event == "[DONE]":
                            continue
                        try:
                            event: object = json.loads(raw_event)
                        except json.JSONDecodeError:
                            raise InferenceError(
                                InferenceErrorCategory.INVALID_RESPONSE,
                                retryable=False,
                            ) from None
                        if not isinstance(event, dict):
                            raise InferenceError(
                                InferenceErrorCategory.INVALID_RESPONSE,
                                retryable=False,
                            )
                        event_type = event.get("type")
                        if event_type == "response.output_text.delta":
                            delta = event.get("delta")
                            if not isinstance(delta, str):
                                raise InferenceError(
                                    InferenceErrorCategory.INVALID_RESPONSE,
                                    retryable=False,
                                )
                            if delta:
                                emitted = True
                                yield delta
                        elif event_type == "response.completed":
                            completed = True
                        elif event_type in {"response.failed", "response.incomplete"}:
                            raise InferenceError(
                                InferenceErrorCategory.PROVIDER_ERROR,
                                retryable=False,
                            )
        except asyncio.CancelledError:
            raise
        except InferenceError:
            raise
        except Exception as error:
            self._raise_transport(error)
        if not completed or not emitted:
            raise InferenceError(
                InferenceErrorCategory.INVALID_RESPONSE,
                retryable=False,
            )

    def embed(self, request: EmbeddingRequest) -> EmbeddingResult:
        if not request.inputs or any(not value for value in request.inputs):
            raise InferenceError(
                InferenceErrorCategory.INVALID_REQUEST,
                retryable=False,
            )
        if request.options:
            raise InferenceError(
                InferenceErrorCategory.INVALID_REQUEST,
                retryable=False,
            )
        body = self._post_json(
            "/embeddings",
            {
                "model": request.model_id,
                "input": list(request.inputs),
                "encoding_format": "float",
            },
            timeout_seconds=request.timeout_seconds,
        )
        data = body.get("data")
        if not isinstance(data, list) or len(data) != len(request.inputs):
            self._invalid_response()
        ordered: list[tuple[int, tuple[float, ...]]] = []
        dimension: int | None = None
        for raw_item in data:
            if not isinstance(raw_item, dict):
                self._invalid_response()
            index = raw_item.get("index")
            raw_vector = raw_item.get("embedding")
            if (
                type(index) is not int
                or not isinstance(raw_vector, list)
                or not raw_vector
            ):
                self._invalid_response()
            if any(
                isinstance(value, bool) or not isinstance(value, (int, float))
                for value in raw_vector
            ):
                self._invalid_response()
            vector = tuple(float(value) for value in raw_vector)
            if any(not math.isfinite(value) for value in vector):
                self._invalid_response()
            if dimension is None:
                dimension = len(vector)
            elif len(vector) != dimension:
                self._invalid_response()
            ordered.append((index, vector))
        ordered.sort(key=lambda item: item[0])
        if [index for index, _ in ordered] != list(range(len(request.inputs))):
            self._invalid_response()
        return EmbeddingResult(
            vectors=tuple(vector for _, vector in ordered),
            usage=self._usage(body),
        )

    def estimate_input_tokens(self, request: TokenEstimateRequest) -> TokenEstimate:
        serialized: dict[str, object] = {
            "model": request.model_id,
            "input": self._messages(request.messages),
            "options": dict(request.options),
        }
        if request.response_schema is not None:
            serialized["response_schema"] = dict(request.response_schema)
        byte_count = len(
            json.dumps(
                serialized,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        return TokenEstimate(
            count=max(1, math.ceil(byte_count / 3 * 1.15)),
            accuracy=TokenEstimateAccuracy.ESTIMATED,
            method="openai_payload_utf8_div3_margin15pct",
        )

    def _response_payload(self, request: TextGenerationRequest) -> dict[str, object]:
        payload: dict[str, object] = {
            "model": request.model_id,
            "input": self._messages(request.messages),
            "max_output_tokens": request.max_output_tokens,
            "store": False,
        }
        options: Mapping[str, JsonValue] = request.options
        for key in ("temperature", "top_p"):
            if key in options:
                payload[key] = options[key]
        reasoning_effort = options.get("reasoning_effort")
        if reasoning_effort is not None:
            payload["reasoning"] = {"effort": reasoning_effort}
        return payload

    @staticmethod
    def _messages(messages: tuple[InferenceMessage, ...]) -> list[dict[str, str]]:
        return [
            {"role": message.role, "content": message.content} for message in messages
        ]

    def _post_json(
        self,
        path: str,
        payload: Mapping[str, object],
        *,
        timeout_seconds: float,
    ) -> dict[str, object]:
        try:
            response = self._http_client.post(
                f"{OPENAI_API_BASE_URL}{path}",
                json=dict(payload),
                headers=self._headers,
                timeout=httpx.Timeout(timeout_seconds),
            )
        except Exception as error:
            self._raise_transport(error)
        if response.is_error:
            error_code: str | None = None
            try:
                error_body: object = response.json()
                if isinstance(error_body, dict):
                    error_value = error_body.get("error")
                    if isinstance(error_value, dict):
                        candidate = error_value.get("code")
                        if isinstance(candidate, str):
                            error_code = candidate
            except ValueError:
                pass
            self._raise_http_status(response.status_code, error_code)
        try:
            body: object = response.json()
        except ValueError:
            self._invalid_response()
        if not isinstance(body, dict):
            self._invalid_response()
        return cast(dict[str, object], body)

    def _text_result(self, body: Mapping[str, object]) -> ProviderTextResult:
        if body.get("status") != "completed":
            raise InferenceError(
                InferenceErrorCategory.PROVIDER_ERROR,
                retryable=False,
            )
        text = body.get("output_text")
        if not isinstance(text, str):
            text_parts: list[str] = []
            output = body.get("output")
            if isinstance(output, list):
                for item in output:
                    if not isinstance(item, dict) or item.get("type") != "message":
                        continue
                    content = item.get("content")
                    if not isinstance(content, list):
                        continue
                    for part in content:
                        if (
                            isinstance(part, dict)
                            and part.get("type") == "output_text"
                            and isinstance(part.get("text"), str)
                        ):
                            text_parts.append(cast(str, part["text"]))
            text = "".join(text_parts)
        if not text:
            self._invalid_response()
        return ProviderTextResult(text=text, usage=self._usage(body))

    @staticmethod
    def _usage(body: Mapping[str, object]) -> InferenceUsage | None:
        usage = body.get("usage")
        if not isinstance(usage, dict):
            return None
        input_tokens = usage.get("input_tokens")
        output_tokens = usage.get("output_tokens", 0)
        total_tokens = usage.get("total_tokens")
        if (
            type(input_tokens) is not int
            or type(output_tokens) is not int
            or input_tokens < 0
            or output_tokens < 0
        ):
            OpenAIAPIAdapter._invalid_response()
        expected_total = input_tokens + output_tokens
        if total_tokens is None:
            total_tokens = expected_total
        if type(total_tokens) is not int or total_tokens != expected_total:
            OpenAIAPIAdapter._invalid_response()
        return InferenceUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            provider_reported=True,
        )

    def _new_async_client(self, timeout_seconds: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            headers=self._headers,
            timeout=httpx.Timeout(timeout_seconds),
            trust_env=False,
        )

    @staticmethod
    def _raise_http_status(status_code: int, error_code: str | None) -> NoReturn:
        if status_code == 401:
            category = InferenceErrorCategory.AUTHENTICATION_FAILED
            retryable = False
        elif status_code == 403:
            category = InferenceErrorCategory.PERMISSION_DENIED
            retryable = False
        elif status_code == 404 or error_code == "model_not_found":
            category = InferenceErrorCategory.MODEL_NOT_FOUND
            retryable = False
        elif status_code == 429:
            category = InferenceErrorCategory.RATE_LIMITED
            retryable = True
        elif status_code in {408, 504}:
            category = InferenceErrorCategory.TIMEOUT
            retryable = True
        elif status_code == 400:
            category = InferenceErrorCategory.INVALID_REQUEST
            retryable = False
        elif status_code >= 500:
            category = InferenceErrorCategory.UNAVAILABLE
            retryable = True
        else:
            category = InferenceErrorCategory.PROVIDER_ERROR
            retryable = False
        raise InferenceError(category, retryable=retryable)

    @staticmethod
    def _raise_transport(error: Exception) -> NoReturn:
        if isinstance(error, httpx.TimeoutException):
            category = InferenceErrorCategory.TIMEOUT
        elif isinstance(error, (httpx.ConnectError, httpx.NetworkError)):
            category = InferenceErrorCategory.UNAVAILABLE
        else:
            category = InferenceErrorCategory.PROVIDER_ERROR
        raise InferenceError(
            category,
            retryable=category is not InferenceErrorCategory.PROVIDER_ERROR,
        ) from None

    @staticmethod
    def _invalid_response() -> NoReturn:
        raise InferenceError(
            InferenceErrorCategory.INVALID_RESPONSE,
            retryable=False,
        )
