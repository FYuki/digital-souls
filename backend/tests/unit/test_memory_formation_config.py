from __future__ import annotations

import pytest


def _resolve(environment: dict[str, str]):
    from app.memory.formation.config import resolve_memory_formation_settings

    return resolve_memory_formation_settings(environment)


def test_formation_settings_resolve_required_defaults() -> None:
    settings = _resolve({})

    assert settings.llm_timeout_seconds == 15
    assert settings.max_attempts == 2
    assert settings.total_timeout_seconds == 35
    assert settings.max_queue_age_seconds == 300
    assert settings.queue_maxsize == 100
    assert settings.max_output_tokens > 0


def test_formation_settings_resolve_every_override() -> None:
    settings = _resolve(
        {
            "MEMORY_FORMATION_LLM_TIMEOUT_SECONDS": "7",
            "MEMORY_FORMATION_MAX_ATTEMPTS": "1",
            "MEMORY_FORMATION_TOTAL_TIMEOUT_SECONDS": "12",
            "MEMORY_FORMATION_MAX_QUEUE_AGE_SECONDS": "45",
            "MEMORY_FORMATION_QUEUE_MAXSIZE": "25",
            "MEMORY_FORMATION_MAX_OUTPUT_TOKENS": "256",
        }
    )

    assert settings.llm_timeout_seconds == 7
    assert settings.max_attempts == 1
    assert settings.total_timeout_seconds == 12
    assert settings.max_queue_age_seconds == 45
    assert settings.queue_maxsize == 25
    assert settings.max_output_tokens == 256


@pytest.mark.parametrize(
    "key",
    [
        "MEMORY_FORMATION_LLM_TIMEOUT_SECONDS",
        "MEMORY_FORMATION_MAX_ATTEMPTS",
        "MEMORY_FORMATION_TOTAL_TIMEOUT_SECONDS",
        "MEMORY_FORMATION_MAX_QUEUE_AGE_SECONDS",
        "MEMORY_FORMATION_QUEUE_MAXSIZE",
        "MEMORY_FORMATION_MAX_OUTPUT_TOKENS",
    ],
)
@pytest.mark.parametrize("value", ["", "0", "-1", "1.5", " 1", "invalid"])
def test_formation_settings_reject_invalid_values_without_fallback(
    key: str,
    value: str,
) -> None:
    with pytest.raises(ValueError, match=key):
        _resolve({key: value})
