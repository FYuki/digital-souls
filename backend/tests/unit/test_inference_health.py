from __future__ import annotations

import json
import logging
from types import MethodType

import pytest

from app.inference import InferenceCaller, InferenceMessage, InferenceTarget
from app.inference.contracts import InferenceUsage, ProviderTextResult
from app.inference.errors import InferenceError, InferenceErrorCategory
from app.inference.health import InferenceTargetState, InferenceVerification
from app.inference.runtime import create_inference_runtime


def _environment() -> dict[str, str]:
    return {
        "INFERENCE_TARGET_CHAT": "ollama/chat:latest",
        "INFERENCE_TARGET_CHAT_MAX_INPUT_TOKENS": "100",
        "INFERENCE_TARGET_CHAT_MAX_OUTPUT_TOKENS": "20",
        "INFERENCE_TARGET_PRIVACY": "ollama/privacy:latest",
        "INFERENCE_TARGET_PRIVACY_MAX_INPUT_TOKENS": "100",
        "INFERENCE_TARGET_PRIVACY_MAX_OUTPUT_TOKENS": "20",
        "INFERENCE_TARGET_MEMORY_EXTRACTION": "ollama/extract:latest",
        "INFERENCE_TARGET_MEMORY_EXTRACTION_MAX_INPUT_TOKENS": "100",
        "INFERENCE_TARGET_MEMORY_EXTRACTION_MAX_OUTPUT_TOKENS": "20",
        "INFERENCE_TARGET_MEMORY_CONSOLIDATION": "ollama/consolidate:latest",
        "INFERENCE_TARGET_MEMORY_CONSOLIDATION_MAX_INPUT_TOKENS": "100",
        "INFERENCE_TARGET_MEMORY_CONSOLIDATION_MAX_OUTPUT_TOKENS": "20",
        "INFERENCE_TARGET_EMBEDDING": "ollama/embed:latest",
        "INFERENCE_TARGET_EMBEDDING_MAX_INPUT_TOKENS": "100",
    }


def test_health_starts_unverified_and_keeps_optional_target_unconfigured() -> None:
    runtime = create_inference_runtime(_environment())
    try:
        states = {item.target: item for item in runtime.health.snapshot()}

        assert states[InferenceTarget.CHAT].state is InferenceTargetState.DEGRADED
        assert (
            states[InferenceTarget.CHAT].verification
            is InferenceVerification.UNVERIFIED
        )
        assert (
            states[InferenceTarget.HEAVY_REASONING].state
            is InferenceTargetState.UNCONFIGURED
        )
        assert runtime.health.is_ready() is False
    finally:
        runtime.close()


def test_startup_probe_marks_targets_ready_without_sending_inference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = create_inference_runtime(_environment())
    calls: list[str] = []
    monkeypatch.setattr(
        runtime.ollama_adapter,
        "probe",
        lambda model_id, *, timeout_seconds: calls.append(model_id),
    )
    try:
        runtime.probe_startup()

        assert calls == [
            "chat:latest",
            "privacy:latest",
            "extract:latest",
            "consolidate:latest",
            "embed:latest",
        ]
        assert runtime.health.is_ready() is True
    finally:
        runtime.close()


def test_required_probe_failure_aborts_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = create_inference_runtime(_environment())
    calls: list[str] = []

    def fail_chat(model_id: str, *, timeout_seconds: float) -> None:
        del timeout_seconds
        calls.append(model_id)
        if model_id == "chat:latest":
            raise InferenceError(InferenceErrorCategory.MODEL_NOT_FOUND, retryable=False)

    monkeypatch.setattr(runtime.ollama_adapter, "probe", fail_chat)
    try:
        with pytest.raises(InferenceError) as error:
            runtime.probe_startup()

        assert error.value.category is InferenceErrorCategory.MODEL_NOT_FOUND
        assert runtime.health.is_ready() is False
        chat = next(
            item
            for item in runtime.health.snapshot()
            if item.target is InferenceTarget.CHAT
        )
        assert chat.state is InferenceTargetState.INVALID
        assert calls == [
            "chat:latest",
            "privacy:latest",
            "extract:latest",
            "consolidate:latest",
            "embed:latest",
        ]
    finally:
        runtime.close()


