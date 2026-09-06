from __future__ import annotations

from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
import logging
from threading import Event, Lock
from types import MethodType

from PIL import Image
import pytest

from app.inference import (
    InferenceCaller,
    InferenceCancellationToken,
    InferenceCapability,
    InferenceError,
    InferenceErrorCategory,
    InferenceImagePart,
    InferenceMessage,
    InferenceTarget,
    InferenceTextPart,
)
from app.inference.config import resolve_inference_settings
from app.inference.contracts import (
    EmbeddingRequest,
    EmbeddingResult,
    ModelProbeResult,
    ProviderTextResult,
    StructuredGenerationRequest,
    TextGenerationRequest,
    TokenEstimate,
    TokenEstimateAccuracy,
    TokenEstimateRequest,
)
from app.inference.health import (
    InferenceTargetState,
    InferenceVerification,
)
from app.inference.registry import default_provider_registry
from app.inference.router import InferenceRouter
from app.inference.runtime import create_inference_runtime
from app.screen_perception.vision import (
    VISION_OBSERVATION_SCHEMA,
    VisionInferenceClient,
)


def _environment(*, vision: bool = True) -> dict[str, str]:
    environment = {
        "INFERENCE_TARGET_CHAT": "ollama/chat:latest",
        "INFERENCE_TARGET_CHAT_MAX_INPUT_TOKENS": "7168",
        "INFERENCE_TARGET_CHAT_MAX_OUTPUT_TOKENS": "1024",
        "INFERENCE_TARGET_PRIVACY": "ollama/privacy:latest",
        "INFERENCE_TARGET_PRIVACY_MAX_INPUT_TOKENS": "7680",
        "INFERENCE_TARGET_PRIVACY_MAX_OUTPUT_TOKENS": "512",
        "INFERENCE_TARGET_MEMORY_EXTRACTION": "ollama/extract:latest",
        "INFERENCE_TARGET_MEMORY_EXTRACTION_MAX_INPUT_TOKENS": "7680",
        "INFERENCE_TARGET_MEMORY_EXTRACTION_MAX_OUTPUT_TOKENS": "512",
        "INFERENCE_TARGET_MEMORY_CONSOLIDATION": "ollama/consolidate:latest",
        "INFERENCE_TARGET_MEMORY_CONSOLIDATION_MAX_INPUT_TOKENS": "7680",
        "INFERENCE_TARGET_MEMORY_CONSOLIDATION_MAX_OUTPUT_TOKENS": "512",
        "INFERENCE_TARGET_EMBEDDING": "ollama/embed:latest",
        "INFERENCE_TARGET_EMBEDDING_MAX_INPUT_TOKENS": "8192",
    }
    if vision:
        environment.update(
            {
                "INFERENCE_TARGET_VISION": "ollama/gemma4:e4b",
                "INFERENCE_TARGET_VISION_MAX_INPUT_TOKENS": "7168",
                "INFERENCE_TARGET_VISION_MAX_OUTPUT_TOKENS": "1024",
                "INFERENCE_TARGET_VISION_TIMEOUT_SECONDS": "30",
                "INFERENCE_TARGET_VISION_MAX_CONCURRENCY": "1",
            }
        )
    return environment


def _image_bytes(
    *, image_format: str = "PNG", size: tuple[int, int] = (2, 2)
) -> bytes:
    output = BytesIO()
    Image.new("RGB", size, color=(12, 34, 56)).save(output, format=image_format)
    return output.getvalue()


def _image(**overrides: object) -> InferenceImagePart:
    values: dict[str, object] = {
        "data": _image_bytes(),
        "mime_type": "image/png",
        "width": 2,
        "height": 2,
    }
    values.update(overrides)
    return InferenceImagePart(**values)  # type: ignore[arg-type]


def _messages(*images: InferenceImagePart) -> tuple[InferenceMessage, ...]:
    return (
        InferenceMessage(
            "user",
            (InferenceTextPart("この画面を読んで"), *images),
        ),
    )


