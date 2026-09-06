from __future__ import annotations

import asyncio
import base64
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
    InferenceImagePart,
    InferenceMessage,
    InferenceTextPart,
    InferenceUsage,
    JsonValue,
    ModelProbeResult,
    ProviderTextResult,
    StructuredGenerationRequest,
    TextGenerationRequest,
    TokenEstimate,
    TokenEstimateAccuracy,
    TokenEstimateRequest,
)
from app.inference.errors import InferenceError, InferenceErrorCategory
from app.inference.images import CONSERVATIVE_IMAGE_TOKEN_ESTIMATE
from app.inference.diagnostics import diagnostic, ollama_diagnostics
from app.inference.token_estimate_cache import ExactTokenEstimateCache


_DIGEST_PATTERN = re.compile(r"sha256[:-]([0-9a-fA-F]{64})")
_UNSUPPORTED_GRAMMAR_SCHEMA_KEYWORDS = frozenset(
    {"minLength", "maxLength", "minItems", "maxItems"}
)


class OllamaAdapter:
    provider_id = "ollama"
    capabilities = frozenset(
        {
            InferenceCapability.GENERATE_TEXT,
            InferenceCapability.STREAM_TEXT,
            InferenceCapability.GENERATE_STRUCTURED,
            InferenceCapability.IMAGE_INPUT,
            InferenceCapability.EMBED,
            InferenceCapability.ESTIMATE_INPUT_TOKENS,
        }
    )

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
        self._token_counts = ExactTokenEstimateCache()
        self._model_digests: dict[str, str] = {}
        self._model_details: dict[str, Mapping[str, object]] = {}

    def close(self) -> None:
        self._token_counts.clear()
        if self._owns_http_client:
            self._http_client.close()

    def probe(self, model_id: str, *, timeout_seconds: float) -> ModelProbeResult:
        """生成を行わずendpoint到達性とmodel存在を確認する。"""
        body = self._resolve_model_details(model_id, timeout_seconds=timeout_seconds)
        self._digest_from_details(model_id, body)
        raw_capabilities = body.get("capabilities")
        if not isinstance(raw_capabilities, list) or any(
            not isinstance(item, str) for item in raw_capabilities
        ):
            return ModelProbeResult()
        capabilities: set[InferenceCapability] = set()
        if "vision" in raw_capabilities:
            capabilities.add(InferenceCapability.IMAGE_INPUT)
        return ModelProbeResult(frozenset(capabilities))

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
                diagnostic("llm_http_started")
                async with client.stream(
                    "POST", self._endpoint("/api/chat"), json=payload
                ) as response:
                    response.raise_for_status()
                    diagnostic("llm_http_headers_received")
                    async for line in response.aiter_lines():
                        if not line.strip():
                            continue
                        content, done = self._stream_chunk(line)
                        if content:
                            emitted = True
                            yield content
                        if done:
                            ollama_diagnostics(json.loads(line))
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
        image_count = sum(
            isinstance(part, InferenceImagePart)
            for message in request.messages
            if isinstance(message.content, tuple)
            for part in message.content
        )
        if image_count:
            serialized = {
                "model": request.model_id,
                "messages": self._messages_without_images(request.messages),
                "options": dict(request.options),
                "response_schema": request.response_schema,
            }
            text_bytes = len(
                json.dumps(
                    serialized,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
            return TokenEstimate(
                math.ceil(text_bytes / 3 * 1.15)
                + CONSERVATIVE_IMAGE_TOKEN_ESTIMATE * image_count,
                TokenEstimateAccuracy.ESTIMATED,
                "ollama_multimodal_text_utf8_div3_margin15pct+1120_per_image",
                external_request_count=0,
            )
        estimate_request = TextGenerationRequest(
            messages=request.messages,
            model_id=request.model_id,
            options=request.options,
            max_input_tokens=request.max_input_tokens,
            max_output_tokens=1,
            timeout_seconds=request.timeout_seconds,
        )
        context_window = request.context_window_tokens
        if context_window is not None and (
            type(context_window) is not int
            or context_window < request.max_input_tokens + 1
        ):
            raise InferenceError(
                InferenceErrorCategory.INVALID_REQUEST, retryable=False,
            )
        cache_key: bytes | None = None
        metadata_requests = 0
        if request.allow_cached_exact_result and request.response_schema is None:
            # tagsのmanifest digestは毎回確認する。mutable tagの変更後に旧countを使わない。
            metadata_requests = 1
            diagnostic("token_estimate_metadata_requests", 1)
            digest = self._current_model_manifest_digest(request)
            if digest is not None:
                payload = self._chat_payload(
                    estimate_request, stream=False, context_window_tokens=context_window,
                )
                cache_key = self._token_counts.key(json.dumps(
                    [digest, payload], ensure_ascii=False, sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8"))
                cached = self._token_counts.get(cache_key)
                if cached is not None:
                    diagnostic("token_estimate_cache_hits", 1)
                    return TokenEstimate(
                        cached, TokenEstimateAccuracy.EXACT, "ollama_prompt_eval_count",
                        external_request_count=metadata_requests,
                    )
        response = self._post_chat(
            estimate_request, context_window_tokens=context_window,
        )
        body = self._response_object(response)
        ollama_diagnostics(body)
        prompt_count = self._positive_count(body.get("prompt_eval_count"))
        if request.response_schema is None:
            if cache_key is not None:
                metadata_requests += 1
                diagnostic("token_estimate_metadata_requests", 1)
                if self._current_model_manifest_digest(request) == digest:
                    self._token_counts.put(cache_key, prompt_count)
            return TokenEstimate(
                prompt_count,
                TokenEstimateAccuracy.EXACT,
                "ollama_prompt_eval_count",
                external_request_count=1 + metadata_requests,
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
            external_request_count=1,
        )

    def _current_model_manifest_digest(self, request: TokenEstimateRequest) -> str | None:
        try:
            response = self._http_client.get(
                self._endpoint("/api/tags"), timeout=httpx.Timeout(min(request.timeout_seconds, 1.0)),
            )
            response.raise_for_status()
            models = self._response_object(response).get("models")
        except (httpx.HTTPError, InferenceError):
            # metadataを確認できないときは通常の計測を行い、cacheを使用しない。
            return None
        if not isinstance(models, list):
            return None
        names = {request.model_id}
        if ":" not in request.model_id.rsplit("/", 1)[-1]:
            names.add(f"{request.model_id}:latest")
        matches = [
            model for model in models if isinstance(model, Mapping)
            and isinstance(model.get("name"), str) and model.get("name") in names
        ]
        if len(matches) != 1:
            return None
        digest = matches[0].get("digest")
        if isinstance(digest, str) and re.fullmatch(r"(?:sha256:)?[0-9a-f]{64}", digest):
            return digest
        return None

    def resolve_model_digest(self, model_id: str, *, timeout_seconds: float) -> str:
        cached = self._model_digests.get(model_id)
        if cached is not None:
            return cached
        body = self._resolve_model_details(model_id, timeout_seconds=timeout_seconds)
        return self._digest_from_details(model_id, body)

    def _resolve_model_details(
        self, model_id: str, *, timeout_seconds: float
    ) -> Mapping[str, object]:
        cached = self._model_details.get(model_id)
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
        self._model_details[model_id] = body
        return body

    def _digest_from_details(
        self, model_id: str, body: Mapping[str, object]
    ) -> str:
        cached = self._model_digests.get(model_id)
        if cached is not None:
            return cached
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
        context_window_tokens: int | None = None,
    ) -> httpx.Response:
        try:
            response = self._http_client.post(
                self._endpoint("/api/chat"),
                json=self._chat_payload(
                    request,
                    stream=False,
                    response_schema=response_schema,
                    context_window_tokens=context_window_tokens,
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
        context_window_tokens: int | None = None,
    ) -> dict[str, object]:
        options: dict[str, JsonValue] = dict(request.options)
        thinking = options.pop("think", None)
        options.update(
            {
                "num_ctx": (
                    context_window_tokens
                    if context_window_tokens is not None
                    else request.max_input_tokens + request.max_output_tokens
                ),
                "num_predict": request.max_output_tokens,
            }
        )
        payload: dict[str, object] = {
            "model": request.model_id,
            "stream": stream,
            "messages": OllamaAdapter._messages(request.messages),
            "options": options,
        }
        if thinking is not None:
            payload["think"] = thinking
        if response_schema is not None:
            payload["format"] = OllamaAdapter._grammar_schema(response_schema)
            payload["think"] = False
        return payload

    @staticmethod
    def _grammar_schema(value: object) -> object:
        """Ollama grammar未対応制約だけを除き、Coreの完全schema検証は維持する。"""
        if isinstance(value, Mapping):
            return {
                key: OllamaAdapter._grammar_schema(item)
                for key, item in value.items()
                if key not in _UNSUPPORTED_GRAMMAR_SCHEMA_KEYWORDS
            }
        if isinstance(value, list):
            return [OllamaAdapter._grammar_schema(item) for item in value]
        return value

    @staticmethod
    def _messages(messages: tuple[InferenceMessage, ...]) -> list[dict[str, object]]:
        mapped: list[dict[str, object]] = []
        for message in messages:
            if isinstance(message.content, str):
                mapped.append({"role": message.role, "content": message.content})
                continue
            texts = [
                part.text
                for part in message.content
                if isinstance(part, InferenceTextPart)
            ]
            images = [
                base64.b64encode(part.data).decode("ascii")
                for part in message.content
                if isinstance(part, InferenceImagePart)
            ]
            mapped_message: dict[str, object] = {
                "role": message.role,
                "content": "\n".join(texts),
            }
            if images:
                mapped_message["images"] = images
            mapped.append(mapped_message)
        return mapped

    @staticmethod
    def _messages_without_images(
        messages: tuple[InferenceMessage, ...],
    ) -> list[dict[str, str]]:
        mapped: list[dict[str, str]] = []
        for message in messages:
            content = (
                message.content
                if isinstance(message.content, str)
                else "\n".join(
                    part.text
                    for part in message.content
                    if isinstance(part, InferenceTextPart)
                )
            )
            mapped.append({"role": message.role, "content": content})
        return mapped

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
        diagnostic("llm_first_provider_chunk")
        thinking = message.get("thinking")
        if isinstance(thinking, str) and thinking:
            diagnostic("llm_first_thinking_chunk")
            diagnostic("llm_thinking_chunks", 1)
            diagnostic("llm_thinking_characters", len(thinking))
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
