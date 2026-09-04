from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

WHISPER_MODEL_ENV = "WHISPER_MODEL"
MAX_COMPLETED_TURNS_ENV = "CONVERSATION_HISTORY_MAX_COMPLETED_TURNS"
HISTORY_TOKEN_LIMIT_ENV = "CONVERSATION_HISTORY_TOKEN_LIMIT"
USER_INPUT_TOKEN_LIMIT_ENV = "USER_INPUT_TOKEN_LIMIT"
MODEL_CONTEXT_TOKEN_LIMIT_ENV = "LLM_CONTEXT_TOKEN_LIMIT"
MODEL_ENVIRONMENT_KEYS = (
    WHISPER_MODEL_ENV,
    MAX_COMPLETED_TURNS_ENV,
    HISTORY_TOKEN_LIMIT_ENV,
    USER_INPUT_TOKEN_LIMIT_ENV,
    MODEL_CONTEXT_TOKEN_LIMIT_ENV,
)

WHISPER_MODEL_NAME = "medium"
DEFAULT_CHAT_CONTEXT_TOKENS = 8_192
DEFAULT_RESPONSE_RESERVE_TOKENS = 1_024
DEFAULT_MAX_COMPLETED_TURNS = 10
DEFAULT_HISTORY_TOKEN_LIMIT = 4_096
DEFAULT_USER_INPUT_TOKEN_LIMIT = 8_192
DEFAULT_MODEL_CONTEXT_TOKEN_LIMIT = 32_768


@dataclass(frozen=True)
class ModelSettings:
    whisper_model: str
    chat_context_tokens: int
    assistant_max_generation_tokens: int
    max_completed_turns: int
    history_token_limit: int
    user_input_token_limit: int
    model_context_token_limit: int


def resolve_model_settings(
    environment: Mapping[str, str],
    *,
    chat_context_tokens: int = DEFAULT_CHAT_CONTEXT_TOKENS,
    assistant_max_generation_tokens: int = DEFAULT_RESPONSE_RESERVE_TOKENS,
) -> ModelSettings:
    if type(chat_context_tokens) is not int or chat_context_tokens < 1:
        raise ValueError("chat context token limit must be a positive integer")
    if (
        type(assistant_max_generation_tokens) is not int
        or assistant_max_generation_tokens < 1
    ):
        raise ValueError("chat output token limit must be a positive integer")
    settings = ModelSettings(
        whisper_model=_string_value(environment, WHISPER_MODEL_ENV, WHISPER_MODEL_NAME),
        chat_context_tokens=chat_context_tokens,
        assistant_max_generation_tokens=assistant_max_generation_tokens,
        max_completed_turns=_positive_integer(
            environment, MAX_COMPLETED_TURNS_ENV, DEFAULT_MAX_COMPLETED_TURNS
        ),
        history_token_limit=_positive_integer(
            environment, HISTORY_TOKEN_LIMIT_ENV, DEFAULT_HISTORY_TOKEN_LIMIT
        ),
        user_input_token_limit=_positive_integer(
            environment, USER_INPUT_TOKEN_LIMIT_ENV, DEFAULT_USER_INPUT_TOKEN_LIMIT
        ),
        model_context_token_limit=_positive_integer(
            environment,
            MODEL_CONTEXT_TOKEN_LIMIT_ENV,
            DEFAULT_MODEL_CONTEXT_TOKEN_LIMIT,
        ),
    )
    _validate_token_relationships(settings)
    return settings


def model_settings_environment(settings: ModelSettings) -> dict[str, str]:
    return {
        WHISPER_MODEL_ENV: settings.whisper_model,
        MAX_COMPLETED_TURNS_ENV: str(settings.max_completed_turns),
        HISTORY_TOKEN_LIMIT_ENV: str(settings.history_token_limit),
        USER_INPUT_TOKEN_LIMIT_ENV: str(settings.user_input_token_limit),
        MODEL_CONTEXT_TOKEN_LIMIT_ENV: str(settings.model_context_token_limit),
    }


def _string_value(
    environment: Mapping[str, str], key: str, default: str
) -> str:
    value = environment.get(key, default)
    if not value or value.strip() != value:
        raise ValueError(f"{key} must be a non-empty canonical string")
    return value


def _positive_integer(
    environment: Mapping[str, str], key: str, default: int
) -> int:
    raw_value = environment.get(key)
    if raw_value is None:
        return default
    if not raw_value.isascii() or not raw_value.isdecimal():
        raise ValueError(f"{key} must be a positive integer")
    value = int(raw_value)
    if value < 1 or str(value) != raw_value:
        raise ValueError(f"{key} must be a positive integer")
    return value


def _validate_token_relationships(settings: ModelSettings) -> None:
    if settings.assistant_max_generation_tokens >= settings.chat_context_tokens:
        raise ValueError(
            "chat output token limit must be less than chat context token limit"
        )
    if settings.chat_context_tokens > settings.model_context_token_limit:
        raise ValueError(
            "chat context token limit must not exceed "
            f"{MODEL_CONTEXT_TOKEN_LIMIT_ENV}"
        )
