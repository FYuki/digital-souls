from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
import json
import math
import re
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


_DIGEST_PATTERN = re.compile(r"sha256[:-]([0-9a-fA-F]{64})")


class OllamaAdapter:
    provider_id = "ollama"
    capabilities = frozenset(InferenceCapability)

    def __init__(
        self,
        *,
        base_url: str,
        http_client: httpx.Client | None = None,
    ) -> None:
        if not base_url.strip() or base_url.strip() != base_url:
            raise ValueError("Ollama base URL must be canonical")
        self._base_url = base_url.rstrip("/")
        self._http_client = http_client or httpx.Client(trust_env=False)
        self._owns_http_client = http_client is None
        self._model_digests: dict[str, str] = {}

    def close(self) -> None:
        if self._owns_http_client:
            self._http_client.close()

    def generate_text(self, request: TextGenerationRequest) -> ProviderTextResult:
        response = self._post_chat(request)
        body = self._response_object(response)
        return ProviderTextResult(
            text=self._message_content(body),
            usage=self._usage(body),
        )

    def generate_structured(
        self, request: StructuredGenerationRequest
    ) -> ProviderTextResult:
        response = self._post_chat(request, response_schema=request.response_schema)
        body = self._response_object(response)
        return ProviderTextResult(
            text=self._message_content(body),
            usage=self._usage(body),
        )

    async def stream_text(
        self, request: TextGenerationRequest
    ) -> AsyncIterator[str]:
        payload = self._chat_payload(request, stream=True)
        completed = False
        emitted = False
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(request.timeout_seconds),
                trust_env=False,
            ) as client:
                async with client.stream(
                    "POST", self._endpoint("/api/chat"), json=payload
                ) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if not line.strip():
                            continue
                        content, done = self._stream_chunk(line)
                        if content:
                            emitted = True
                            yield content
                        if done:
                            completed = True
                            break
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._raise_normalized(error)
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
        try:
            response = self._http_client.post(
                self._endpoint("/api/embed"),
                json={
                    "model": request.model_id,
                    "input": list(request.inputs),
                    "truncate": False,
                    "options": dict(request.options),
                },
                timeout=httpx.Timeout(request.timeout_seconds),
            )
            response.raise_for_status()
        except Exception as error:
            self._raise_normalized(error)
        body = self._response_object(response)
        raw_vectors = body.get("embeddings")
        if not isinstance(raw_vectors, list) or len(raw_vectors) != len(request.inputs):
            raise InferenceError(
                InferenceErrorCategory.INVALID_RESPONSE,
                retryable=False,
            )
        vectors: list[tuple[float, ...]] = []
        dimension: int | None = None
        for raw_vector in raw_vectors:
            if not isinstance(raw_vector, list) or not raw_vector:
                raise InferenceError(
                    InferenceErrorCategory.INVALID_RESPONSE,
                    retryable=False,
                )
            if any(
                isinstance(value, bool) or not isinstance(value, (int, float))
                for value in raw_vector
            ):
                raise InferenceError(
                    InferenceErrorCategory.INVALID_RESPONSE,
                    retryable=False,
                )
            vector = tuple(float(value) for value in raw_vector)
            if any(not math.isfinite(value) for value in vector):
                raise InferenceError(
                    InferenceErrorCategory.INVALID_RESPONSE,
                    retryable=False,
                )
            if dimension is None:
                dimension = len(vector)
            elif len(vector) != dimension:
                raise InferenceError(
                    InferenceErrorCategory.INVALID_RESPONSE,
                    retryable=False,
                )
            vectors.append(vector)
        input_tokens = self._non_negative_count(body.get("prompt_eval_count"))
        usage = (
            None
            if input_tokens is None
            else InferenceUsage(input_tokens, 0, input_tokens, provider_reported=True)
        )
        return EmbeddingResult(vectors=tuple(vectors), usage=usage)

    def estimate_input_tokens(self, request: TokenEstimateRequest) -> TokenEstimate:
        estimate_request = TextGenerationRequest(
            messages=request.messages,
            model_id=request.model_id,
            options=request.options,
            max_input_tokens=request.max_input_tokens,
            max_output_tokens=1,
            timeout_seconds=request.timeout_seconds,
        )
        response = self._post_chat(estimate_request)
        body = self._response_object(response)
        prompt_count = self._positive_count(body.get("prompt_eval_count"))
        if request.response_schema is None:
            return TokenEstimate(
                prompt_count,
                TokenEstimateAccuracy.EXACT,
                "ollama_prompt_eval_count",
            )
        schema_bytes = json.dumps(
            request.response_schema,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        conservative_schema_tokens = math.ceil(len(schema_bytes) / 3 * 1.1)
        return TokenEstimate(
            prompt_count + conservative_schema_tokens,
            TokenEstimateAccuracy.ESTIMATED,
            "ollama_prompt_eval_count+schema_utf8_div3_margin10pct",
        )

    def resolve_model_digest(self, model_id: str, *, timeout_seconds: float) -> str:
        cached = self._model_digests.get(model_id)
        if cached is not None:
            return cached
        try:
            response = self._http_client.post(
                self._endpoint("/api/show"),
                json={"model": model_id},
                timeout=httpx.Timeout(timeout_seconds),
            )
            response.raise_for_status()
        except Exception as error:
            self._raise_normalized(error)
        body = self._response_object(response)
        digest = body.get("digest")
        if isinstance(digest, str) and digest.strip():
            resolved = digest
        else:
            modelfile = body.get("modelfile")
            match = _DIGEST_PATTERN.search(modelfile) if isinstance(modelfile, str) else None
            if match is None:
                raise InferenceError(
                    InferenceErrorCategory.INVALID_RESPONSE,
                    retryable=False,
                )
            resolved = f"sha256:{match.group(1).lower()}"
        self._model_digests[model_id] = resolved
        return resolved

    def _post_chat(
        self,
        request: TextGenerationRequest,
        *,
        response_schema: Mapping[str, object] | None = None,
    ) -> httpx.Response:
        try:
            response = self._http_client.post(
                self._endpoint("/api/chat"),
                json=self._chat_payload(
                    request,
                    stream=False,
                    response_schema=response_schema,
                ),
                timeout=httpx.Timeout(request.timeout_seconds),
            )
            response.raise_for_status()
            return response
        except Exception as error:
            self._raise_normalized(error)

    @staticmethod
    def _chat_payload(
        request: TextGenerationRequest,
        *,
        stream: bool,
        response_schema: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        options: dict[str, JsonValue] = dict(request.options)
        options.update(
            {
                "num_ctx": request.max_input_tokens + request.max_output_tokens,
                "num_predict": request.max_output_tokens,
            }
        )
        payload: dict[str, object] = {
            "model": request.model_id,
            "stream": stream,
            "messages": OllamaAdapter._messages(request.messages),
            "options": options,
        }
        if response_schema is not None:
            payload["format"] = dict(response_schema)
            payload["think"] = False
        return payload

    @staticmethod
    def _messages(messages: tuple[InferenceMessage, ...]) -> list[dict[str, str]]:
        return [
            {"role": message.role, "content": message.content}
            for message in messages
        ]

    @staticmethod
    def _message_content(body: Mapping[str, object]) -> str:
        message = body.get("message")
        if not isinstance(message, Mapping):
            raise InferenceError(
                InferenceErrorCategory.INVALID_RESPONSE,
                retryable=False,
            )
        content = message.get("content")
        if not isinstance(content, str) or not content:
            raise InferenceError(
                InferenceErrorCategory.INVALID_RESPONSE,
                retryable=False,
            )
        return content

    @staticmethod
    def _stream_chunk(line: str) -> tuple[str, bool]:
        try:
            value: object = json.loads(line)
        except json.JSONDecodeError:
            raise InferenceError(
                InferenceErrorCategory.INVALID_RESPONSE,
                retryable=False,
            ) from None
        if not isinstance(value, Mapping):
            raise InferenceError(
                InferenceErrorCategory.INVALID_RESPONSE,
                retryable=False,
            )
        done = value.get("done")
        message = value.get("message")
        if not isinstance(done, bool) or not isinstance(message, Mapping):
            raise InferenceError(
                InferenceErrorCategory.INVALID_RESPONSE,
                retryable=False,
            )
        content = message.get("content")
        if not isinstance(content, str):
            raise InferenceError(
                InferenceErrorCategory.INVALID_RESPONSE,
                retryable=False,
            )
        return content, done

    @staticmethod
    def _response_object(response: httpx.Response) -> Mapping[str, object]:
        try:
            value: object = response.json()
        except ValueError:
            raise InferenceError(
                InferenceErrorCategory.INVALID_RESPONSE,
                retryable=False,
            ) from None
        if not isinstance(value, Mapping):
            raise InferenceError(
                InferenceErrorCategory.INVALID_RESPONSE,
                retryable=False,
            )
        return cast(Mapping[str, object], value)

    @classmethod
    def _usage(cls, body: Mapping[str, object]) -> InferenceUsage | None:
        input_tokens = cls._non_negative_count(body.get("prompt_eval_count"))
        output_tokens = cls._non_negative_count(body.get("eval_count"))
        if input_tokens is None or output_tokens is None:
            return None
        return InferenceUsage(
            input_tokens,
            output_tokens,
            input_tokens + output_tokens,
            provider_reported=True,
        )

    @staticmethod
    def _positive_count(value: object) -> int:
        if type(value) is not int or value < 1:
            raise InferenceError(
                InferenceErrorCategory.INVALID_RESPONSE,
                retryable=False,
            )
        return value

    @staticmethod
    def _non_negative_count(value: object) -> int | None:
        return value if type(value) is int and value >= 0 else None

    def _endpoint(self, path: str) -> str:
        return f"{self._base_url}{path}"

    @staticmethod
    def _raise_normalized(error: Exception) -> NoReturn:
        if isinstance(error, InferenceError):
            raise error
        if isinstance(error, httpx.TimeoutException):
            category = InferenceErrorCategory.TIMEOUT
            retryable = True
        elif isinstance(error, httpx.HTTPStatusError):
            status = error.response.status_code
            if status == 401:
                category, retryable = InferenceErrorCategory.AUTHENTICATION_FAILED, False
            elif status == 403:
                category, retryable = InferenceErrorCategory.PERMISSION_DENIED, False
            elif status == 404:
                category, retryable = InferenceErrorCategory.MODEL_NOT_FOUND, False
            elif status == 429:
                category, retryable = InferenceErrorCategory.RATE_LIMITED, True
            elif status >= 500:
                category, retryable = InferenceErrorCategory.UNAVAILABLE, True
            else:
                category, retryable = InferenceErrorCategory.PROVIDER_ERROR, False
        elif isinstance(error, httpx.HTTPError):
            category, retryable = InferenceErrorCategory.UNAVAILABLE, True
        else:
            category, retryable = InferenceErrorCategory.PROVIDER_ERROR, False
        raise InferenceError(category, retryable=retryable) from None
