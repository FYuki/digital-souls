from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


INTERVAL_ENV = "MEMORY_CONSOLIDATION_INTERVAL_SECONDS"
IDLE_ENV = "MEMORY_CONSOLIDATION_IDLE_SECONDS"
BATCH_SIZE_ENV = "MEMORY_CONSOLIDATION_BATCH_SIZE"
MAX_RUNTIME_ENV = "MEMORY_CONSOLIDATION_MAX_RUNTIME_SECONDS"


@dataclass(frozen=True)
class MemoryConsolidationSettings:
    interval_seconds: int
    idle_seconds: int
    batch_size: int
    max_runtime_seconds: int
    llm_timeout_seconds: int
    max_output_tokens: int


def resolve_memory_consolidation_settings(
    environment: Mapping[str, str],
) -> MemoryConsolidationSettings:
    return MemoryConsolidationSettings(
        interval_seconds=_positive_integer(environment, INTERVAL_ENV, 3600),
        idle_seconds=_positive_integer(environment, IDLE_ENV, 1800),
        batch_size=_positive_integer(environment, BATCH_SIZE_ENV, 10),
        max_runtime_seconds=_positive_integer(environment, MAX_RUNTIME_ENV, 300),
        llm_timeout_seconds=15,
        max_output_tokens=512,
    )


def _positive_integer(
    environment: Mapping[str, str], key: str, default: int
) -> int:
    raw = environment.get(key)
    if raw is None:
        return default
    if not raw.isascii() or not raw.isdecimal() or str(int(raw)) != raw or int(raw) < 1:
        raise ValueError(f"{key} must be a positive integer")
    return int(raw)
