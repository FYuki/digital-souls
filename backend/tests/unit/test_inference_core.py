from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock
import pytest

from app.inference.authorization import (
    InferenceCaller,
)
from app.inference.config import parse_provider_reference, resolve_inference_settings
from app.inference.contracts import (
    EmbeddingRequest,
    EmbeddingResult,
    InferenceCapability,
    InferenceMessage,
    InferenceTarget,
    InferenceUsage,
    ProviderTextResult,
    StructuredGenerationRequest,
    TextGenerationRequest,
    TextGenerationResult,
    TokenEstimate,
    TokenEstimateAccuracy,
    TokenEstimateRequest,
)
from app.inference.errors import InferenceError, InferenceErrorCategory
from app.inference.registry import default_provider_registry
from app.inference.router import InferenceRouter


def _environment() -> dict[str, str]:
    environment: dict[str, str] = {}
    for token, model in {
        "CHAT": "gemma4:e4b",
        "PRIVACY": "gemma4:e4b",
        "MEMORY_EXTRACTION": "gemma4:e4b",
        "MEMORY_CONSOLIDATION": "gemma4:12b",
        "EMBEDDING": "nomic-embed-text:latest",
    }.items():
        environment[f"INFERENCE_TARGET_{token}"] = f"ollama/{model}"
        environment[f"INFERENCE_TARGET_{token}_MAX_INPUT_TOKENS"] = "8192"
        if token != "EMBEDDING":
            environment[f"INFERENCE_TARGET_{token}_MAX_OUTPUT_TOKENS"] = "1024"
    return environment


class _FakeAdapter:
    provider_id = "ollama"
    capabilities = frozenset(InferenceCapability)

    def __init__(self) -> None:
        self.structured_calls = 0
        self.text_calls = 0
        self.estimate_calls = 0
        self.structured_text = '{"answer":"ok"}'
        self.failure: InferenceError | None = None

    def probe(self, model_id: str, *, timeout_seconds: float) -> None:
        del model_id, timeout_seconds

    def generate_text(self, request: TextGenerationRequest) -> ProviderTextResult:
        del request
        self.text_calls += 1
        if self.failure is not None:
            raise self.failure
        return ProviderTextResult(
            "ok",
            InferenceUsage(3, 2, 5, provider_reported=True),
        )

    async def stream_text(self, request: TextGenerationRequest) -> AsyncIterator[str]:
        del request
        yield "ok"

    def generate_structured(
        self, request: StructuredGenerationRequest
    ) -> ProviderTextResult:
        del request
        self.structured_calls += 1
        return ProviderTextResult(self.structured_text)

    def embed(self, request: EmbeddingRequest) -> EmbeddingResult:
        return EmbeddingResult(tuple((1.0, 2.0) for _ in request.inputs))

    def estimate_input_tokens(self, request: TokenEstimateRequest) -> TokenEstimate:
        del request
        self.estimate_calls += 1
        return TokenEstimate(12, TokenEstimateAccuracy.EXACT, "fixture")


