from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
import json

from jsonschema import Draft202012Validator, SchemaError, ValidationError

from app.inference.authorization import InferenceAuthorizer, InferencePrincipal
from app.inference.config import InferenceSettings
from app.inference.contracts import (
    EmbeddingRequest,
    EmbeddingResult,
    InferenceAdapter,
    InferenceCapability,
    InferenceMessage,
    InferenceTarget,
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
from app.inference.registry import ProviderRegistry


class InferenceRouter:
    def __init__(
        self,
        *,
        settings: InferenceSettings,
        registry: ProviderRegistry,
        authorizer: InferenceAuthorizer,
    ) -> None:
        self._settings = settings
        self._registry = registry
        self._authorizer = authorizer

    def generate_text(
        self,
        *,
        principal: InferencePrincipal,
        target: InferenceTarget,
        messages: tuple[InferenceMessage, ...],
        timeout_seconds: float | None = None,
    ) -> TextGenerationResult:
        resolved, adapter = self._resolve(
            principal, target, InferenceCapability.GENERATE_TEXT
        )
        max_output_tokens = resolved.max_output_tokens
        if max_output_tokens is None:
            raise AssertionError("text generation target requires an output limit")
        provider_result = adapter.generate_text(
            TextGenerationRequest(
                messages=messages,
                model_id=resolved.reference.model_id,
                options=resolved.options,
                max_input_tokens=resolved.max_input_tokens,
                max_output_tokens=max_output_tokens,
                timeout_seconds=self._timeout(resolved.timeout_seconds, timeout_seconds),
            )
        )
        self._validate_text(provider_result)
        return TextGenerationResult(
            text=provider_result.text,
            usage=provider_result.usage,
        )

    async def stream_text(
        self,
        *,
        principal: InferencePrincipal,
        target: InferenceTarget,
        messages: tuple[InferenceMessage, ...],
        timeout_seconds: float | None = None,
    ) -> AsyncIterator[str]:
        resolved, adapter = self._resolve(
            principal, target, InferenceCapability.STREAM_TEXT
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
        async for delta in adapter.stream_text(request):
            if not isinstance(delta, str):
                raise InferenceError(
                    InferenceErrorCategory.INVALID_RESPONSE,
                    retryable=False,
                )
            yield delta

    def generate_structured(
        self,
        *,
        principal: InferencePrincipal,
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
            principal, target, InferenceCapability.GENERATE_STRUCTURED
        )
        max_output_tokens = resolved.max_output_tokens
        if max_output_tokens is None:
            raise AssertionError("structured target requires an output limit")
        provider_result = adapter.generate_structured(
            StructuredGenerationRequest(
                messages=messages,
                model_id=resolved.reference.model_id,
                options=resolved.options,
                max_input_tokens=resolved.max_input_tokens,
                max_output_tokens=max_output_tokens,
                timeout_seconds=self._timeout(resolved.timeout_seconds, timeout_seconds),
                response_schema=response_schema,
            )
        )
        self._validate_text(provider_result)
        try:
            value: JsonValue = json.loads(provider_result.text)
            Draft202012Validator(response_schema).validate(value)
        except (json.JSONDecodeError, ValidationError):
            raise InferenceError(
                InferenceErrorCategory.INVALID_RESPONSE,
                retryable=False,
            ) from None
        return StructuredGenerationResult(value=value, usage=provider_result.usage)

    def embed(
        self,
        *,
        principal: InferencePrincipal,
        target: InferenceTarget,
        inputs: tuple[str, ...],
        timeout_seconds: float | None = None,
    ) -> EmbeddingResult:
        resolved, adapter = self._resolve(
            principal, target, InferenceCapability.EMBED
        )
        return adapter.embed(
            EmbeddingRequest(
                inputs=inputs,
                model_id=resolved.reference.model_id,
                options=resolved.options,
                max_input_tokens=resolved.max_input_tokens,
                timeout_seconds=self._timeout(resolved.timeout_seconds, timeout_seconds),
            )
        )

    def estimate_input_tokens(
        self,
        *,
        principal: InferencePrincipal,
        target: InferenceTarget,
        messages: tuple[InferenceMessage, ...],
        response_schema: Mapping[str, object] | None = None,
        timeout_seconds: float | None = None,
    ) -> TokenEstimate:
        resolved, adapter = self._resolve(
            principal, target, InferenceCapability.ESTIMATE_INPUT_TOKENS
        )
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
        return estimate

    def _resolve(
        self,
        principal: InferencePrincipal,
        target: InferenceTarget,
        capability: InferenceCapability,
    ) -> tuple[ResolvedTarget, InferenceAdapter]:
        self._authorizer.authorize(principal, target)
        resolved = self._settings.target(target)
        adapter = self._registry.adapter(resolved.reference.provider_id)
        if capability not in adapter.capabilities:
            raise InferenceError(
                InferenceErrorCategory.UNSUPPORTED_CAPABILITY,
                retryable=False,
            )
        return resolved, adapter

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
