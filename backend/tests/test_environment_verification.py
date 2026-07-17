from __future__ import annotations

import pytest


def test_should_record_observation_before_rejecting_external_service() -> None:
    from environment_verification import (
        EnvironmentVerificationError,
        record_and_validate_verification,
    )

    observation = {
        "url": "http://frontend.example:5173/",
        "attempts": 1,
        "elapsedSeconds": 0.1,
        "result": "not_ready",
    }
    recorded: list[tuple[str, object]] = []

    with pytest.raises(EnvironmentVerificationError) as error:
        record_and_validate_verification(
            {
                "frontend": {
                    "classification": "readiness",
                    "checks": [],
                    "readiness": observation,
                }
            },
            lambda service, readiness: recorded.append((service, readiness)),
        )

    assert error.value.category == "readiness"
    assert recorded == [("frontend", observation)]


def test_should_distinguish_unpreparable_verification_failure() -> None:
    from environment_verification import (
        EnvironmentVerificationError,
        record_and_validate_verification,
    )

    with pytest.raises(EnvironmentVerificationError) as error:
        record_and_validate_verification(
            {
                "ollama": {
                    "classification": "preparation_required",
                    "checks": [
                        {
                            "classification": "preparation_required",
                            "canPrepare": False,
                        }
                    ],
                }
            },
            lambda service, readiness: None,
        )

    assert error.value.category == "preparation"


@pytest.mark.parametrize(
    ("classification", "category"),
    [("preparation", "preparation"), ("readiness", "readiness")],
)
def test_should_map_service_readiness_failure_category(
    classification: str, category: str
) -> None:
    from adapters.base import ReadinessValidationResult
    from environment_verification import (
        EnvironmentVerificationError,
        require_service_readiness,
    )

    validation = ReadinessValidationResult(classification, "dependency unavailable")

    with pytest.raises(EnvironmentVerificationError) as error:
        require_service_readiness(validation)

    assert error.value.category == category
