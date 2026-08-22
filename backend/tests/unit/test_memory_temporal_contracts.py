from dataclasses import fields, replace
from datetime import UTC, datetime

import pytest

from app.memory.persistence.contracts import (
    FormationMethod,
    MemorySourceInput,
    MemorySourceType,
    MemoryWriteContext,
    TemporalPrecision,
)


STATED_AT = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)


def _assert_v2_context_contract_present() -> None:
    assert {field.name for field in fields(MemoryWriteContext)} >= {
        "occurred_at",
        "occurred_timezone",
        "occurred_precision",
        "stated_at",
    }


def _context() -> MemoryWriteContext:
    return MemoryWriteContext(
        formation_method=FormationMethod.EXTRACTED,
        idempotency_key="conversation-1:turn-1:0:extractor-v1",
        occurred_at=datetime(2025, 3, 1, tzinfo=UTC),
        occurred_timezone="Asia/Tokyo",
        occurred_precision=TemporalPrecision.MONTH,
        stated_at=STATED_AT,
        expires_at=None,
        policy_version="policy-v1",
        classifier_version="classifier-v1",
        model_id="gemma4:e4b",
        model_digest="model-digest",
        prompt_version="prompt-v1",
        sources=(
            MemorySourceInput(
                source_type=MemorySourceType.CONVERSATION_TURN,
                source_provider_id="core",
                source_ref="conversation-1:turn-1",
            ),
        ),
    )


def test_write_context_accepts_all_known_or_all_unknown_occurred_date_fields() -> None:
    known = _context()
    unknown = replace(
        known,
        occurred_at=None,
        occurred_timezone=None,
        occurred_precision=None,
    )

    assert known.occurred_precision is TemporalPrecision.MONTH
    assert unknown.occurred_at is None
    assert unknown.occurred_timezone is None
    assert unknown.occurred_precision is None
    assert unknown.stated_at == STATED_AT


@pytest.mark.parametrize(
    ("occurred_at", "occurred_timezone", "occurred_precision"),
    [
        (None, "Asia/Tokyo", TemporalPrecision.DAY),
        (datetime(2025, 3, 1, tzinfo=UTC), None, TemporalPrecision.DAY),
        (datetime(2025, 3, 1, tzinfo=UTC), "Asia/Tokyo", None),
    ],
)
def test_write_context_rejects_partially_known_occurred_date_fields(
    occurred_at: datetime | None,
    occurred_timezone: str | None,
    occurred_precision: TemporalPrecision | None,
) -> None:
    _assert_v2_context_contract_present()
    with pytest.raises((TypeError, ValueError)):
        replace(
            _context(),
            occurred_at=occurred_at,
            occurred_timezone=occurred_timezone,
            occurred_precision=occurred_precision,
        )


def test_write_context_always_requires_timezone_aware_stated_at() -> None:
    _assert_v2_context_contract_present()
    with pytest.raises(ValueError, match="stated_at"):
        replace(_context(), stated_at=datetime(2026, 8, 20, 12, 0))


def test_temporal_precision_remains_the_existing_six_value_contract() -> None:
    assert {precision.value for precision in TemporalPrecision} == {
        "YEAR",
        "MONTH",
        "DAY",
        "HOUR",
        "MINUTE",
        "SECOND",
    }
