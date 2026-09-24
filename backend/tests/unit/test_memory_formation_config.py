from __future__ import annotations

import sys

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
@pytest.mark.parametrize(
    "value",
    ["", "0", "-1", "01", "1.5", " 1", "1 ", "１２", "+1", "invalid"],
)
def test_formation_settings_reject_invalid_values_without_fallback(
    key: str,
    value: str,
) -> None:
    with pytest.raises(ValueError, match=key):
        _resolve({key: value})


def test_formation_settings_reject_leading_zero_before_int_conversion() -> None:
    raw = "0" + "9" * (sys.get_int_max_str_digits() + 1)

    with pytest.raises(ValueError) as exc_info:
        _resolve({"MEMORY_FORMATION_MAX_ATTEMPTS": raw})

    assert str(exc_info.value) == (
        "MEMORY_FORMATION_MAX_ATTEMPTS must be a positive integer"
    )


def test_formation_settings_propagate_standard_int_error_for_oversized_digits() -> (
    None
):
    raw = "9" * (sys.get_int_max_str_digits() + 1)

    with pytest.raises(ValueError) as direct_error:
        int(raw)
    with pytest.raises(ValueError) as settings_error:
        _resolve({"MEMORY_FORMATION_MAX_ATTEMPTS": raw})

    assert str(settings_error.value) == str(direct_error.value)
