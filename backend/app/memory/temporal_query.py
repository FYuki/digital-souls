from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import Enum
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.memory.persistence.contracts import TemporalPrecision


class TemporalQueryKind(str, Enum):
    MONTH = "MONTH"
    SEASON = "SEASON"
    RANGE = "RANGE"


class Season(str, Enum):
    SPRING = "SPRING"
    SUMMER = "SUMMER"
    AUTUMN = "AUTUMN"
    WINTER = "WINTER"


class SeasonMatchReasonCode(str, Enum):
    MATCHED = "MATCHED"
    OUTSIDE_RANGE = "OUTSIDE_RANGE"
    PRECISION_TOO_COARSE = "PRECISION_TOO_COARSE"
    PRECISION_UNKNOWN = "PRECISION_UNKNOWN"
    OCCURRED_AT_UNKNOWN = "OCCURRED_AT_UNKNOWN"


@dataclass(frozen=True)
class TemporalQuery:
    kind: TemporalQueryKind
    start: datetime
    end: datetime
    season: Season | None = None


@dataclass(frozen=True)
class SeasonMatch:
    matched: bool
    reason_code: SeasonMatchReasonCode


_RANGE_PATTERN = re.compile(
    r"(?P<start>\d{4}-\d{2}-\d{2})\s*(?:から|〜|～|-)\s*"
    r"(?P<end>\d{4}-\d{2}-\d{2})"
)
_ABSOLUTE_MONTH_PATTERN = re.compile(r"(?P<year>\d{4})年(?P<month>\d{1,2})月")
_LAST_YEAR_MONTH_PATTERN = re.compile(
    r"(?:昨年|去年)(?:の)?(?P<month>\d{1,2})月"
)
_SEASON_PATTERN = re.compile(r"(?P<year>今年|昨年|去年)(?:の)?(?P<season>春|夏|秋|冬)")
_SEASONS = {"春": Season.SPRING, "夏": Season.SUMMER, "秋": Season.AUTUMN, "冬": Season.WINTER}


def parse_temporal_query(
    text: str, *, now: datetime, timezone: str
) -> TemporalQuery | None:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    try:
        zone = ZoneInfo(timezone)
    except ZoneInfoNotFoundError as error:
        raise ValueError("timezone must be an IANA timezone") from error
    local_now = now.astimezone(zone)

    range_match = _RANGE_PATTERN.search(text)
    if range_match is not None:
        try:
            start_date = date.fromisoformat(range_match.group("start"))
            inclusive_end = date.fromisoformat(range_match.group("end"))
        except ValueError:
            return None
        if start_date > inclusive_end:
            return None
        return TemporalQuery(
            TemporalQueryKind.RANGE,
            _local_midnight(start_date.year, start_date.month, start_date.day, zone),
            _next_day_midnight(inclusive_end, zone),
        )

    season_match = _SEASON_PATTERN.search(text)
    if season_match is not None:
        season = _SEASONS[season_match.group("season")]
        year = local_now.year - (season_match.group("year") != "今年")
        return _season_query(year, season, zone)
    if "前年同季節" in text or "昨年同季節" in text or "去年の同じ季節" in text:
        current_season = season_for_month(local_now.month)
        current_start_year = (
            local_now.year - 1
            if current_season is Season.WINTER and local_now.month in (1, 2)
            else local_now.year
        )
        return _season_query(
            current_start_year - 1,
            current_season,
            zone,
        )

    absolute_match = _ABSOLUTE_MONTH_PATTERN.search(text)
    if absolute_match is not None:
        return _month_query(
            int(absolute_match.group("year")),
            int(absolute_match.group("month")),
            zone,
        )

    last_year_match = _LAST_YEAR_MONTH_PATTERN.search(text)
    if last_year_match is not None:
        return _month_query(
            local_now.year - 1, int(last_year_match.group("month")), zone
        )
    if "前年同月" in text or "昨年同月" in text or "去年の同じ月" in text:
        return _month_query(local_now.year - 1, local_now.month, zone)
    if "先月" in text:
        month_index = local_now.year * 12 + local_now.month - 2
        year, zero_based_month = divmod(month_index, 12)
        return _month_query(year, zero_based_month + 1, zone)
    return None


def season_for_month(month: int) -> Season:
    if month in (3, 4, 5):
        return Season.SPRING
    if month in (6, 7, 8):
        return Season.SUMMER
    if month in (9, 10, 11):
        return Season.AUTUMN
    if month in (12, 1, 2):
        return Season.WINTER
    raise ValueError("month must be between 1 and 12")


def match_season(
    query: TemporalQuery,
    *,
    occurred_at: datetime | None,
    occurred_precision: TemporalPrecision | None,
    occurred_timezone: str | None,
) -> SeasonMatch:
    if query.kind is not TemporalQueryKind.SEASON or query.season is None:
        raise ValueError("query must be a season query")
    if occurred_at is None or occurred_timezone is None:
        return SeasonMatch(False, SeasonMatchReasonCode.OCCURRED_AT_UNKNOWN)
    if occurred_precision is None:
        return SeasonMatch(False, SeasonMatchReasonCode.PRECISION_UNKNOWN)
    if occurred_precision is TemporalPrecision.YEAR:
        return SeasonMatch(False, SeasonMatchReasonCode.PRECISION_TOO_COARSE)
    try:
        zone = ZoneInfo(occurred_timezone)
    except ZoneInfoNotFoundError as error:
        raise ValueError("occurred_timezone must be an IANA timezone") from error
    local_month = occurred_at.astimezone(zone).month
    if (
        query.start <= occurred_at.astimezone(UTC) < query.end
        and season_for_month(local_month) is query.season
    ):
        return SeasonMatch(True, SeasonMatchReasonCode.MATCHED)
    return SeasonMatch(False, SeasonMatchReasonCode.OUTSIDE_RANGE)


def _month_query(year: int, month: int, zone: ZoneInfo) -> TemporalQuery | None:
    if month < 1 or month > 12:
        return None
    next_year, next_month = (year + 1, 1) if month == 12 else (year, month + 1)
    return TemporalQuery(
        TemporalQueryKind.MONTH,
        _local_midnight(year, month, 1, zone),
        _local_midnight(next_year, next_month, 1, zone),
    )


def _season_query(year: int, season: Season, zone: ZoneInfo) -> TemporalQuery:
    bounds = {
        Season.SPRING: (year, 3, year, 6),
        Season.SUMMER: (year, 6, year, 9),
        Season.AUTUMN: (year, 9, year, 12),
        Season.WINTER: (year, 12, year + 1, 3),
    }
    start_year, start_month, end_year, end_month = bounds[season]
    return TemporalQuery(
        TemporalQueryKind.SEASON,
        _local_midnight(start_year, start_month, 1, zone),
        _local_midnight(end_year, end_month, 1, zone),
        season,
    )


def _local_midnight(year: int, month: int, day: int, zone: ZoneInfo) -> datetime:
    return datetime(year, month, day, tzinfo=zone).astimezone(UTC)


def _next_day_midnight(value: date, zone: ZoneInfo) -> datetime:
    ordinal = value.toordinal() + 1
    next_date = date.fromordinal(ordinal)
    return _local_midnight(next_date.year, next_date.month, next_date.day, zone)
