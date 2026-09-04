from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
import json
import logging
from threading import BoundedSemaphore
from time import perf_counter
from uuid import uuid4

from jsonschema import Draft202012Validator, SchemaError, ValidationError

from app.inference.authorization import InferenceCaller, authorize
from app.inference.config import InferenceSettings
from app.inference.contracts import (
    EmbeddingRequest,
    EmbeddingResult,
    InferenceAdapter,
    InferenceCapability,
    InferenceMessage,
    InferenceTarget,
    InferenceUsage,
    JsonValue,
    ProviderTextResult,
    ResolvedTarget,
    StructuredGenerationRequest,
    StructuredGenerationResult,
    TextGenerationRequest,
    TextGenerationResult,
    TokenEstimate,
    TokenEstimateRequest,
)
from app.inference.errors import InferenceError, InferenceErrorCategory
from app.inference.observer import (
    InferenceObservation,
    InferenceObserver,
    ignore_inference_observation,
)
from app.inference.registry import ProviderRegistry


logger = logging.getLogger(__name__)


class InferenceRouter:
    def __init__(
        self,
        *,
        settings: InferenceSettings,
        registry: ProviderRegistry,
        observer: InferenceObserver = ignore_inference_observation,
    ) -> None:
        self._settings = settings
        self._registry = registry
        self._observer = observer
        self._capacity = {
            target: BoundedSemaphore(resolved.max_concurrency)
            for target, resolved in settings.targets.items()
        }

    def generate_text(
        self,
        *,
        caller: InferenceCaller,
        target: InferenceTarget,
        messages: tuple[InferenceMessage, ...],
        timeout_seconds: float | None = None,
    ) -> TextGenerationResult:
        resolved, adapter = self._resolve(
            caller, target, InferenceCapability.GENERATE_TEXT
        )
        max_output_tokens = resolved.max_output_tokens
        if max_output_tokens is None:
            raise AssertionError("text generation target requires an output limit")
        request_id = str(uuid4())
        started_at = perf_counter()
        try:
            with self._capacity[target]:
                provider_result = adapter.generate_text(
                    TextGenerationRequest(
                        messages=messages,
                        model_id=resolved.reference.model_id,
                        options=resolved.options,
                        max_input_tokens=resolved.max_input_tokens,
                        max_output_tokens=max_output_tokens,
                        timeout_seconds=self._timeout(
                            resolved.timeout_seconds, timeout_seconds
                        ),
                    )
                )
            self._validate_text(provider_result)
        except Exception as error:
            self._observe_error(
                request_id, started_at, caller, target,
                InferenceCapability.GENERATE_TEXT, resolved, error,
            )
            raise
        self._observe(
            request_id, started_at, caller, target,
            InferenceCapability.GENERATE_TEXT, resolved, None,
            usage=provider_result.usage,
        )
        return TextGenerationResult(
            text=provider_result.text,
            usage=provider_result.usage,
        )

    async def stream_text(
        self,
        *,
        caller: InferenceCaller,
        target: InferenceTarget,
        messages: tuple[InferenceMessage, ...],
        timeout_seconds: float | None = None,
    ) -> AsyncIterator[str]:
        resolved, adapter = self._resolve(
            caller, target, InferenceCapability.STREAM_TEXT
        )
        max_output_tokens = resolved.max_output_tokens
        if max_output_tokens is None:
            raise AssertionError("streaming target requires an output limit")
        request = TextGenerationRequest(
            messages=messages,
            model_id=resolved.reference.model_id,
            options=resolved.options,
            max_input_tokens=resolved.max_input_tokens,
            max_output_tokens=max_output_tokens,
            timeout_seconds=self._timeout(resolved.timeout_seconds, timeout_seconds),
        )
        capacity = self._capacity[target]
        request_id = str(uuid4())
        started_at = perf_counter()
        while not capacity.acquire(blocking=False):
            await asyncio.sleep(0.01)
        error_category: InferenceErrorCategory | None = None
        try:
            async for delta in adapter.stream_text(request):
                if not isinstance(delta, str):
                    raise InferenceError(
                        InferenceErrorCategory.INVALID_RESPONSE,
                        retryable=False,
                )
                yield delta
        except asyncio.CancelledError:
            error_category = InferenceErrorCategory.CANCELLED
            raise
        except Exception as error:
            error_category = (
                error.category
                if isinstance(error, InferenceError)
                else InferenceErrorCategory.PROVIDER_ERROR
            )
            raise
        finally:
            capacity.release()
            self._observe(
                request_id, started_at, caller, target,
                InferenceCapability.STREAM_TEXT, resolved, error_category,
            )

    def generate_structured(
        self,
        *,
        caller: InferenceCaller,
        target: InferenceTarget,
        messages: tuple[InferenceMessage, ...],
        response_schema: Mapping[str, object],
        timeout_seconds: float | None = None,
    ) -> StructuredGenerationResult:
        try:
            Draft202012Validator.check_schema(response_schema)
        except SchemaError:
            raise InferenceError(
                InferenceErrorCategory.INVALID_REQUEST,
                retryable=False,
            ) from None
        resolved, adapter = self._resolve(
            caller, target, InferenceCapability.GENERATE_STRUCTURED
        )
        max_output_tokens = resolved.max_output_tokens
        if max_output_tokens is None:
            raise AssertionError("structured target requires an output limit")
        request_id = str(uuid4())
        started_at = perf_counter()
        try:
            with self._capacity[target]:
                provider_result = adapter.generate_structured(
                    StructuredGenerationRequest(
                        messages=messages,
                        model_id=resolved.reference.model_id,
                        options=resolved.options,
                        max_input_tokens=resolved.max_input_tokens,
                        max_output_tokens=max_output_tokens,
                        timeout_seconds=self._timeout(
                            resolved.timeout_seconds, timeout_seconds
                        ),
                        response_schema=response_schema,
                    )
                )
            self._validate_text(provider_result)
            value: JsonValue = json.loads(provider_result.text)
            Draft202012Validator(response_schema).validate(value)
        except (json.JSONDecodeError, ValidationError):
            error = InferenceError(
                InferenceErrorCategory.INVALID_RESPONSE,
                retryable=False,
            )
            self._observe(
                request_id,
                started_at,
                caller,
                target,
                InferenceCapability.GENERATE_STRUCTURED,
                resolved,
                error.category,
            )
            raise error from None
        except Exception as error:
            self._observe(
                request_id,
                started_at,
                caller,
                target,
                InferenceCapability.GENERATE_STRUCTURED,
                resolved,
                (
                    error.category
                    if isinstance(error, InferenceError)
                    else InferenceErrorCategory.PROVIDER_ERROR
                ),
            )
            raise
        self._observe(
            request_id,
            started_at,
            caller,
            target,
            InferenceCapability.GENERATE_STRUCTURED,
            resolved,
            None,
            usage=provider_result.usage,
        )
        return StructuredGenerationResult(value=value, usage=provider_result.usage)

    def embed(
        self,
        *,
        caller: InferenceCaller,
        target: InferenceTarget,
        inputs: tuple[str, ...],
        timeout_seconds: float | None = None,
    ) -> EmbeddingResult:
        resolved, adapter = self._resolve(caller, target, InferenceCapability.EMBED)
        request_id = str(uuid4())
        started_at = perf_counter()
        try:
            with self._capacity[target]:
                result = adapter.embed(
                    EmbeddingRequest(
                        inputs=inputs,
                        model_id=resolved.reference.model_id,
                        options=resolved.options,
                        max_input_tokens=resolved.max_input_tokens,
                        timeout_seconds=self._timeout(
                            resolved.timeout_seconds, timeout_seconds
                        ),
                    )
                )
        except Exception as error:
            self._observe(
                request_id,
                started_at,
                caller,
                target,
                InferenceCapability.EMBED,
                resolved,
                (
                    error.category
                    if isinstance(error, InferenceError)
                    else InferenceErrorCategory.PROVIDER_ERROR
                ),
            )
            raise
        self._observe(
            request_id,
            started_at,
            caller,
            target,
            InferenceCapability.EMBED,
            resolved,
            None,
            usage=result.usage,
        )
        return result

    def estimate_input_tokens(
        self,
        *,
        caller: InferenceCaller,
        target: InferenceTarget,
        messages: tuple[InferenceMessage, ...],
        response_schema: Mapping[str, object] | None = None,
        timeout_seconds: float | None = None,
    ) -> TokenEstimate:
        resolved, adapter = self._resolve(
            caller, target, InferenceCapability.ESTIMATE_INPUT_TOKENS
        )
        request_id = str(uuid4())
        started_at = perf_counter()
        try:
            with self._capacity[target]:
                estimate = adapter.estimate_input_tokens(
                    TokenEstimateRequest(
                        messages=messages,
                        model_id=resolved.reference.model_id,
                        options=resolved.options,
                        max_input_tokens=resolved.max_input_tokens,
                        timeout_seconds=self._timeout(
                            resolved.timeout_seconds, timeout_seconds
                        ),
                        response_schema=response_schema,
                    )
                )
            if estimate.count > resolved.max_input_tokens:
                raise InferenceError(
                    InferenceErrorCategory.INVALID_REQUEST,
                    retryable=False,
                    message="inference input exceeds the configured token limit",
                )
        except Exception as error:
            self._observe_error(
                request_id, started_at, caller, target,
                InferenceCapability.ESTIMATE_INPUT_TOKENS, resolved, error,
            )
            raise
        self._observe(
            request_id, started_at, caller, target,
            InferenceCapability.ESTIMATE_INPUT_TOKENS, resolved, None,
            token_estimate=estimate,
        )
        return estimate

    def _resolve(
        self,
        caller: InferenceCaller,
        target: InferenceTarget,
        capability: InferenceCapability,
    ) -> tuple[ResolvedTarget, InferenceAdapter]:
        authorize(caller, target)
        resolved = self._settings.target(target)
        adapter = self._registry.adapter(resolved.reference.provider_id)
        if capability not in adapter.capabilities:
            raise InferenceError(
                InferenceErrorCategory.UNSUPPORTED_CAPABILITY,
                retryable=False,
            )
        return resolved, adapter

    def _observe(
        self,
        request_id: str,
        started_at: float,
        caller: InferenceCaller,
        target: InferenceTarget,
        capability: InferenceCapability,
        resolved: ResolvedTarget,
        error_category: InferenceErrorCategory | None,
        *,
        token_estimate: TokenEstimate | None = None,
        usage: InferenceUsage | None = None,
    ) -> None:
        try:
            self._observer(
                InferenceObservation(
                    request_id=request_id,
                    caller=caller,
                    target=target,
                    capability=capability,
                    provider_id=resolved.reference.provider_id,
                    model_id=resolved.reference.model_id,
                    auth_kind=self._auth_kind(resolved.reference.provider_id),
                    latency_ms=max(0.0, (perf_counter() - started_at) * 1000),
                    external_request_count=self._external_request_count(
                        resolved.reference.provider_id,
                        capability,
                    ),
                    token_estimate=token_estimate,
                    usage=usage,
                    success=error_category is None,
                    error_category=error_category,
                )
            )
        except Exception:
            logger.warning("Inference observer failed")

    def _observe_error(
        self,
        request_id: str,
        started_at: float,
        caller: InferenceCaller,
        target: InferenceTarget,
        capability: InferenceCapability,
        resolved: ResolvedTarget,
        error: Exception,
    ) -> None:
        self._observe(
            request_id,
            started_at,
            caller,
            target,
            capability,
            resolved,
            error.category
            if isinstance(error, InferenceError)
            else InferenceErrorCategory.PROVIDER_ERROR,
        )

    @staticmethod
    def _auth_kind(provider_id: str) -> str:
        return {
            "ollama": "none",
            "openai-api": "api_key",
            "openai-codex": "subscription",
        }.get(provider_id, "unknown")

    @staticmethod
    def _external_request_count(
        provider_id: str,
        capability: InferenceCapability,
    ) -> int:
        if (
            capability is InferenceCapability.ESTIMATE_INPUT_TOKENS
            and provider_id in {"openai-api", "openai-codex"}
        ):
            return 0
        return 1

    @staticmethod
    def _validate_text(result: ProviderTextResult) -> None:
        if not isinstance(result.text, str) or not result.text:
            raise InferenceError(
                InferenceErrorCategory.INVALID_RESPONSE,
                retryable=False,
            )

    @staticmethod
    def _timeout(configured: float, requested: float | None) -> float:
        if requested is None:
            return configured
        if requested <= 0:
            raise InferenceError(
                InferenceErrorCategory.INVALID_REQUEST,
                retryable=False,
            )
        return min(configured, requested)
