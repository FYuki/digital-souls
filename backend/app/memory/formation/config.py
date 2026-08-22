from collections.abc import Mapping
from dataclasses import dataclass

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
        llm_timeout_seconds=_positive_integer(environment, LLM_TIMEOUT_ENV, 15),
        max_attempts=_positive_integer(environment, MAX_ATTEMPTS_ENV, 2),
        total_timeout_seconds=_positive_integer(environment, TOTAL_TIMEOUT_ENV, 35),
        max_queue_age_seconds=_positive_integer(environment, MAX_QUEUE_AGE_ENV, 300),
        queue_maxsize=_positive_integer(environment, QUEUE_MAXSIZE_ENV, 100),
        max_output_tokens=_positive_integer(environment, MAX_OUTPUT_TOKENS_ENV, 512),
    )


def _positive_integer(
    environment: Mapping[str, str], key: str, default: int
) -> int:
    raw = environment.get(key)
    if raw is None:
        return default
    if not raw.isascii() or not raw.isdecimal() or raw.startswith("0"):
        raise ValueError(f"{key} must be a positive integer")
    value = int(raw)
    if value < 1:
        raise ValueError(f"{key} must be a positive integer")
    return value
