import importlib
from collections.abc import Mapping
from pathlib import Path

import pytest


MODEL_ENV_KEYS = (
    "OLLAMA_CHAT_MODEL",
    "OLLAMA_CLASSIFIER_MODEL",
    "OLLAMA_EXTRACTOR_MODEL",
    "WHISPER_MODEL",
    "OLLAMA_CONTEXT_TOKENS",
    "OLLAMA_RESPONSE_RESERVE_TOKENS",
    "CONVERSATION_HISTORY_MAX_COMPLETED_TURNS",
    "CONVERSATION_HISTORY_TOKEN_LIMIT",
    "USER_INPUT_TOKEN_LIMIT",
    "ASSISTANT_MAX_GENERATION_TOKENS",
    "LLM_CONTEXT_TOKEN_LIMIT",
)
NUMERIC_ENV_KEYS = MODEL_ENV_KEYS[4:]
BACKEND_DIR = Path(__file__).parents[2]


def _resolve(environment: Mapping[str, str]):
    module = importlib.import_module("app.model_settings")
    resolver = getattr(module, "resolve_model_settings", None)
    assert callable(resolver), "model settings resolver must be exposed"
    return resolver(environment)


def test_should_resolve_all_documented_model_defaults() -> None:
    settings = _resolve({})

    assert settings.ollama_chat_model == "gemma4:e4b"
    assert settings.ollama_classifier_model == "gemma4:e4b"
    assert settings.ollama_extractor_model == "gemma4:e4b"
    assert settings.whisper_model == "medium"
    assert settings.ollama_context_tokens == 8192
    assert settings.assistant_max_generation_tokens == 1024
    assert settings.max_completed_turns == 10
    assert settings.history_token_limit == 4096
    assert settings.user_input_token_limit == 8192
    assert settings.model_context_token_limit == 32768


def test_env_example_should_match_executable_setting_defaults() -> None:
    lines = (BACKEND_DIR / ".env.example").read_text(encoding="utf-8").splitlines()

    assert {
        "OLLAMA_CHAT_MODEL=gemma4:e4b",
        "OLLAMA_CLASSIFIER_MODEL=gemma4:e4b",
        "OLLAMA_EXTRACTOR_MODEL=gemma4:e4b",
        "WHISPER_MODEL=medium",
        "OLLAMA_CONTEXT_TOKENS=8192",
        "OLLAMA_RESPONSE_RESERVE_TOKENS=1024",
        "CONVERSATION_HISTORY_MAX_COMPLETED_TURNS=10",
        "CONVERSATION_HISTORY_TOKEN_LIMIT=4096",
        "USER_INPUT_TOKEN_LIMIT=8192",
        "ASSISTANT_MAX_GENERATION_TOKENS=1024",
        "LLM_CONTEXT_TOKEN_LIMIT=32768",
    }.issubset(lines)


def test_should_resolve_every_environment_override_from_one_mapping() -> None:
    settings = _resolve(
        {
            "OLLAMA_CHAT_MODEL": "custom-chat:9b",
            "OLLAMA_CLASSIFIER_MODEL": "custom-classifier:4b",
            "OLLAMA_EXTRACTOR_MODEL": "custom-extractor:4b",
            "WHISPER_MODEL": "large-v3",
            "OLLAMA_CONTEXT_TOKENS": "12000",
            "OLLAMA_RESPONSE_RESERVE_TOKENS": "1500",
            "CONVERSATION_HISTORY_MAX_COMPLETED_TURNS": "4",
            "CONVERSATION_HISTORY_TOKEN_LIMIT": "2400",
            "USER_INPUT_TOKEN_LIMIT": "900",
            "LLM_CONTEXT_TOKEN_LIMIT": "16000",
        }
    )

    assert settings.ollama_chat_model == "custom-chat:9b"
    assert settings.ollama_classifier_model == "custom-classifier:4b"
    assert settings.ollama_extractor_model == "custom-extractor:4b"
    assert settings.whisper_model == "large-v3"
    assert settings.ollama_context_tokens == 12000
    assert settings.assistant_max_generation_tokens == 1500
    assert settings.max_completed_turns == 4
    assert settings.history_token_limit == 2400
    assert settings.user_input_token_limit == 900
    assert settings.model_context_token_limit == 16000


@pytest.mark.parametrize("key", MODEL_ENV_KEYS[:4])
@pytest.mark.parametrize("value", ["", " ", " medium", "medium "])
def test_should_reject_empty_or_noncanonical_string_settings(
    key: str,
    value: str,
) -> None:
    with pytest.raises(ValueError, match=key):
        _resolve({key: value})


@pytest.mark.parametrize("key", NUMERIC_ENV_KEYS)
@pytest.mark.parametrize("value", ["", "0", "-1", "1.5", " 1", "+1", "invalid"])
def test_should_reject_invalid_positive_integer_without_fallback(
    key: str,
    value: str,
) -> None:
    with pytest.raises(ValueError, match=key):
        _resolve({key: value})


def test_should_accept_equal_alias_values_as_one_generation_reservation() -> None:
    settings = _resolve(
        {
            "OLLAMA_RESPONSE_RESERVE_TOKENS": "768",
            "ASSISTANT_MAX_GENERATION_TOKENS": "768",
        }
    )

    assert settings.assistant_max_generation_tokens == 768


def test_should_reject_conflicting_generation_reservation_aliases() -> None:
    environment = {
        "OLLAMA_RESPONSE_RESERVE_TOKENS": "768",
        "ASSISTANT_MAX_GENERATION_TOKENS": "769",
    }

    with pytest.raises(ValueError) as exc_info:
        _resolve(environment)

    message = str(exc_info.value)
    assert "OLLAMA_RESPONSE_RESERVE_TOKENS" in message
    assert "ASSISTANT_MAX_GENERATION_TOKENS" in message
    assert "match" in message.lower()


@pytest.mark.parametrize(
    "environment",
    [
        {
            "OLLAMA_CONTEXT_TOKENS": "1024",
            "OLLAMA_RESPONSE_RESERVE_TOKENS": "1024",
        },
        {
            "OLLAMA_CONTEXT_TOKENS": "1024",
            "OLLAMA_RESPONSE_RESERVE_TOKENS": "1025",
        },
    ],
)
def test_should_reject_response_reservation_at_or_above_runtime_context(
    environment: dict[str, str],
) -> None:
    with pytest.raises(ValueError) as exc_info:
        _resolve(environment)

    message = str(exc_info.value)
    assert "OLLAMA_RESPONSE_RESERVE_TOKENS" in message
    assert "OLLAMA_CONTEXT_TOKENS" in message
    assert "less than" in message.lower()


def test_should_reject_runtime_context_above_model_context() -> None:
    environment = {
        "OLLAMA_CONTEXT_TOKENS": "32769",
        "LLM_CONTEXT_TOKEN_LIMIT": "32768",
    }

    with pytest.raises(ValueError) as exc_info:
        _resolve(environment)

    message = str(exc_info.value)
    assert "OLLAMA_CONTEXT_TOKENS" in message
    assert "LLM_CONTEXT_TOKEN_LIMIT" in message


def test_should_resolve_fresh_values_after_module_import() -> None:
    first = _resolve({"OLLAMA_CHAT_MODEL": "first:1b"})
    second = _resolve({"OLLAMA_CHAT_MODEL": "second:2b"})

    assert first.ollama_chat_model == "first:1b"
    assert second.ollama_chat_model == "second:2b"
    assert first is not second
