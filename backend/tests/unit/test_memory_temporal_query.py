from datetime import UTC, datetime

import pytest

from app.memory.persistence.contracts import TemporalPrecision
from app.memory.temporal_query import (
    Season,
    SeasonMatchReasonCode,
    TemporalQueryKind,
    match_season,
    parse_temporal_query,
    season_for_month,
)


NOW = datetime(2026, 8, 20, 3, 30, tzinfo=UTC)


@pytest.mark.parametrize(
    ("text", "kind", "start", "end"),
    [
        (
            "前年同月の旅行を教えて",
            TemporalQueryKind.MONTH,
            datetime(2025, 7, 31, 15, 0, tzinfo=UTC),
            datetime(2025, 8, 31, 15, 0, tzinfo=UTC),
        ),
        (
            "先月の出来事は？",
            TemporalQueryKind.MONTH,
            datetime(2026, 6, 30, 15, 0, tzinfo=UTC),
            datetime(2026, 7, 31, 15, 0, tzinfo=UTC),
        ),
        (
            "2025年3月の旅行",
            TemporalQueryKind.MONTH,
            datetime(2025, 2, 28, 15, 0, tzinfo=UTC),
            datetime(2025, 3, 31, 15, 0, tzinfo=UTC),
        ),
        (
            "2025-03-01から2025-03-31までの旅行",
            TemporalQueryKind.RANGE,
            datetime(2025, 2, 28, 15, 0, tzinfo=UTC),
            datetime(2025, 3, 31, 15, 0, tzinfo=UTC),
        ),
        (
            "去年の冬の旅行",
            TemporalQueryKind.SEASON,
            datetime(2025, 11, 30, 15, 0, tzinfo=UTC),
            datetime(2026, 2, 28, 15, 0, tzinfo=UTC),
        ),
        (
            "今年の夏の旅行",
            TemporalQueryKind.SEASON,
            datetime(2026, 5, 31, 15, 0, tzinfo=UTC),
            datetime(2026, 8, 31, 15, 0, tzinfo=UTC),
        ),
        (
            "前年同季節の旅行",
            TemporalQueryKind.SEASON,
            datetime(2025, 5, 31, 15, 0, tzinfo=UTC),
            datetime(2025, 8, 31, 15, 0, tzinfo=UTC),
        ),
    ],
)
def test_parses_supported_japanese_temporal_queries_into_utc_half_open_ranges(
    text: str,
    kind: TemporalQueryKind,
    start: datetime,
    end: datetime,
) -> None:
    result = parse_temporal_query(text, now=NOW, timezone="Asia/Tokyo")

    assert result is not None
    assert result.kind is kind
    assert result.start == start
    assert result.end == end


@pytest.mark.parametrize(
    ("month", "season"),
    [
        (2, Season.WINTER),
        (3, Season.SPRING),
        (5, Season.SPRING),
        (6, Season.SUMMER),
        (8, Season.SUMMER),
        (9, Season.AUTUMN),
        (11, Season.AUTUMN),
        (12, Season.WINTER),
    ],
)
def test_season_is_derived_from_the_occurred_month(month: int, season: Season) -> None:
    assert season_for_month(month) is season


@pytest.mark.parametrize(
    ("precision", "reason_code"),
    [
        (TemporalPrecision.YEAR, SeasonMatchReasonCode.PRECISION_TOO_COARSE),
        (None, SeasonMatchReasonCode.PRECISION_UNKNOWN),
    ],
)
def test_season_match_excludes_year_or_unknown_precision_with_reason_code(
    precision: TemporalPrecision | None,
    reason_code: SeasonMatchReasonCode,
) -> None:
    query = parse_temporal_query("去年の冬", now=NOW, timezone="Asia/Tokyo")
    assert query is not None

    result = match_season(
        query,
        occurred_at=datetime(2025, 12, 15, tzinfo=UTC),
        occurred_precision=precision,
        occurred_timezone="Asia/Tokyo",
    )

    assert result.matched is False
    assert result.reason_code is reason_code


@pytest.mark.parametrize(
    "text",
    [
        "いつか行った旅行",
        "2025-02-30から2025-03-01",
        "2025-03-31から2025-03-01",
    ],
)
def test_unsupported_or_invalid_expression_degrades_to_no_temporal_condition(
    text: str,
) -> None:
    assert parse_temporal_query(text, now=NOW, timezone="Asia/Tokyo") is None
