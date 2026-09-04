from __future__ import annotations

import logging
from pathlib import Path

import pytest

from app.inference.runtime import (
    create_inference_runtime,
    reject_legacy_inference_environment,
)


def _target_environment() -> dict[str, str]:
    return {
        "INFERENCE_TARGET_CHAT": "ollama/gemma4:e4b",
        "INFERENCE_TARGET_CHAT_MAX_INPUT_TOKENS": "7168",
        "INFERENCE_TARGET_CHAT_MAX_OUTPUT_TOKENS": "1024",
        "INFERENCE_TARGET_PRIVACY": "ollama/gemma4:e4b",
        "INFERENCE_TARGET_PRIVACY_MAX_INPUT_TOKENS": "7680",
        "INFERENCE_TARGET_PRIVACY_MAX_OUTPUT_TOKENS": "512",
        "INFERENCE_TARGET_MEMORY_EXTRACTION": "ollama/gemma4:e4b",
        "INFERENCE_TARGET_MEMORY_EXTRACTION_MAX_INPUT_TOKENS": "7680",
        "INFERENCE_TARGET_MEMORY_EXTRACTION_MAX_OUTPUT_TOKENS": "512",
        "INFERENCE_TARGET_MEMORY_CONSOLIDATION": "ollama/gemma4:e4b",
        "INFERENCE_TARGET_MEMORY_CONSOLIDATION_MAX_INPUT_TOKENS": "7680",
        "INFERENCE_TARGET_MEMORY_CONSOLIDATION_MAX_OUTPUT_TOKENS": "512",
        "INFERENCE_TARGET_EMBEDDING": "ollama/nomic-embed-text:latest",
        "INFERENCE_TARGET_EMBEDDING_MAX_INPUT_TOKENS": "8192",
    }


@pytest.mark.parametrize(
    "legacy_key",
    [
        "OLLAMA_CHAT_MODEL",
        "OLLAMA_CLASSIFIER_MODEL",
        "OLLAMA_EXTRACTOR_MODEL",
        "OLLAMA_EMBEDDING_MODEL",
        "OLLAMA_CONTEXT_TOKENS",
        "OLLAMA_RESPONSE_RESERVE_TOKENS",
    ],
)
def test_legacy_inference_settings_are_always_rejected(legacy_key: str) -> None:
    with pytest.raises(ValueError, match=f"forbidden: {legacy_key}"):
        reject_legacy_inference_environment({legacy_key: "legacy-value"})


def test_ollama_only_runtime_does_not_require_openai_credentials() -> None:
    runtime = create_inference_runtime(_target_environment())
    try:
        assert runtime.openai_api_adapter is None
        assert runtime.openai_codex_adapter is None
    finally:
        runtime.close()


def test_openai_api_is_bound_only_when_selected_and_requires_key(
    caplog: pytest.LogCaptureFixture,
) -> None:
    environment = _target_environment()
    environment["INFERENCE_TARGET_HEAVY_REASONING"] = "openai-api/gpt-5.6-sol"
    environment["INFERENCE_TARGET_HEAVY_REASONING_MAX_INPUT_TOKENS"] = "8192"
    environment["INFERENCE_TARGET_HEAVY_REASONING_MAX_OUTPUT_TOKENS"] = "1024"

    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        create_inference_runtime(environment)

    caplog.set_level(logging.WARNING)
    environment["OPENAI_API_KEY"] = "test-api-key"
    runtime = create_inference_runtime(environment)
    try:
        assert runtime.openai_api_adapter is not None
        assert "target=heavy-reasoning" in caplog.text
        assert "provider=openai-api" in caplog.text
        assert "model=gpt-5.6-sol" in caplog.text
        assert "test-api-key" not in caplog.text
    finally:
        runtime.close()


def test_openai_api_endpoint_override_is_always_rejected() -> None:
    environment = _target_environment()
    environment.update(
        {
            "INFERENCE_TARGET_HEAVY_REASONING": "openai-api/gpt-5.6-sol",
            "INFERENCE_TARGET_HEAVY_REASONING_MAX_INPUT_TOKENS": "8192",
            "INFERENCE_TARGET_HEAVY_REASONING_MAX_OUTPUT_TOKENS": "1024",
            "OPENAI_API_KEY": "test-api-key",
            "OPENAI_BASE_URL": "https://example.invalid/v1",
        }
    )

    with pytest.raises(ValueError, match="endpoint override is forbidden"):
        create_inference_runtime(environment)


def test_codex_is_bound_only_when_selected_and_uses_dedicated_paths(
    tmp_path: Path,
) -> None:
    environment = _target_environment()
    environment.update(
        {
            "INFERENCE_TARGET_HEAVY_REASONING": "openai-codex/gpt-5.6-sol",
            "INFERENCE_TARGET_HEAVY_REASONING_MAX_INPUT_TOKENS": "8192",
            "INFERENCE_TARGET_HEAVY_REASONING_MAX_OUTPUT_TOKENS": "1024",
        }
    )

    with pytest.raises(ValueError, match="OPENAI_CODEX_HOME"):
        create_inference_runtime(environment)

    codex_home = tmp_path / "codex-auth"
    codex_home.mkdir()
    executable = tmp_path / "codex"
    executable.write_text("synthetic executable", encoding="utf-8")
    executable.chmod(0o700)
    environment.update(
        {
            "OPENAI_CODEX_HOME": str(codex_home),
            "OPENAI_CODEX_EXECUTABLE": str(executable),
        }
    )
    runtime = create_inference_runtime(environment)
    try:
        assert runtime.openai_codex_adapter is not None
    finally:
        runtime.close()