class _BlockingAdapter(_FakeAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.entered = Event()
        self.release = Event()
        self._active = 0
        self.max_active = 0
        self._lock = Lock()

    def generate_text(self, request: TextGenerationRequest) -> ProviderTextResult:
        del request
        with self._lock:
            self.text_calls += 1
            self._active += 1
            self.max_active = max(self.max_active, self._active)
        self.entered.set()
        self.release.wait(timeout=2.0)
        with self._lock:
            self._active -= 1
        return ProviderTextResult("ok")


class _CancellableAdapter(_FakeAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def stream_text(self, request: TextGenerationRequest) -> AsyncIterator[str]:
        del request
        self.entered.set()
        await self.release.wait()
        yield "late"


@pytest.mark.parametrize(
    ("value", "provider_id", "model_id"),
    [
        ("ollama/gemma4:e4b", "ollama", "gemma4:e4b"),
        (
            "openai-api/organization/model/version",
            "openai-api",
            "organization/model/version",
        ),
    ],
)
def test_provider_reference_splits_only_the_first_slash(
    value: str, provider_id: str, model_id: str
) -> None:
    reference = parse_provider_reference(value)

    assert (reference.provider_id, reference.model_id) == (provider_id, model_id)


@pytest.mark.parametrize(
    "value",
    [
        "",
        "ollama",
        "/model",
        "ollama/",
        " ollama/model",
        "ollama/model ",
        "ollama@a/model",
    ],
)
def test_provider_reference_rejects_noncanonical_or_reserved_values(value: str) -> None:
    with pytest.raises(ValueError):
        parse_provider_reference(value)


def test_settings_resolve_all_fixed_targets_and_defaults() -> None:
    registry = default_provider_registry()

    settings = resolve_inference_settings(_environment(), registry)

    assert set(settings.targets) == {
        InferenceTarget.CHAT,
        InferenceTarget.PRIVACY,
        InferenceTarget.MEMORY_EXTRACTION,
        InferenceTarget.MEMORY_CONSOLIDATION,
        InferenceTarget.EMBEDDING,
    }
    chat = settings.target(InferenceTarget.CHAT)
    assert chat.reference.model_id == "gemma4:e4b"
    assert chat.timeout_seconds == 30.0
    assert chat.max_concurrency == 1
    assert chat.options == {}


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("INFERENCE_TARGET_CHATT", "ollama/gemma4:e4b"),
        ("INFERENCE_TARGET_CHAT_UNKNOWN", "1"),
        ("INFERENCE_TARGET_CHAT", "unknown/model"),
        ("INFERENCE_TARGET_CHAT", "ollama/"),
        ("INFERENCE_TARGET_CHAT_OPTIONS_JSON", "[]"),
        ("INFERENCE_TARGET_CHAT_OPTIONS_JSON", "not-json"),
        ("INFERENCE_TARGET_CHAT_OPTIONS_JSON", '{"api_key":"secret"}'),
        ("INFERENCE_TARGET_CHAT_OPTIONS_JSON", '{"unknown":true}'),
        ("INFERENCE_TARGET_CHAT_MAX_INPUT_TOKENS", "0"),
        ("INFERENCE_TARGET_CHAT_MAX_OUTPUT_TOKENS", "01"),
        ("INFERENCE_TARGET_CHAT_TIMEOUT_SECONDS", "nan"),
        ("INFERENCE_TARGET_CHAT_MAX_CONCURRENCY", "1.5"),
    ],
)
def test_settings_reject_invalid_values_before_adapter_execution(
    key: str, value: str
) -> None:
    environment = _environment()
    environment[key] = value

    with pytest.raises(ValueError):
        resolve_inference_settings(environment, default_provider_registry())


def test_settings_require_input_and_generation_limits() -> None:
    environment = _environment()
    del environment["INFERENCE_TARGET_CHAT_MAX_OUTPUT_TOKENS"]

    with pytest.raises(ValueError, match="MAX_OUTPUT_TOKENS"):
        resolve_inference_settings(environment, default_provider_registry())


def test_settings_reject_capability_mismatch_and_privacy_cloud_assignment() -> None:
    registry = default_provider_registry()
    chat_environment = _environment()
    chat_environment["INFERENCE_TARGET_CHAT"] = "openai-codex/gpt-5"
    privacy_environment = _environment()
    privacy_environment["INFERENCE_TARGET_PRIVACY"] = "openai-api/gpt-5"

    with pytest.raises(ValueError, match="lacks capability"):
        resolve_inference_settings(chat_environment, registry)
    with pytest.raises(ValueError, match="requires a local provider"):
        resolve_inference_settings(privacy_environment, registry)


def _router(adapter: _FakeAdapter, *, observer=None) -> InferenceRouter:
    registry = default_provider_registry()
    registry.bind(adapter)
    settings = resolve_inference_settings(_environment(), registry)
    if observer is None:
        return InferenceRouter(settings=settings, registry=registry)
    return InferenceRouter(settings=settings, registry=registry, observer=observer)


def _messages() -> tuple[InferenceMessage, ...]:
    return (InferenceMessage("user", "hello"),)


def test_structured_generation_is_revalidated_without_repair_or_retry() -> None:
    adapter = _FakeAdapter()
    router = _router(adapter)
    schema: Mapping[str, object] = {
        "type": "object",
        "properties": {"answer": {"type": "string"}},
        "required": ["answer"],
        "additionalProperties": False,
    }

    result = router.generate_structured(
        caller=InferenceCaller.SEMANTIC_PRIVACY,
        target=InferenceTarget.PRIVACY,
        messages=_messages(),
        response_schema=schema,
    )
    assert result.value == {"answer": "ok"}

    adapter.structured_text = '{"unexpected":true}'
    with pytest.raises(InferenceError) as exc_info:
        router.generate_structured(
            caller=InferenceCaller.SEMANTIC_PRIVACY,
            target=InferenceTarget.PRIVACY,
            messages=_messages(),
            response_schema=schema,
        )

    assert exc_info.value.category is InferenceErrorCategory.INVALID_RESPONSE
    assert adapter.structured_calls == 2


