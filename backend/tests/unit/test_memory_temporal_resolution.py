from datetime import UTC, datetime

import pytest

from app.memory.formation.temporal_resolution import (
    AbsoluteDateExpression,
    DateExpressionRole,
    RelativeDateExpression,
    resolve_occurred_at,
)
from app.memory.persistence.contracts import TemporalPrecision


STATED_AT = datetime(2026, 8, 20, 3, 30, tzinfo=UTC)


def test_resolves_last_year_march_from_stated_at_in_runtime_timezone() -> None:
    result = resolve_occurred_at(
        (
            RelativeDateExpression(
                role=DateExpressionRole.PRIMARY,
                year_offset=-1,
                month=3,
            ),
        ),
        stated_at=STATED_AT,
        timezone="Asia/Tokyo",
    )

    assert result.occurred_at == datetime(2025, 2, 28, 15, 0, tzinfo=UTC)
    assert result.occurred_timezone == "Asia/Tokyo"
    assert result.occurred_precision is TemporalPrecision.MONTH


@pytest.mark.parametrize(
    ("expression", "expected_at", "expected_precision"),
    [
        (
            RelativeDateExpression(
                role=DateExpressionRole.PRIMARY,
                month_offset=-1,
            ),
            datetime(2026, 6, 30, 15, 0, tzinfo=UTC),
            TemporalPrecision.MONTH,
        ),
        (
            RelativeDateExpression(
                role=DateExpressionRole.PRIMARY,
                week_offset=-1,
                weekday=6,
            ),
            datetime(2026, 8, 15, 15, 0, tzinfo=UTC),
            TemporalPrecision.DAY,
        ),
        (
            AbsoluteDateExpression(
                role=DateExpressionRole.PRIMARY,
                year=2024,
                month=2,
                day=29,
            ),
            datetime(2024, 2, 28, 15, 0, tzinfo=UTC),
            TemporalPrecision.DAY,
        ),
    ],
)
def test_resolves_relative_and_absolute_boundaries_deterministically(
    expression: object,
    expected_at: datetime,
    expected_precision: TemporalPrecision,
) -> None:
    result = resolve_occurred_at(
        (expression,),
        stated_at=STATED_AT,
        timezone="Asia/Tokyo",
    )

    assert result.occurred_at == expected_at
    assert result.occurred_timezone == "Asia/Tokyo"
    assert result.occurred_precision is expected_precision


def test_multiple_dates_use_the_primary_expression_for_the_single_saved_occurrence() -> None:
    result = resolve_occurred_at(
        (
            AbsoluteDateExpression(
                role=DateExpressionRole.START,
                year=2025,
                month=3,
                day=1,
            ),
            AbsoluteDateExpression(
                role=DateExpressionRole.END,
                year=2025,
                month=3,
                day=31,
            ),
            AbsoluteDateExpression(
                role=DateExpressionRole.PRIMARY,
                year=2025,
                month=3,
                day=15,
            ),
        ),
        stated_at=STATED_AT,
        timezone="Asia/Tokyo",
    )

    assert result.occurred_at == datetime(2025, 3, 14, 15, 0, tzinfo=UTC)
    assert result.occurred_precision is TemporalPrecision.DAY


def test_no_date_expression_does_not_fall_back_to_stated_at() -> None:
    result = resolve_occurred_at(
        (),
        stated_at=STATED_AT,
        timezone="Asia/Tokyo",
    )

    assert result.occurred_at is None
    assert result.occurred_timezone is None
    assert result.occurred_precision is None


def test_primary_absence_returns_all_unknown_when_only_range_roles_exist() -> None:
    result = resolve_occurred_at(
        (
            AbsoluteDateExpression(
                role=DateExpressionRole.START,
                year=2025,
                month=3,
                day=1,
            ),
            AbsoluteDateExpression(
                role=DateExpressionRole.END,
                year=2025,
                month=3,
                day=31,
            ),
        ),
        stated_at=STATED_AT,
        timezone="Asia/Tokyo",
    )

    assert result.occurred_at is None
    assert result.occurred_timezone is None
    assert result.occurred_precision is None


@pytest.mark.parametrize(("second_month", "second_day"), [(5, 4), (3, 15)])
def test_multiple_primary_expressions_return_all_unknown_regardless_of_value(
    second_month: int,
    second_day: int,
) -> None:
    result = resolve_occurred_at(
        (
            AbsoluteDateExpression(
                role=DateExpressionRole.PRIMARY,
                year=2025,
                month=3,
                day=15,
            ),
            AbsoluteDateExpression(
                role=DateExpressionRole.PRIMARY,
                year=2025,
                month=second_month,
                day=second_day,
            ),
        ),
        stated_at=STATED_AT,
        timezone="Asia/Tokyo",
    )

    assert result.occurred_at is None
    assert result.occurred_timezone is None
    assert result.occurred_precision is None