class _VisionAdapter:
    provider_id = "ollama"
    capabilities = frozenset(InferenceCapability)

    def __init__(self) -> None:
        self.structured_calls = 0
        self.text_calls = 0
        self.estimate_calls = 0
        self.estimated_tokens = 1200
        self.last_request: StructuredGenerationRequest | None = None
        self.structured_text = (
            '{"recognized_content":"設定画面",'
            '"unreadable_regions_or_reasons":[],"uncertainty":"低い"}'
        )
        self.on_structured = lambda: None

    def probe(self, model_id: str, *, timeout_seconds: float) -> ModelProbeResult:
        del model_id, timeout_seconds
        return ModelProbeResult(frozenset({InferenceCapability.IMAGE_INPUT}))

    def generate_text(self, request: TextGenerationRequest) -> ProviderTextResult:
        del request
        self.text_calls += 1
        return ProviderTextResult("ok")

    async def stream_text(
        self, request: TextGenerationRequest
    ) -> AsyncIterator[str]:
        del request
        yield "ok"

    def generate_structured(
        self, request: StructuredGenerationRequest
    ) -> ProviderTextResult:
        self.structured_calls += 1
        self.last_request = request
        self.on_structured()
        return ProviderTextResult(self.structured_text)

    def embed(self, request: EmbeddingRequest) -> EmbeddingResult:
        return EmbeddingResult(tuple((1.0,) for _ in request.inputs))

    def estimate_input_tokens(self, request: TokenEstimateRequest) -> TokenEstimate:
        del request
        self.estimate_calls += 1
        return TokenEstimate(
            self.estimated_tokens,
            TokenEstimateAccuracy.ESTIMATED,
            "fixture",
        )


