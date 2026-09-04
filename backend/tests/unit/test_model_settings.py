from pathlib import Path

import pytest

from app.model_settings import model_settings_environment, resolve_model_settings


BACKEND_DIR = Path(__file__).parents[2]


def test_should_resolve_provider_independent_prompt_defaults() -> None:
    settings = resolve_model_settings({})

    assert settings.whisper_model == "medium"
    assert settings.chat_context_tokens == 8192
    assert settings.assistant_max_generation_tokens == 1024
    assert settings.max_completed_turns == 10
    assert settings.history_token_limit == 4096
    assert settings.user_input_token_limit == 8192
    assert settings.model_context_token_limit == 32768


def test_env_example_uses_target_settings_as_inference_source_of_truth() -> None:
    lines = (BACKEND_DIR / ".env.example").read_text(encoding="utf-8").splitlines()

    assert "INFERENCE_TARGET_CHAT=ollama/gemma4:e4b" in lines
    assert "INFERENCE_TARGET_CHAT_MAX_INPUT_TOKENS=7168" in lines
    assert "INFERENCE_TARGET_CHAT_MAX_OUTPUT_TOKENS=1024" in lines


def test_should_resolve_environment_and_resolved_target_values() -> None:
    settings = resolve_model_settings(
        {
            "WHISPER_MODEL": "large-v3",
            "CONVERSATION_HISTORY_MAX_COMPLETED_TURNS": "4",
            "CONVERSATION_HISTORY_TOKEN_LIMIT": "2400",
            "USER_INPUT_TOKEN_LIMIT": "900",
            "LLM_CONTEXT_TOKEN_LIMIT": "16000",
        },
        chat_context_tokens=12000,
        assistant_max_generation_tokens=1500,
    )

    assert settings.whisper_model == "large-v3"
    assert settings.chat_context_tokens == 12000
    assert settings.assistant_max_generation_tokens == 1500
    assert settings.max_completed_turns == 4
    assert settings.history_token_limit == 2400
    assert settings.user_input_token_limit == 900


@pytest.mark.parametrize(
    "key",
    [
        "CONVERSATION_HISTORY_MAX_COMPLETED_TURNS",
        "CONVERSATION_HISTORY_TOKEN_LIMIT",
        "USER_INPUT_TOKEN_LIMIT",
        "LLM_CONTEXT_TOKEN_LIMIT",
    ],
)
@pytest.mark.parametrize("value", ["", "0", "-1", "1.5", " 1", "+1", "invalid"])
def test_should_reject_invalid_positive_integer(key: str, value: str) -> None:
    with pytest.raises(ValueError, match=key):
        resolve_model_settings({key: value})


def test_should_reject_output_at_or_above_target_context() -> None:
    with pytest.raises(ValueError, match="output token limit"):
        resolve_model_settings(
            {}, chat_context_tokens=1024, assistant_max_generation_tokens=1024
        )


def test_should_reject_target_context_above_model_context() -> None:
    with pytest.raises(ValueError, match="LLM_CONTEXT_TOKEN_LIMIT"):
        resolve_model_settings(
            {"LLM_CONTEXT_TOKEN_LIMIT": "32768"},
            chat_context_tokens=32769,
        )


def test_export_does_not_reintroduce_provider_specific_settings() -> None:
    exported = model_settings_environment(resolve_model_settings({}))

    assert exported == {
        "WHISPER_MODEL": "medium",
        "CONVERSATION_HISTORY_MAX_COMPLETED_TURNS": "10",
        "CONVERSATION_HISTORY_TOKEN_LIMIT": "4096",
        "USER_INPUT_TOKEN_LIMIT": "8192",
        "LLM_CONTEXT_TOKEN_LIMIT": "32768",
    }