def test_degradable_probe_failure_is_exposed_without_aborting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = create_inference_runtime(_environment())

    def fail_embedding(model_id: str, *, timeout_seconds: float) -> None:
        del timeout_seconds
        if model_id == "embed:latest":
            raise InferenceError(InferenceErrorCategory.UNAVAILABLE, retryable=True)

    monkeypatch.setattr(runtime.ollama_adapter, "probe", fail_embedding)
    try:
        runtime.probe_startup()
        states = {item.target: item for item in runtime.health.snapshot()}

        assert runtime.health.is_ready() is True
        assert (
            states[InferenceTarget.EMBEDDING].state
            is InferenceTargetState.DEGRADED
        )
        assert (
            states[InferenceTarget.EMBEDDING].error_category
            is InferenceErrorCategory.UNAVAILABLE
        )
    finally:
        runtime.close()


def test_structured_observation_contains_metadata_only(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    runtime = create_inference_runtime(_environment())

    def generate_text(_self, request):
        del request
        return ProviderTextResult(
            "PRIVATE_RESPONSE",
            InferenceUsage(2, 3, 5, provider_reported=True),
        )

    monkeypatch.setattr(
        runtime.ollama_adapter,
        "generate_text",
        MethodType(generate_text, runtime.ollama_adapter),
    )
    caplog.set_level(logging.INFO, logger="app.inference.runtime")
    try:
        runtime.router.generate_text(
            caller=InferenceCaller.CHAT,
            target=InferenceTarget.CHAT,
            messages=(InferenceMessage("user", "PRIVATE_PROMPT"),),
        )
    finally:
        runtime.close()

    payload = json.loads(caplog.records[-1].message)
    assert payload["event"] == "inference_request"
    assert payload["request_id"]
    assert payload["target"] == "chat"
    assert payload["provider"] == "ollama"
    assert payload["model"] == "chat:latest"
    assert payload["auth_kind"] == "none"
    assert payload["external_request_count"] == 1
    assert payload["usage"]["total"] == 5
    assert payload["success"] is True
    serialized = json.dumps(payload)
    assert "PRIVATE_PROMPT" not in serialized
    assert "PRIVATE_RESPONSE" not in serialized


def test_local_token_estimate_does_not_mark_cloud_target_ready() -> None:
    environment = {
        **_environment(),
        "INFERENCE_TARGET_HEAVY_REASONING": "openai-api/gpt-5.6-sol",
        "INFERENCE_TARGET_HEAVY_REASONING_MAX_INPUT_TOKENS": "100",
        "INFERENCE_TARGET_HEAVY_REASONING_MAX_OUTPUT_TOKENS": "20",
        "OPENAI_API_KEY": "synthetic-test-key",
    }
    runtime = create_inference_runtime(environment)
    try:
        runtime.router.estimate_input_tokens(
            caller=InferenceCaller.HEAVY_REASONING,
            target=InferenceTarget.HEAVY_REASONING,
            messages=(InferenceMessage("user", "synthetic"),),
        )
        heavy = next(
            item
            for item in runtime.health.snapshot()
            if item.target is InferenceTarget.HEAVY_REASONING
        )

        assert heavy.state is InferenceTargetState.DEGRADED
        assert heavy.last_checked_at is None
    finally:
        runtime.close()


def test_health_api_exposes_only_approved_target_fields() -> None:
    from app import main

    runtime = create_inference_runtime(_environment())
    main.app.state.inference_health = runtime.health
    not_ready_response = main.inference_readiness()
    for target in runtime.settings.targets:
        runtime.health.record_success(target)
    try:
        ready_response = main.inference_readiness()
        detail_response = main.inference_health()
    finally:
        del main.app.state.inference_health
        runtime.close()

    assert not_ready_response.status_code == 503
    assert json.loads(not_ready_response.body) == {"status": "not_ready"}
    assert ready_response.status_code == 200
    assert json.loads(ready_response.body) == {"status": "ready"}
    payload = json.loads(detail_response.body)
    assert set(payload) == {"targets"}
    assert set(payload["targets"][0]) == {
        "target",
        "state",
        "verification",
        "required_capabilities",
        "error_category",
        "last_checked_at",
    }
    serialized = json.dumps(payload)
    assert "ollama" not in serialized
    assert "chat:latest" not in serialized
    assert "auth" not in serialized