class _BlockingVisionAdapter(_VisionAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.entered = Event()
        self.release = Event()
        self._active = 0
        self.max_active = 0
        self._lock = Lock()

    def generate_structured(
        self, request: StructuredGenerationRequest
    ) -> ProviderTextResult:
        del request
        with self._lock:
            self.structured_calls += 1
            self._active += 1
            self.max_active = max(self.max_active, self._active)
        self.entered.set()
        self.release.wait(timeout=2.0)
        with self._lock:
            self._active -= 1
        return ProviderTextResult(self.structured_text)


def _router(adapter: _VisionAdapter) -> InferenceRouter:
    registry = default_provider_registry()
    registry.bind(adapter)
    return InferenceRouter(
        settings=resolve_inference_settings(_environment(), registry),
        registry=registry,
    )


def test_vision_client_returns_only_schema_validated_observation() -> None:
    adapter = _VisionAdapter()
    client = VisionInferenceClient(router=_router(adapter))

    observation = client.observe(
        question="この画面を読んで",
        image=_image(),
        timeout_seconds=45.0,
    )

    assert observation.recognized_content == "設定画面"
    assert observation.unreadable_regions_or_reasons == ()
    assert observation.uncertainty == "低い"
    assert adapter.estimate_calls == 1
    assert adapter.structured_calls == 1
    assert adapter.last_request is not None
    assert adapter.last_request.timeout_seconds == 30.0
    assert adapter.last_request.response_schema is VISION_OBSERVATION_SCHEMA
    assert any(
        isinstance(part, InferenceImagePart)
        for message in adapter.last_request.messages
        if isinstance(message.content, tuple)
        for part in message.content
    )


def test_vision_rejects_schema_mismatch_without_repair_or_retry() -> None:
    adapter = _VisionAdapter()
    adapter.structured_text = '{"recognized_content":"private"}'

    with pytest.raises(InferenceError) as exc_info:
        VisionInferenceClient(router=_router(adapter)).observe(
            question="この画面を読んで",
            image=_image(),
        )

    assert exc_info.value.category is InferenceErrorCategory.INVALID_RESPONSE
    assert adapter.structured_calls == 1


@pytest.mark.parametrize(
    "image",
    [
        _image(mime_type="image/webp"),
        _image(data=b"not-an-image"),
        _image(width=3),
        _image(width=2561),
        _image(width=2049, height=2049),
    ],
)
def test_core_rejects_invalid_images_before_adapter_send(
    image: InferenceImagePart,
) -> None:
    adapter = _VisionAdapter()

    with pytest.raises(InferenceError) as exc_info:
        _router(adapter).generate_structured(
            caller=InferenceCaller.SCREEN_VISION,
            target=InferenceTarget.VISION,
            messages=_messages(image),
            response_schema=VISION_OBSERVATION_SCHEMA,
        )

    assert exc_info.value.category is InferenceErrorCategory.INVALID_REQUEST
    assert adapter.structured_calls == 0


def test_core_rejects_multiple_images_and_oversized_bytes() -> None:
    adapter = _VisionAdapter()
    router = _router(adapter)
    oversized = _image(data=b"\x89PNG\r\n\x1a\n" + b"x" * 5_242_880)

    for messages in (_messages(_image(), _image()), _messages(oversized)):
        with pytest.raises(InferenceError) as exc_info:
            router.generate_structured(
                caller=InferenceCaller.SCREEN_VISION,
                target=InferenceTarget.VISION,
                messages=messages,
                response_schema=VISION_OBSERVATION_SCHEMA,
            )
        assert exc_info.value.category is InferenceErrorCategory.INVALID_REQUEST

    assert adapter.structured_calls == 0


@pytest.mark.parametrize(
    ("image_format", "mime_type"),
    [("PNG", "image/png"), ("JPEG", "image/jpeg")],
)
def test_core_accepts_supported_decoded_image_formats(
    image_format: str,
    mime_type: str,
) -> None:
    adapter = _VisionAdapter()
    image = _image(data=_image_bytes(image_format=image_format), mime_type=mime_type)

    _router(adapter).generate_structured(
        caller=InferenceCaller.SCREEN_VISION,
        target=InferenceTarget.VISION,
        messages=_messages(image),
        response_schema=VISION_OBSERVATION_SCHEMA,
    )

    assert adapter.structured_calls == 1


def test_core_rejects_animated_png() -> None:
    output = BytesIO()
    frames = [Image.new("RGB", (2, 2), color=value) for value in ("red", "blue")]
    frames[0].save(
        output,
        format="PNG",
        save_all=True,
        append_images=frames[1:],
        duration=100,
        loop=0,
    )
    adapter = _VisionAdapter()

    with pytest.raises(InferenceError) as exc_info:
        _router(adapter).generate_structured(
            caller=InferenceCaller.SCREEN_VISION,
            target=InferenceTarget.VISION,
            messages=_messages(_image(data=output.getvalue())),
            response_schema=VISION_OBSERVATION_SCHEMA,
        )

    assert exc_info.value.category is InferenceErrorCategory.INVALID_REQUEST
    assert adapter.structured_calls == 0


def test_image_repr_and_errors_do_not_expose_payload() -> None:
    private = b"PRIVATE_IMAGE_BYTES"
    image = _image(data=private)

    assert "PRIVATE_IMAGE_BYTES" not in repr(image)
    with pytest.raises(InferenceError) as exc_info:
        _router(_VisionAdapter()).generate_structured(
            caller=InferenceCaller.SCREEN_VISION,
            target=InferenceTarget.VISION,
            messages=_messages(image),
            response_schema=VISION_OBSERVATION_SCHEMA,
        )
    assert "PRIVATE" not in str(exc_info.value)


def test_cancelled_vision_result_is_not_adopted() -> None:
    adapter = _VisionAdapter()
    router = _router(adapter)
    token = InferenceCancellationToken()
    adapter.on_structured = token.cancel

    with pytest.raises(InferenceError) as exc_info:
        router.generate_structured(
            caller=InferenceCaller.SCREEN_VISION,
            target=InferenceTarget.VISION,
            messages=_messages(_image()),
            response_schema=VISION_OBSERVATION_SCHEMA,
            cancellation_token=token,
        )

    assert exc_info.value.category is InferenceErrorCategory.CANCELLED
    assert adapter.structured_calls == 1


def test_pre_cancelled_vision_does_not_decode_or_send() -> None:
    adapter = _VisionAdapter()
    token = InferenceCancellationToken()
    token.cancel()

    with pytest.raises(InferenceError) as exc_info:
        _router(adapter).generate_structured(
            caller=InferenceCaller.SCREEN_VISION,
            target=InferenceTarget.VISION,
            messages=_messages(_image(data=b"PRIVATE_INVALID_IMAGE")),
            response_schema=VISION_OBSERVATION_SCHEMA,
            cancellation_token=token,
        )

    assert exc_info.value.category is InferenceErrorCategory.CANCELLED
    assert adapter.structured_calls == 0


def test_vision_target_enforces_configured_single_concurrency() -> None:
    adapter = _BlockingVisionAdapter()
    router = _router(adapter)
    second_attempted = Event()

    def call() -> object:
        return router.generate_structured(
            caller=InferenceCaller.SCREEN_VISION,
            target=InferenceTarget.VISION,
            messages=_messages(_image()),
            response_schema=VISION_OBSERVATION_SCHEMA,
        )

    def call_second() -> object:
        second_attempted.set()
        return call()

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(call)
        assert adapter.entered.wait(timeout=1.0)
        second = executor.submit(call_second)
        assert second_attempted.wait(timeout=1.0)
        assert adapter.structured_calls == 1
        assert not second.done()
        adapter.release.set()
        first.result(timeout=1.0)
        second.result(timeout=1.0)

    assert adapter.max_active == 1


def test_non_image_provider_cannot_be_assigned_to_vision() -> None:
    environment = _environment()
    environment["INFERENCE_TARGET_VISION"] = "openai-codex/gpt-5.6-sol"

    with pytest.raises(ValueError, match="lacks capability"):
        resolve_inference_settings(environment, default_provider_registry())


def test_image_is_rejected_on_target_without_image_capability() -> None:
    adapter = _VisionAdapter()

    with pytest.raises(InferenceError) as exc_info:
        _router(adapter).generate_text(
            caller=InferenceCaller.CHAT,
            target=InferenceTarget.CHAT,
            messages=_messages(_image()),
        )

    assert exc_info.value.category is InferenceErrorCategory.UNSUPPORTED_CAPABILITY
    assert adapter.text_calls == 0


def test_vision_rejects_missing_image_before_adapter_send() -> None:
    adapter = _VisionAdapter()

    with pytest.raises(InferenceError) as exc_info:
        _router(adapter).generate_structured(
            caller=InferenceCaller.SCREEN_VISION,
            target=InferenceTarget.VISION,
            messages=(InferenceMessage("user", "この画面を読んで"),),
            response_schema=VISION_OBSERVATION_SCHEMA,
        )

    assert exc_info.value.category is InferenceErrorCategory.INVALID_REQUEST
    assert adapter.structured_calls == 0


def test_vision_input_token_limit_rejects_before_generation() -> None:
    adapter = _VisionAdapter()
    adapter.estimated_tokens = 7169

    with pytest.raises(InferenceError) as exc_info:
        VisionInferenceClient(router=_router(adapter)).observe(
            question="この画面を読んで",
            image=_image(),
        )

    assert exc_info.value.category is InferenceErrorCategory.INVALID_REQUEST
    assert adapter.estimate_calls == 1
    assert adapter.structured_calls == 0


def test_vision_unconfigured_keeps_backend_configuration_valid() -> None:
    settings = resolve_inference_settings(
        _environment(vision=False), default_provider_registry()
    )

    assert InferenceTarget.VISION not in settings.targets


def test_vision_registry_owns_image_and_execution_limits() -> None:
    settings = resolve_inference_settings(_environment(), default_provider_registry())
    vision = settings.target(InferenceTarget.VISION)
    limits = vision.definition.image_limits

    assert vision.max_input_tokens == 7168
    assert vision.max_output_tokens == 1024
    assert vision.timeout_seconds == 30.0
    assert vision.max_concurrency == 1
    assert limits is not None
    assert limits.allowed_mime_types == {"image/png", "image/jpeg"}
    assert limits.max_images == 1
    assert limits.max_bytes == 5_242_880
    assert (limits.max_width, limits.max_height) == (2_560, 2_560)
    assert limits.max_pixels == 4_194_304


@pytest.mark.parametrize(
    "key",
    [
        "INFERENCE_TARGET_VISION_MAX_INPUT_TOKENS",
        "INFERENCE_TARGET_VISION_MAX_OUTPUT_TOKENS",
    ],
)
def test_configured_vision_requires_generation_limits(key: str) -> None:
    environment = _environment()
    del environment[key]

    with pytest.raises(ValueError, match=key):
        resolve_inference_settings(environment, default_provider_registry())


def test_vision_unknown_setting_fails_fast() -> None:
    environment = _environment()
    environment["INFERENCE_TARGET_VISION_MAX_IMAGE_BYTES"] = "1"

    with pytest.raises(ValueError, match="unknown inference target setting"):
        resolve_inference_settings(environment, default_provider_registry())


@pytest.mark.parametrize(
    ("failure", "expected_state"),
    [
        (
            InferenceError(
                InferenceErrorCategory.MODEL_NOT_FOUND,
                retryable=False,
            ),
            InferenceTargetState.INVALID,
        ),
        (
            InferenceError(InferenceErrorCategory.UNAVAILABLE, retryable=True),
            InferenceTargetState.DEGRADED,
        ),
    ],
)
def test_vision_probe_failure_does_not_stop_chat(
    monkeypatch: pytest.MonkeyPatch,
    failure: InferenceError,
    expected_state: InferenceTargetState,
) -> None:
    runtime = create_inference_runtime(_environment())

    def probe(model_id: str, *, timeout_seconds: float) -> ModelProbeResult:
        del timeout_seconds
        if model_id == "gemma4:e4b":
            raise failure
        return ModelProbeResult()

    monkeypatch.setattr(runtime.ollama_adapter, "probe", probe)
    try:
        runtime.probe_startup()
        states = {item.target: item for item in runtime.health.snapshot()}

        assert states[InferenceTarget.CHAT].state is InferenceTargetState.READY
        assert states[InferenceTarget.VISION].state is expected_state
        assert runtime.health.is_ready() is True
    finally:
        runtime.close()


def test_model_metadata_marks_non_vision_ollama_model_unsupported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = create_inference_runtime(_environment())

    def probe(model_id: str, *, timeout_seconds: float) -> ModelProbeResult:
        del timeout_seconds
        capabilities = (
            frozenset()
            if model_id == "gemma4:e4b"
            else None
        )
        return ModelProbeResult(capabilities)

    monkeypatch.setattr(runtime.ollama_adapter, "probe", probe)
    try:
        runtime.probe_startup()
        vision = next(
            item
            for item in runtime.health.snapshot()
            if item.target is InferenceTarget.VISION
        )

        assert vision.state is InferenceTargetState.INVALID
        assert vision.error_category is InferenceErrorCategory.UNSUPPORTED_CAPABILITY
        assert runtime.health.is_ready() is True
    finally:
        runtime.close()


def test_unknown_model_capability_is_exposed_as_unverified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = create_inference_runtime(_environment())
    monkeypatch.setattr(
        runtime.ollama_adapter,
        "probe",
        lambda _model_id, *, timeout_seconds: ModelProbeResult(),
    )
    try:
        runtime.probe_startup()
        vision = next(
            item
            for item in runtime.health.snapshot()
            if item.target is InferenceTarget.VISION
        )

        assert vision.state is InferenceTargetState.READY
        assert vision.verification is InferenceVerification.UNVERIFIED
    finally:
        runtime.close()


@pytest.mark.parametrize(
    "category",
    [
        InferenceErrorCategory.TIMEOUT,
        InferenceErrorCategory.RATE_LIMITED,
        InferenceErrorCategory.INVALID_RESPONSE,
    ],
)
def test_vision_runtime_failures_are_sanitized_and_do_not_change_chat(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    category: InferenceErrorCategory,
) -> None:
    runtime = create_inference_runtime(_environment())
    runtime.health.record_success(InferenceTarget.CHAT)

    def fail(_self: object, request: StructuredGenerationRequest) -> ProviderTextResult:
        del request
        raise InferenceError(
            category,
            retryable=category is not InferenceErrorCategory.INVALID_RESPONSE,
        )

    monkeypatch.setattr(
        runtime.ollama_adapter,
        "generate_structured",
        MethodType(fail, runtime.ollama_adapter),
    )
    caplog.set_level(logging.INFO, logger="app.inference.runtime")
    try:
        with pytest.raises(InferenceError) as exc_info:
            runtime.router.generate_structured(
                caller=InferenceCaller.SCREEN_VISION,
                target=InferenceTarget.VISION,
                messages=_messages(_image()),
                response_schema=VISION_OBSERVATION_SCHEMA,
            )
        states = {item.target: item for item in runtime.health.snapshot()}

        assert exc_info.value.category is category
        assert states[InferenceTarget.CHAT].state is InferenceTargetState.READY
        assert states[InferenceTarget.VISION].error_category is category
        assert "この画面を読んで" not in caplog.text
        assert "iVBOR" not in caplog.text
    finally:
        runtime.close()


def test_success_log_does_not_contain_question_image_or_observation(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    runtime = create_inference_runtime(_environment())
    private_observation = (
        '{"recognized_content":"PRIVATE_OBSERVATION",'
        '"unreadable_regions_or_reasons":[],"uncertainty":"PRIVATE_UNCERTAINTY"}'
    )

    def succeed(
        _self: object, request: StructuredGenerationRequest
    ) -> ProviderTextResult:
        del request
        return ProviderTextResult(private_observation)

    monkeypatch.setattr(
        runtime.ollama_adapter,
        "generate_structured",
        MethodType(succeed, runtime.ollama_adapter),
    )
    caplog.set_level(logging.INFO, logger="app.inference.runtime")
    try:
        runtime.router.generate_structured(
            caller=InferenceCaller.SCREEN_VISION,
            target=InferenceTarget.VISION,
            messages=_messages(_image()),
            response_schema=VISION_OBSERVATION_SCHEMA,
        )

        assert "この画面を読んで" not in caplog.text
        assert "iVBOR" not in caplog.text
        assert "PRIVATE_OBSERVATION" not in caplog.text
        assert "PRIVATE_UNCERTAINTY" not in caplog.text
    finally:
        runtime.close()
