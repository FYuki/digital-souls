from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

OLLAMA_CHAT_MODEL_ENV = "OLLAMA_CHAT_MODEL"
OLLAMA_CLASSIFIER_MODEL_ENV = "OLLAMA_CLASSIFIER_MODEL"
OLLAMA_EXTRACTOR_MODEL_ENV = "OLLAMA_EXTRACTOR_MODEL"
WHISPER_MODEL_ENV = "WHISPER_MODEL"
OLLAMA_CONTEXT_TOKENS_ENV = "OLLAMA_CONTEXT_TOKENS"
OLLAMA_RESPONSE_RESERVE_TOKENS_ENV = "OLLAMA_RESPONSE_RESERVE_TOKENS"
MAX_COMPLETED_TURNS_ENV = "CONVERSATION_HISTORY_MAX_COMPLETED_TURNS"
HISTORY_TOKEN_LIMIT_ENV = "CONVERSATION_HISTORY_TOKEN_LIMIT"
USER_INPUT_TOKEN_LIMIT_ENV = "USER_INPUT_TOKEN_LIMIT"
ASSISTANT_MAX_GENERATION_TOKENS_ENV = "ASSISTANT_MAX_GENERATION_TOKENS"
MODEL_CONTEXT_TOKEN_LIMIT_ENV = "LLM_CONTEXT_TOKEN_LIMIT"
MODEL_ENVIRONMENT_KEYS = (
    OLLAMA_CHAT_MODEL_ENV,
    OLLAMA_CLASSIFIER_MODEL_ENV,
    OLLAMA_EXTRACTOR_MODEL_ENV,
    WHISPER_MODEL_ENV,
    OLLAMA_CONTEXT_TOKENS_ENV,
    OLLAMA_RESPONSE_RESERVE_TOKENS_ENV,
    ASSISTANT_MAX_GENERATION_TOKENS_ENV,
    MAX_COMPLETED_TURNS_ENV,
    HISTORY_TOKEN_LIMIT_ENV,
    USER_INPUT_TOKEN_LIMIT_ENV,
    MODEL_CONTEXT_TOKEN_LIMIT_ENV,
)

OLLAMA_MODEL_NAME = "gemma4:e4b"
OLLAMA_CLASSIFIER_MODEL_NAME = "gemma4:e4b"
OLLAMA_EXTRACTOR_MODEL_NAME = "gemma4:e4b"
WHISPER_MODEL_NAME = "medium"
DEFAULT_OLLAMA_CONTEXT_TOKENS = 8_192
DEFAULT_RESPONSE_RESERVE_TOKENS = 1_024
DEFAULT_MAX_COMPLETED_TURNS = 10
DEFAULT_HISTORY_TOKEN_LIMIT = 4_096
DEFAULT_USER_INPUT_TOKEN_LIMIT = 8_192
DEFAULT_MODEL_CONTEXT_TOKEN_LIMIT = 32_768


@dataclass(frozen=True)
class ModelSettings:
    ollama_chat_model: str
    ollama_classifier_model: str
    ollama_extractor_model: str
    whisper_model: str
    ollama_context_tokens: int
    assistant_max_generation_tokens: int
    max_completed_turns: int
    history_token_limit: int
    user_input_token_limit: int
    model_context_token_limit: int


def resolve_model_settings(environment: Mapping[str, str]) -> ModelSettings:
    settings = ModelSettings(
        ollama_chat_model=_string_value(
            environment, OLLAMA_CHAT_MODEL_ENV, OLLAMA_MODEL_NAME
        ),
        ollama_classifier_model=_string_value(
            environment,
            OLLAMA_CLASSIFIER_MODEL_ENV,
            OLLAMA_CLASSIFIER_MODEL_NAME,
        ),
        ollama_extractor_model=_string_value(
            environment,
            OLLAMA_EXTRACTOR_MODEL_ENV,
            OLLAMA_EXTRACTOR_MODEL_NAME,
        ),
        whisper_model=_string_value(environment, WHISPER_MODEL_ENV, WHISPER_MODEL_NAME),
        ollama_context_tokens=_positive_integer(
            environment, OLLAMA_CONTEXT_TOKENS_ENV, DEFAULT_OLLAMA_CONTEXT_TOKENS
        ),
        assistant_max_generation_tokens=_generation_reservation(environment),
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
    reservation = str(settings.assistant_max_generation_tokens)
    return {
        OLLAMA_CHAT_MODEL_ENV: settings.ollama_chat_model,
        OLLAMA_CLASSIFIER_MODEL_ENV: settings.ollama_classifier_model,
        OLLAMA_EXTRACTOR_MODEL_ENV: settings.ollama_extractor_model,
        WHISPER_MODEL_ENV: settings.whisper_model,
        OLLAMA_CONTEXT_TOKENS_ENV: str(settings.ollama_context_tokens),
        OLLAMA_RESPONSE_RESERVE_TOKENS_ENV: reservation,
        ASSISTANT_MAX_GENERATION_TOKENS_ENV: reservation,
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


def _generation_reservation(environment: Mapping[str, str]) -> int:
    response_value = environment.get(OLLAMA_RESPONSE_RESERVE_TOKENS_ENV)
    assistant_value = environment.get(ASSISTANT_MAX_GENERATION_TOKENS_ENV)
    if (
        response_value is not None
        and assistant_value is not None
        and response_value != assistant_value
    ):
        raise ValueError(
            f"{OLLAMA_RESPONSE_RESERVE_TOKENS_ENV} and "
            f"{ASSISTANT_MAX_GENERATION_TOKENS_ENV} must match"
        )
    key = (
        OLLAMA_RESPONSE_RESERVE_TOKENS_ENV
        if response_value is not None
        else ASSISTANT_MAX_GENERATION_TOKENS_ENV
    )
    return _positive_integer(environment, key, DEFAULT_RESPONSE_RESERVE_TOKENS)


def _validate_token_relationships(settings: ModelSettings) -> None:
    if settings.assistant_max_generation_tokens >= settings.ollama_context_tokens:
        raise ValueError(
            f"{OLLAMA_RESPONSE_RESERVE_TOKENS_ENV} and "
            f"{ASSISTANT_MAX_GENERATION_TOKENS_ENV} must be less than "
            f"{OLLAMA_CONTEXT_TOKENS_ENV}"
        )
    if settings.ollama_context_tokens > settings.model_context_token_limit:
        raise ValueError(
            f"{OLLAMA_CONTEXT_TOKENS_ENV} must not exceed "
            f"{MODEL_CONTEXT_TOKEN_LIMIT_ENV}"
        )