def test_memory_capabilities_invoke_metadata_only_observer() -> None:
    adapter = _FakeAdapter()
    observations = []
    router = _router(adapter, observer=observations.append)
    schema: Mapping[str, object] = {
        "type": "object",
        "properties": {"answer": {"type": "string"}},
        "required": ["answer"],
        "additionalProperties": False,
    }

    router.generate_structured(
        caller=InferenceCaller.MEMORY_EXTRACTION,
        target=InferenceTarget.MEMORY_EXTRACTION,
        messages=_messages(),
        response_schema=schema,
    )
    router.embed(
        caller=InferenceCaller.MEMORY_INDEX,
        target=InferenceTarget.EMBEDDING,
        inputs=("synthetic",),
    )
    adapter.structured_text = "invalid"
    with pytest.raises(InferenceError):
        router.generate_structured(
            caller=InferenceCaller.MEMORY_CONSOLIDATION,
            target=InferenceTarget.MEMORY_CONSOLIDATION,
            messages=_messages(),
            response_schema=schema,
        )

    assert [observation.success for observation in observations] == [True, True, False]
    assert [observation.capability for observation in observations] == [
        InferenceCapability.GENERATE_STRUCTURED,
        InferenceCapability.EMBED,
        InferenceCapability.GENERATE_STRUCTURED,
    ]
    assert observations[-1].error_category is InferenceErrorCategory.INVALID_RESPONSE
    assert all(observation.provider_id == "ollama" for observation in observations)
    assert all(observation.external_request_count == 1 for observation in observations)
    assert all(not hasattr(observation, "messages") for observation in observations)


def test_stream_cancellation_is_observed_as_failure() -> None:
    adapter = _CancellableAdapter()
    observations = []
    router = _router(adapter, observer=observations.append)

    async def exercise() -> None:
        async def consume() -> None:
            async for _chunk in router.stream_text(
                caller=InferenceCaller.CHAT,
                target=InferenceTarget.CHAT,
                messages=_messages(),
            ):
                pass

        task = asyncio.create_task(consume())
        await adapter.entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(exercise())

    assert len(observations) == 1
    assert observations[0].success is False
    assert observations[0].error_category is InferenceErrorCategory.CANCELLED


def test_router_does_not_retry_or_fallback_and_keeps_errors_sanitized() -> None:
    adapter = _FakeAdapter()
    adapter.failure = InferenceError(
        InferenceErrorCategory.AUTHENTICATION_FAILED,
        retryable=False,
    )
    router = _router(adapter)

    with pytest.raises(InferenceError) as exc_info:
        router.generate_text(
            caller=InferenceCaller.CHAT,
            target=InferenceTarget.CHAT,
            messages=_messages(),
        )

    assert adapter.text_calls == 1
    assert exc_info.value.category is InferenceErrorCategory.AUTHENTICATION_FAILED
    assert "secret" not in str(exc_info.value).lower()


def test_authorization_happens_before_provider_send() -> None:
    adapter = _FakeAdapter()
    router = _router(adapter)

    with pytest.raises(InferenceError) as exc_info:
        router.generate_text(
            caller=InferenceCaller.MEMORY_EXTRACTION,
            target=InferenceTarget.CHAT,
            messages=_messages(),
        )

    assert exc_info.value.category is InferenceErrorCategory.ACCESS_DENIED
    assert adapter.text_calls == 0


def test_token_estimate_and_usage_are_separate_contracts() -> None:
    adapter = _FakeAdapter()
    router = _router(adapter)

    estimate = router.estimate_input_tokens(
        caller=InferenceCaller.CHAT,
        target=InferenceTarget.CHAT,
        messages=_messages(),
    )
    result = router.generate_text(
        caller=InferenceCaller.CHAT,
        target=InferenceTarget.CHAT,
        messages=_messages(),
    )

    assert estimate == TokenEstimate(12, TokenEstimateAccuracy.EXACT, "fixture")
    assert result.usage == InferenceUsage(3, 2, 5, provider_reported=True)
    assert adapter.estimate_calls == 1
    assert adapter.text_calls == 1


def test_router_enforces_target_max_concurrency_in_core() -> None:
    adapter = _BlockingAdapter()
    router = _router(adapter)
    second_attempted = Event()

    def call() -> TextGenerationResult:
        return router.generate_text(
            caller=InferenceCaller.CHAT,
            target=InferenceTarget.CHAT,
            messages=_messages(),
        )

    def call_second() -> TextGenerationResult:
        second_attempted.set()
        return call()

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(call)
        assert adapter.entered.wait(timeout=1.0)
        second = executor.submit(call_second)
        assert second_attempted.wait(timeout=1.0)
        assert adapter.text_calls == 1
        assert not second.done()
        adapter.release.set()
        assert first.result(timeout=1.0).text == "ok"
        assert second.result(timeout=1.0).text == "ok"

    assert adapter.max_active == 1


def test_registry_declares_fixed_provider_kinds_and_capabilities() -> None:
    registry = default_provider_registry()

    assert set(registry.descriptors) == {"ollama", "openai-api", "openai-codex"}
    assert (
        InferenceCapability.GENERATE_STRUCTURED
        not in registry.descriptor("openai-codex").capabilities
    )
    assert registry.descriptor("ollama").kind.value == "local"
