"""Finite trusted settings and content-free diagnostics."""

import pytest
from pydantic import ValidationError

from digital_souls_core.application import CoreError
from digital_souls_core.formation import FormationConfig, FormationInput, FormationSource, job_limit
from digital_souls_core.history import SourceReference, SourceState

from .test_postgres_stores import BINDING, MESSAGES

pytestmark = pytest.mark.ut


@pytest.mark.parametrize(
    "values",
    [
        {"max_jobs": 0},
        {"max_jobs": 101},
        {"max_jobs": True},
        {"max_jobs": "10"},
        {"max_jobs": 1.0},
        {"lease_seconds": 0},
        {"lease_seconds": 3601},
        {"processor_timeout_seconds": 0},
        {"processor_timeout_seconds": 301},
        {"max_attempts": 0},
        {"max_attempts": 101},
        {"retry_backoff_seconds": -1},
        {"retry_backoff_seconds": 3601},
        {"retry_backoff_seconds": False},
        {"unknown": 1},
    ],
)
def test_invalid_trusted_limits_rejected(values: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        FormationConfig.model_validate(values)


@pytest.mark.parametrize("limit", [0, -1, 11, True, "1", 1.0])
def test_python_job_limit_does_not_coerce_or_increase_operator_budget(limit: object) -> None:
    with pytest.raises(CoreError) as error:
        job_limit(FormationConfig(), limit)  # type: ignore[arg-type]
    assert error.value.code == "formation_limit"


def test_input_diagnostics_do_not_contain_synthetic_history() -> None:
    source = FormationSource(
        SourceState(SourceReference("synthetic", 1, 0), True, None), MESSAGES[0]
    )
    job = FormationInput(BINDING, "synthetic", 1, 0, (source,))
    assert "I like synthetic tea" not in repr(source)
    assert "I like synthetic tea" not in repr(job)
