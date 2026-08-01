from dataclasses import dataclass

from app.environment import positive_integer_environment_value

MAX_COMPLETED_TURNS_ENV = "CONVERSATION_HISTORY_MAX_COMPLETED_TURNS"
HISTORY_TOKEN_LIMIT_ENV = "CONVERSATION_HISTORY_TOKEN_LIMIT"
USER_INPUT_TOKEN_LIMIT_ENV = "USER_INPUT_TOKEN_LIMIT"
ASSISTANT_MAX_GENERATION_TOKENS_ENV = "ASSISTANT_MAX_GENERATION_TOKENS"
CONTEXT_TOKEN_LIMIT_ENV = "LLM_CONTEXT_TOKEN_LIMIT"

DEFAULT_MAX_COMPLETED_TURNS = 10
DEFAULT_HISTORY_TOKEN_LIMIT = 4_096
DEFAULT_USER_INPUT_TOKEN_LIMIT = 8_192
DEFAULT_ASSISTANT_MAX_GENERATION_TOKENS = 4_096
DEFAULT_CONTEXT_TOKEN_LIMIT = 32_768


@dataclass(frozen=True)
class PromptRuntimeConfig:
    max_completed_turns: int
    history_token_limit: int
    user_input_token_limit: int
    assistant_max_generation_tokens: int
    context_token_limit: int


def resolve_prompt_config() -> PromptRuntimeConfig:
    config = PromptRuntimeConfig(
        max_completed_turns=positive_integer_environment_value(
            MAX_COMPLETED_TURNS_ENV,
            DEFAULT_MAX_COMPLETED_TURNS,
        ),
        history_token_limit=positive_integer_environment_value(
            HISTORY_TOKEN_LIMIT_ENV,
            DEFAULT_HISTORY_TOKEN_LIMIT,
        ),
        user_input_token_limit=positive_integer_environment_value(
            USER_INPUT_TOKEN_LIMIT_ENV,
            DEFAULT_USER_INPUT_TOKEN_LIMIT,
        ),
        assistant_max_generation_tokens=positive_integer_environment_value(
            ASSISTANT_MAX_GENERATION_TOKENS_ENV,
            DEFAULT_ASSISTANT_MAX_GENERATION_TOKENS,
        ),
        context_token_limit=positive_integer_environment_value(
            CONTEXT_TOKEN_LIMIT_ENV,
            DEFAULT_CONTEXT_TOKEN_LIMIT,
        ),
    )
    if config.assistant_max_generation_tokens >= config.context_token_limit:
        raise ValueError(
            f"{ASSISTANT_MAX_GENERATION_TOKENS_ENV} must be less than "
            f"{CONTEXT_TOKEN_LIMIT_ENV}"
        )
    return config
