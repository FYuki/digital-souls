from __future__ import annotations

import logging
from pathlib import Path

import pytest

from app.inference.runtime import (
    create_inference_runtime,
    transition_inference_environment,
)


def _target_environment() -> dict[str, str]:
    return transition_inference_environment({})


def test_legacy_settings_map_to_fixed_targets_with_deprecation_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING)

    result = transition_inference_environment(
        {
            "OLLAMA_CHAT_MODEL": "chat:4b",
            "OLLAMA_CLASSIFIER_MODEL": "privacy:4b",
            "OLLAMA_EXTRACTOR_MODEL": "memory:12b",
            "OLLAMA_EMBEDDING_MODEL": "embed:latest",
            "OLLAMA_CONTEXT_TOKENS": "8192",
            "OLLAMA_RESPONSE_RESERVE_TOKENS": "1024",
        }
    )

    assert result["INFERENCE_TARGET_CHAT"] == "ollama/chat:4b"
    assert result["INFERENCE_TARGET_PRIVACY"] == "ollama/privacy:4b"
    assert result["INFERENCE_TARGET_MEMORY_EXTRACTION"] == "ollama/memory:12b"
    assert result["INFERENCE_TARGET_MEMORY_CONSOLIDATION"] == "ollama/memory:12b"
    assert result["INFERENCE_TARGET_EMBEDDING"] == "ollama/embed:latest"
    assert result["INFERENCE_TARGET_CHAT_MAX_INPUT_TOKENS"] == "7168"
    assert "deprecated" in caplog.text


def test_new_target_settings_are_not_rewritten() -> None:
    environment = {"INFERENCE_TARGET_CHAT": "ollama/custom/model"}

    assert transition_inference_environment(environment) == environment


def test_new_and_legacy_settings_are_rejected_even_when_equivalent() -> None:
    with pytest.raises(ValueError, match="must not be configured together"):
        transition_inference_environment(
            {
                "INFERENCE_TARGET_CHAT": "ollama/gemma4:e4b",
                "OLLAMA_CHAT_MODEL": "gemma4:e4b",
            }
        )


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
