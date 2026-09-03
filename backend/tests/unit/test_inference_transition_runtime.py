from __future__ import annotations

import logging

import pytest

from app.inference.runtime import transition_inference_environment


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
