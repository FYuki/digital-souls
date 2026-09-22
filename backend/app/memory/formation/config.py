from collections.abc import Mapping
from dataclasses import dataclass

from app.environment import positive_integer_setting

LLM_TIMEOUT_ENV = "MEMORY_FORMATION_LLM_TIMEOUT_SECONDS"
MAX_ATTEMPTS_ENV = "MEMORY_FORMATION_MAX_ATTEMPTS"
TOTAL_TIMEOUT_ENV = "MEMORY_FORMATION_TOTAL_TIMEOUT_SECONDS"
MAX_QUEUE_AGE_ENV = "MEMORY_FORMATION_MAX_QUEUE_AGE_SECONDS"
QUEUE_MAXSIZE_ENV = "MEMORY_FORMATION_QUEUE_MAXSIZE"
MAX_OUTPUT_TOKENS_ENV = "MEMORY_FORMATION_MAX_OUTPUT_TOKENS"


@dataclass(frozen=True)
class MemoryFormationSettings:
    llm_timeout_seconds: int
    max_attempts: int
    total_timeout_seconds: int
    max_queue_age_seconds: int
    queue_maxsize: int
    max_output_tokens: int


def resolve_memory_formation_settings(
    environment: Mapping[str, str],
) -> MemoryFormationSettings:
    return MemoryFormationSettings(
        llm_timeout_seconds=positive_integer_setting(environment, LLM_TIMEOUT_ENV, 15),
        max_attempts=positive_integer_setting(environment, MAX_ATTEMPTS_ENV, 2),
        total_timeout_seconds=positive_integer_setting(environment, TOTAL_TIMEOUT_ENV, 35),
        max_queue_age_seconds=positive_integer_setting(environment, MAX_QUEUE_AGE_ENV, 300),
        queue_maxsize=positive_integer_setting(environment, QUEUE_MAXSIZE_ENV, 100),
        max_output_tokens=positive_integer_setting(environment, MAX_OUTPUT_TOKENS_ENV, 512),
    )