def test_day_offset_uses_local_day_boundary_and_day_precision() -> None:
    result = resolve_occurred_at(
        (
            RelativeDateExpression(
                role=DateExpressionRole.PRIMARY,
                day_offset=-1,
            ),
        ),
        stated_at=STATED_AT,
        timezone="Asia/Tokyo",
    )

    assert result.occurred_at == datetime(2026, 8, 18, 15, 0, tzinfo=UTC)
    assert result.occurred_timezone == "Asia/Tokyo"
    assert result.occurred_precision is TemporalPrecision.DAY


def test_explicit_zero_day_offset_uses_local_day_precision() -> None:
    result = resolve_occurred_at(
        (
            RelativeDateExpression(
                role=DateExpressionRole.PRIMARY,
                day_offset=0,
            ),
        ),
        stated_at=STATED_AT,
        timezone="Asia/Tokyo",
    )

    assert result.occurred_at == datetime(2026, 8, 19, 15, 0, tzinfo=UTC)
    assert result.occurred_timezone == "Asia/Tokyo"
    assert result.occurred_precision is TemporalPrecision.DAY


@pytest.mark.parametrize(
    ("expression", "expected_at", "expected_precision"),
    [
        (
            RelativeDateExpression(
                role=DateExpressionRole.PRIMARY,
                month_offset=0,
            ),
            datetime(2026, 7, 31, 15, 0, tzinfo=UTC),
            TemporalPrecision.MONTH,
        ),
        (
            RelativeDateExpression(
                role=DateExpressionRole.PRIMARY,
                year_offset=0,
            ),
            datetime(2025, 12, 31, 15, 0, tzinfo=UTC),
            TemporalPrecision.YEAR,
        ),
    ],
)
def test_explicit_zero_offsets_preserve_their_declared_precision(
    expression: RelativeDateExpression,
    expected_at: datetime,
    expected_precision: TemporalPrecision,
) -> None:
    result = resolve_occurred_at(
        (expression,),
        stated_at=STATED_AT,
        timezone="Asia/Tokyo",
    )

    assert result.occurred_at == expected_at
    assert result.occurred_precision is expected_precision


def test_role_only_relative_expression_is_unresolved() -> None:
    result = resolve_occurred_at(
        (RelativeDateExpression(role=DateExpressionRole.PRIMARY),),
        stated_at=STATED_AT,
        timezone="Asia/Tokyo",
    )

    assert result.occurred_at is None
    assert result.occurred_timezone is None
    assert result.occurred_precision is None


def test_invalid_absolute_calendar_date_is_unresolved() -> None:
    result = resolve_occurred_at(
        (
            AbsoluteDateExpression(
                role=DateExpressionRole.PRIMARY,
                year=2025,
                month=2,
                day=30,
            ),
        ),
        stated_at=STATED_AT,
        timezone="Asia/Tokyo",
    )

    assert result.occurred_at is None
    assert result.occurred_timezone is None
    assert result.occurred_precision is None


def test_unknown_timezone_is_rejected() -> None:
    with pytest.raises(ValueError, match="IANA timezone"):
        resolve_occurred_at(
            (),
            stated_at=STATED_AT,
            timezone="Asia/Nowhere",
        )


@pytest.mark.parametrize(
    "expression",
    [
        AbsoluteDateExpression(
            role=DateExpressionRole.PRIMARY,
            year=2011,
            month=12,
            day=30,
        ),
        RelativeDateExpression(
            role=DateExpressionRole.PRIMARY,
            day_offset=1,
        ),
    ],
    ids=["absolute", "relative"],
)
def test_nonexistent_local_date_is_unresolved(
    expression: AbsoluteDateExpression | RelativeDateExpression,
) -> None:
    result = resolve_occurred_at(
        (expression,),
        stated_at=datetime(2011, 12, 29, 12, 0, tzinfo=UTC),
        timezone="Pacific/Apia",
    )

    assert result.occurred_at is None
    assert result.occurred_timezone is None
    assert result.occurred_precision is None


def test_fold_from_dst_transition_does_not_reject_a_normal_absolute_date() -> None:
    result = resolve_occurred_at(
        (
            AbsoluteDateExpression(
                role=DateExpressionRole.PRIMARY,
                year=2026,
                month=6,
                day=1,
            ),
        ),
        stated_at=datetime(2026, 11, 1, 9, 30, tzinfo=UTC),
        timezone="America/Los_Angeles",
    )

    assert result.occurred_at == datetime(2026, 6, 1, 7, 0, tzinfo=UTC)
    assert result.occurred_timezone == "America/Los_Angeles"
    assert result.occurred_precision is TemporalPrecision.DAY


def test_last_month_crosses_the_year_boundary_in_the_runtime_timezone() -> None:
    result = resolve_occurred_at(
        (
            RelativeDateExpression(
                role=DateExpressionRole.PRIMARY,
                month_offset=-1,
            ),
        ),
        stated_at=datetime(2026, 1, 15, 3, 0, tzinfo=UTC),
        timezone="Pacific/Chatham",
    )

    assert result.occurred_at == datetime(2025, 11, 30, 10, 15, tzinfo=UTC)
    assert result.occurred_timezone == "Pacific/Chatham"
    assert result.occurred_precision is TemporalPrecision.MONTH
